"""Bounded background jobs for importing and analyzing Warcraft Logs reports."""

from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
import re
import threading
import uuid

if __package__:
    from .encounter_parser import analyze_encounter, scan_encounters
    from .warcraft_logs_importer import (
        WarcraftLogsClient,
        WarcraftLogsError,
        extract_report_code,
        load_credentials,
    )
else:  # Support ``python analysis_api.py`` from this directory.
    from encounter_parser import analyze_encounter, scan_encounters
    from warcraft_logs_importer import (
        WarcraftLogsClient,
        WarcraftLogsError,
        extract_report_code,
        load_credentials,
    )


IMPORT_SCHEMA_VERSION = 1
MAX_ACTIVE_IMPORTS = 4
MAX_IMPORT_JOBS = 50
MAX_PROGRESS_EVENTS = 50
MAX_JOB_ERRORS = 50
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ImportJobError(Exception):
    """Stable, JSON-friendly import job error."""

    def __init__(self, code: str, message: str, status: int = 400, **details):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details

    def response(self):
        return {"error": {"code": self.code, "message": self.message, **self.details}}


class ReportImportJobManager:
    """Run report imports outside HTTP/Discord threads with bounded retained state.

    A single worker intentionally serializes imports. The importer prunes shared
    report caches after a download, so concurrent download/analysis jobs could
    otherwise remove a cache while another job is analyzing it.
    """

    def __init__(
        self,
        storage: Path,
        *,
        credentials_loader=load_credentials,
        client_factory=WarcraftLogsClient,
        max_active: int = MAX_ACTIVE_IMPORTS,
        max_jobs: int = MAX_IMPORT_JOBS,
    ):
        self.storage = Path(storage).resolve()
        self.credentials_loader = credentials_loader
        self.client_factory = client_factory
        self.max_active = max(1, int(max_active))
        self.max_jobs = max(self.max_active, int(max_jobs))
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._progress: dict[str, deque] = {}
        self._queue: Queue[str] = Queue(maxsize=self.max_active)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._work, name="warcraft-report-import", daemon=True
        )
        self._worker.start()

    @staticmethod
    def _safe_message(error: Exception, secrets=()) -> str:
        if isinstance(error, (WarcraftLogsError, ValueError)):
            message = str(error).strip() or "The report could not be processed."
        else:
            message = "The report could not be processed by the analysis backend."
        for secret in secrets:
            if secret:
                message = message.replace(str(secret), "[redacted]")
        return message[:500]

    def _prune_locked(self):
        while len(self._jobs) >= self.max_jobs:
            removable = next(
                (job_id for job_id, job in self._jobs.items()
                 if job["status"] in TERMINAL_STATES),
                None,
            )
            if removable is None:
                break
            self._jobs.pop(removable, None)
            self._progress.pop(removable, None)

    def submit(self, report_url: str) -> dict:
        if not isinstance(report_url, str) or not report_url.strip():
            raise ImportJobError(
                "REPORT_URL_REQUIRED", "report_url must be a Warcraft Logs URL or report code."
            )
        try:
            report_id = extract_report_code(report_url)
        except ValueError as error:
            raise ImportJobError("INVALID_REPORT_URL", str(error)) from error

        with self._lock:
            for existing in self._jobs.values():
                if (existing["report_id"].casefold() == report_id.casefold()
                        and existing["status"] in ACTIVE_STATES):
                    result = self._public_locked(existing["job_id"])
                    result["deduplicated"] = True
                    return result
            active = sum(job["status"] in ACTIVE_STATES for job in self._jobs.values())
            if active >= self.max_active:
                raise ImportJobError(
                    "IMPORT_CAPACITY_REACHED",
                    "The report import queue is full. Try again after a current job finishes.",
                    429,
                )
            self._prune_locked()
            if len(self._jobs) >= self.max_jobs:
                raise ImportJobError(
                    "IMPORT_CAPACITY_REACHED",
                    "Import job history is full while jobs are still active.",
                    429,
                )
            job_id = uuid.uuid4().hex
            timestamp = _now()
            self._jobs[job_id] = {
                "schema_version": IMPORT_SCHEMA_VERSION,
                "job_id": job_id,
                "report_id": report_id,
                "status": "queued",
                "stage": "queued",
                "created_at": timestamp,
                "updated_at": timestamp,
                "progress": {
                    "message": "Report import queued.",
                    "completed_fights": 0,
                    "total_fights": None,
                },
                "errors": [],
                "result": None,
            }
            self._progress[job_id] = deque(
                [{"timestamp": timestamp, "stage": "queued", "message": "Report import queued."}],
                maxlen=MAX_PROGRESS_EVENTS,
            )
            try:
                self._queue.put_nowait(job_id)
            except Full:
                self._jobs.pop(job_id, None)
                self._progress.pop(job_id, None)
                raise ImportJobError(
                    "IMPORT_CAPACITY_REACHED", "The report import queue is full.", 429
                )
            return self._public_locked(job_id)

    def get(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ImportJobError("IMPORT_JOB_NOT_FOUND", "The import job was not found.", 404)
        with self._lock:
            if job_id not in self._jobs:
                raise ImportJobError("IMPORT_JOB_NOT_FOUND", "The import job was not found.", 404)
            return self._public_locked(job_id)

    def _public_locked(self, job_id: str) -> dict:
        result = deepcopy(self._jobs[job_id])
        result["progress_events"] = deepcopy(list(self._progress.get(job_id, ())))
        return result

    def _update(self, job_id: str, *, status=None, stage=None, message=None, **progress):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if status is not None:
                job["status"] = status
            if stage is not None:
                job["stage"] = stage
            if message is not None:
                job["progress"]["message"] = str(message)[:500]
            job["progress"].update(progress)
            job["updated_at"] = _now()
            if message is not None:
                self._progress[job_id].append({
                    "timestamp": job["updated_at"],
                    "stage": job["stage"],
                    "message": str(message)[:500],
                })

    def _add_error(self, job_id: str, code: str, message: str, **details):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or len(job["errors"]) >= MAX_JOB_ERRORS:
                return
            job["errors"].append({"code": code, "message": message[:500], **details})
            job["updated_at"] = _now()

    def _finish_failed(self, job_id: str, code: str, message: str):
        self._add_error(job_id, code, message)
        self._update(job_id, status="failed", stage="failed", message=message)

    def _work(self):
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                try:
                    self._run(job_id)
                except Exception as error:
                    self._finish_failed(
                        job_id, "IMPORT_FAILED", self._safe_message(error)
                    )
            finally:
                self._queue.task_done()

    def _run(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            report_id = job["report_id"]
        self._update(
            job_id, status="running", stage="configuration",
            message="Loading backend Warcraft Logs credentials.",
        )
        try:
            client_id, client_secret = self.credentials_loader()
        except Exception as error:
            self._finish_failed(
                job_id, "CONFIGURATION_ERROR", self._safe_message(error)
            )
            return
        if not client_id or not client_secret:
            self._finish_failed(
                job_id,
                "CONFIGURATION_ERROR",
                "Backend Warcraft Logs credentials are not configured.",
            )
            return
        secrets = (client_id, client_secret)
        destination = self.storage / f"warcraft-logs-{report_id}.wcl.json"
        try:
            self.storage.mkdir(parents=True, exist_ok=True)
            client = self.client_factory(client_id, client_secret)
            self._update(job_id, stage="download", message="Downloading Warcraft Logs report.")

            def download_progress(message):
                safe = str(message)
                for secret in secrets:
                    if secret:
                        safe = safe.replace(str(secret), "[redacted]")
                self._update(job_id, stage="download", message=safe[:500])

            client.download_report(report_id, destination, download_progress)
            encounters = scan_encounters(destination)
        except Exception as error:
            self._finish_failed(
                job_id, "DOWNLOAD_FAILED", self._safe_message(error, secrets)
            )
            return

        total = len(encounters)
        if total == 0:
            self._finish_failed(
                job_id, "NO_FIGHTS", "The imported report contains no boss fights."
            )
            return
        self._update(
            job_id, stage="analysis", message=f"Analyzing {total} boss fights.",
            total_fights=total, completed_fights=0,
        )
        fights = []
        analyzed = 0
        for index, encounter in enumerate(encounters, 1):
            fight_id = encounter.external_id if encounter.external_id is not None else encounter.attempt
            try:
                # analyze_encounter persists the result atomically through save_analysis.
                analyze_encounter(destination, encounter)
                analyzed += 1
                fights.append({
                    "fight_id": int(fight_id),
                    "encounter_id": int(encounter.encounter_id),
                    "name": encounter.name,
                    "status": "succeeded",
                })
            except Exception as error:
                message = self._safe_message(error, secrets)
                fights.append({
                    "fight_id": int(fight_id),
                    "encounter_id": int(encounter.encounter_id),
                    "name": encounter.name,
                    "status": "failed",
                })
                self._add_error(
                    job_id, "ANALYSIS_FAILED", message, fight_id=int(fight_id)
                )
            self._update(
                job_id,
                stage="analysis",
                message=f"Analyzed fight {index} of {total}.",
                completed_fights=index,
                total_fights=total,
            )

        failed = total - analyzed
        status = "succeeded" if failed == 0 else ("partial" if analyzed else "failed")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["result"] = {
                "report_id": report_id,
                "fight_count": total,
                "analyzed_fights": analyzed,
                "failed_fights": failed,
                "fights": fights,
            }
        message = (
            f"Imported and analyzed {analyzed} of {total} fights."
            if status != "failed" else "The report was imported, but no fights were analyzed."
        )
        self._update(job_id, status=status, stage="complete", message=message)

    def close(self, timeout: float = 1.0):
        self._stop.set()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=max(0.0, float(timeout)))
