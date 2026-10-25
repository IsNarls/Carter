"""Bounded background jobs for importing and analyzing Warcraft Logs reports."""

from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
import shutil
import sys
import threading
import uuid

if __package__:
    from .encounter_parser import (analyze_encounter, scan_encounters,
                                   scan_warcraft_logs_encounters)
    from .warcraft_logs_importer import (
        WarcraftLogsClient,
        WarcraftLogsError,
        WarcraftLogsRateLimitError,
        download_checkpoint_path,
        extract_report_code,
        load_credentials,
        analysis_directory,
        metadata_path,
        load_cache,
    )
else:  # Support ``python analysis_api.py`` from this directory.
    from encounter_parser import (analyze_encounter, scan_encounters,
                                  scan_warcraft_logs_encounters)
    from warcraft_logs_importer import (
        WarcraftLogsClient,
        WarcraftLogsError,
        WarcraftLogsRateLimitError,
        download_checkpoint_path,
        extract_report_code,
        load_credentials,
        analysis_directory,
        metadata_path,
        load_cache,
    )


IMPORT_SCHEMA_VERSION = 1
MAX_ACTIVE_IMPORTS = 4
MAX_IMPORT_JOBS = 50
MAX_PROGRESS_EVENTS = 50
MAX_JOB_ERRORS = 50
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "rate_limited"})


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

    def submit(self, report_url: str, *, refresh: bool = False) -> dict:
        if not isinstance(report_url, str) or not report_url.strip():
            raise ImportJobError(
                "REPORT_URL_REQUIRED", "report_url must be a Warcraft Logs URL or report code."
            )
        try:
            report_id = extract_report_code(report_url)
        except ValueError as error:
            raise ImportJobError("INVALID_REPORT_URL", str(error)) from error
        if not isinstance(refresh, bool):
            raise ImportJobError("INVALID_REFRESH", "refresh must be true or false.")

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
                "refresh": refresh,
                "status": "queued",
                "stage": "queued",
                "created_at": timestamp,
                "updated_at": timestamp,
                "progress": {
                    "message": "Report import queued.",
                    "percent": None,
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
            self._log_progress_locked(self._jobs[job_id])
            return self._public_locked(job_id)

    def get(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ImportJobError("IMPORT_JOB_NOT_FOUND", "The import job was not found.", 404)
        with self._lock:
            if job_id not in self._jobs:
                raise ImportJobError("IMPORT_JOB_NOT_FOUND", "The import job was not found.", 404)
            return self._public_locked(job_id)

    def reset_report_data(self) -> dict:
        """Delete this guild's Warcraft Logs caches and retained import state."""
        with self._lock:
            if any(job["status"] in ACTIVE_STATES for job in self._jobs.values()):
                raise ImportJobError(
                    "IMPORTS_ACTIVE",
                    "Guild report data cannot be reset while an import is queued or running.",
                    409,
                )

            report_ids = {job["report_id"] for job in self._jobs.values()}
            artifact = re.compile(
                r"warcraft-logs-([A-Za-z0-9]{8,32})"
                r"(?:\.wcl\.json(?:\.download\.json)?|\.meta\.json|\.analysis)"
                r"(?:\.tmp|\.part)?$",
                re.IGNORECASE,
            )
            # Discover report IDs from every recognized final or atomic-temporary
            # artifact. This also cleans interrupted imports that never produced a
            # final cache/job record, without touching unrelated guild files.
            for path in self.storage.glob("warcraft-logs-*"):
                match = artifact.fullmatch(path.name)
                if match:
                    report_ids.add(match.group(1))

            removed_files = 0
            removed_analysis = 0
            try:
                for report_id in report_ids:
                    cache = self.storage / f"warcraft-logs-{report_id}.wcl.json"
                    related_files = (
                        cache,
                        cache.with_name(cache.name + ".tmp"),
                        cache.with_name(cache.name + ".part"),
                        metadata_path(cache),
                        metadata_path(cache).with_name(metadata_path(cache).name + ".tmp"),
                        metadata_path(cache).with_name(metadata_path(cache).name + ".part"),
                        cache.with_name(cache.name + ".download.json"),
                        cache.with_name(cache.name + ".download.json.tmp"),
                        cache.with_name(cache.name + ".download.json.part"),
                    )
                    for path in related_files:
                        if path.is_file() or path.is_symlink():
                            path.unlink()
                            removed_files += 1
                    sidecar = analysis_directory(cache)
                    if sidecar.is_symlink():
                        sidecar.unlink()
                        removed_files += 1
                    elif sidecar.is_dir() and sidecar.resolve().parent == self.storage:
                        shutil.rmtree(sidecar)
                        removed_analysis += 1
            except OSError as error:
                raise ImportJobError(
                    "RESET_FAILED", "Guild report data could not be fully reset.", 500,
                ) from error

            for report_id in sorted(report_ids, key=str.casefold):
                timestamp = _now()
                event = {
                    "report_id": report_id,
                    "status": "reset",
                    "stage": "reset",
                    "progress": {
                        "percent": None,
                        "completed_fights": 0,
                        "total_fights": None,
                        "message": "Guild report data was reset.",
                    },
                    "errors": [],
                }
                print(
                    f"{timestamp} WCL_IMPORT_PROGRESS "
                    f"{json.dumps(event, ensure_ascii=True, separators=(',', ':'))}",
                    file=sys.stderr,
                    flush=True,
                )
            self._jobs.clear()
            self._progress.clear()
            return {
                "status": "reset",
                "reports_removed": len(report_ids),
                "files_removed": removed_files,
                "analysis_directories_removed": removed_analysis,
            }

    def _public_locked(self, job_id: str) -> dict:
        result = deepcopy(self._jobs[job_id])
        result["progress_events"] = deepcopy(list(self._progress.get(job_id, ())))
        return result

    @staticmethod
    def _log_progress_locked(job):
        progress_event = {
            "report_id": job["report_id"],
            "status": job["status"],
            "stage": job["stage"],
            "progress": job["progress"],
            "errors": job["errors"],
        }
        print(
            f"{job['updated_at']} WCL_IMPORT_PROGRESS "
            f"{json.dumps(progress_event, ensure_ascii=True, separators=(',', ':'))}",
            file=sys.stderr,
            flush=True,
        )

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
            percent = job["progress"].get("percent")
            if isinstance(percent, int):
                percent = max(0, min(100, percent))
                if job["status"] in ACTIVE_STATES:
                    percent = min(99, percent)
                job["progress"]["percent"] = percent
            job["updated_at"] = _now()
            if message is not None:
                self._log_progress_locked(job)
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

    def _finish_rate_limited(self, job_id: str, error: WarcraftLogsRateLimitError,
                             checkpoint_saved: bool):
        details = {"checkpoint_saved": bool(checkpoint_saved), "retryable": True}
        if error.retry_after_seconds is not None:
            details["retry_after_seconds"] = error.retry_after_seconds
        message = (
            "Warcraft Logs rate limit reached. Saved progress will resume on the next import."
            if checkpoint_saved else
            "Warcraft Logs rate limit reached before any download page was saved. Retry later."
        )
        self._add_error(
            job_id, "RATE_LIMITED",
            message,
            **details,
        )
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["result"] = {"report_id": job["report_id"], **details}
        self._update(
            job_id, status="rate_limited", stage="paused",
            message=message,
        )

    def _finish_download_failed(self, job_id: str, message: str,
                                checkpoint_saved: bool):
        details = {"checkpoint_saved": bool(checkpoint_saved),
                   "retryable": bool(checkpoint_saved)}
        self._add_error(job_id, "DOWNLOAD_FAILED", message, **details)
        if checkpoint_saved:
            message = message + " Saved pages will resume on the next import."
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
            refresh = bool(job.get("refresh"))
        destination = self.storage / f"warcraft-logs-{report_id}.wcl.json"
        encounters = None
        cache_payload = None
        cache_hit = False
        secrets = ()
        checkpoint = download_checkpoint_path(destination)

        if destination.is_file() and not refresh and not checkpoint.is_file():
            try:
                cache_payload = load_cache(destination)
                encounters = scan_warcraft_logs_encounters(destination, cache_payload)
                cache_hit = bool(encounters)
            except Exception:
                encounters = None
            if cache_hit:
                self._update(
                    job_id, status="running", stage="cache",
                    message="Using the previously downloaded local report cache.",
                )

        if not cache_hit:
            self._update(
                job_id, status="running", stage="configuration",
                message="Loading backend Warcraft Logs credentials.",
                percent=None, completed_fights=0, total_fights=None,
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
            try:
                self.storage.mkdir(parents=True, exist_ok=True)
                client = self.client_factory(client_id, client_secret)
                self._update(
                    job_id, stage="download", message="Downloading Warcraft Logs report.",
                    percent=None, completed_fights=0, total_fights=None,
                )

                def download_progress(message):
                    safe = str(message)
                    for secret in secrets:
                        if secret:
                            safe = safe.replace(str(secret), "[redacted]")
                    self._update(job_id, stage="download", message=safe[:500])

                client.download_report(report_id, destination, download_progress)
                cache_payload = load_cache(destination)
                encounters = scan_warcraft_logs_encounters(destination, cache_payload)
            except WarcraftLogsRateLimitError as error:
                self._finish_rate_limited(
                    job_id, error, checkpoint.is_file(),
                )
                return
            except Exception as error:
                self._finish_download_failed(
                    job_id, self._safe_message(error, secrets),
                    checkpoint.is_file(),
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
            percent=0, total_fights=total, completed_fights=0,
        )
        fights = []
        analyzed = 0
        for index, encounter in enumerate(encounters, 1):
            fight_id = encounter.external_id if encounter.external_id is not None else encounter.attempt
            try:
                # analyze_encounter persists the result atomically through save_analysis.
                analyze_encounter(destination, encounter, wcl_cache=cache_payload)
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
                percent=min(99, int(index * 100 / total)),
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
                "cache_hit": cache_hit,
                "fight_count": total,
                "analyzed_fights": analyzed,
                "failed_fights": failed,
                "fights": fights,
            }
        message = (
            f"Imported and analyzed {analyzed} of {total} fights."
            if status != "failed" else "The report was imported, but no fights were analyzed."
        )
        self._update(
            job_id, status=status, stage="complete", message=message,
            percent=100, completed_fights=total, total_fights=total,
        )

    def close(self, timeout: float = 1.0):
        self._stop.set()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=max(0.0, float(timeout)))
