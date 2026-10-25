import io
import json
import tempfile
import threading
import time
import unittest
import importlib
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if __package__:
    from .analysis_api import create_server
    from .import_jobs import ImportJobError, ReportImportJobManager
    from .warcraft_logs_importer import (analysis_directory, metadata_path,
                                         WarcraftLogsRateLimitError,
                                         download_checkpoint_path)
else:
    from analysis_api import create_server
    from import_jobs import ImportJobError, ReportImportJobManager
    from warcraft_logs_importer import (analysis_directory, metadata_path,
                                        WarcraftLogsRateLimitError,
                                        download_checkpoint_path)


CLIENT_ID = "private-client-id-for-test"
CLIENT_SECRET = "private-client-secret-for-test"
GUILD_ID = "123456789"


def fixture_report(code="Import001", fights=1):
    records = []
    events = {}
    for index in range(1, fights + 1):
        records.append({
            "id": index,
            "encounterID": 100 + index,
            "name": f"Boss {index}",
            "startTime": index * 1000,
            "endTime": index * 1000 + 5000,
            "kill": index % 2 == 0,
            "difficulty": 16,
            "size": 2,
        })
        events[str(index)] = []
    return {
        "code": code,
        "title": "Import fixture",
        "startTime": 0,
        "endTime": 10000,
        "fights": records,
        "masterData": {"actors": [], "abilities": []},
    }, events


class FakeClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret

    def download_report(self, report_id, destination, progress):
        progress(f"using {self.client_id} and {self.client_secret}")
        report, events = fixture_report(report_id)
        destination = Path(destination)
        destination.write_text(json.dumps({
            "format": "wow-log-tools-warcraft-logs-v1",
            "report": report,
            "eventsByFight": events,
        }), encoding="utf-8")
        metadata_path(destination).write_text(json.dumps({
            "format": "wow-log-tools-metadata-v1",
            "report": {key: report[key] for key in ("code", "title", "startTime", "endTime")},
            "fights": report["fights"],
        }), encoding="utf-8")
        return destination


def wait_for(manager, job_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.01)
    raise AssertionError("import job did not finish")


class ImportJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.temp.name)
        self.storage = self.root / "guilds" / GUILD_ID
        self.storage.mkdir(parents=True)
        self.manager = ReportImportJobManager(
            self.storage,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=FakeClient,
        )

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_background_import_analyzes_and_redacts_credentials(self):
        accepted = self.manager.submit("https://www.warcraftlogs.com/reports/Import001")
        self.assertEqual(accepted["report_id"], "Import001")
        job = wait_for(self.manager, accepted["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["analyzed_fights"], 1)
        self.assertEqual(job["result"]["fights"][0]["fight_id"], 1)
        self.assertEqual(job["progress"]["percent"], 100)
        self.assertEqual(job["progress"]["completed_fights"], 1)
        self.assertEqual(job["progress"]["total_fights"], 1)
        serialized = json.dumps(job)
        self.assertNotIn(CLIENT_ID, serialized)
        self.assertNotIn(CLIENT_SECRET, serialized)
        cache = self.storage / "warcraft-logs-Import001.wcl.json"
        self.assertTrue((analysis_directory(cache) / "fight-1.json.gz").is_file())
        self.assertNotIn("report_url", job)

    def test_background_progress_is_written_for_gui_console_without_credentials(self):
        output = io.StringIO()
        with redirect_stderr(output):
            accepted = self.manager.submit("Import001")
            job = wait_for(self.manager, accepted["job_id"])

        self.assertEqual(job["status"], "succeeded")
        logged = output.getvalue()
        self.assertIn("WCL_IMPORT_PROGRESS", logged)
        updates = [
            json.loads(line.split("WCL_IMPORT_PROGRESS ", 1)[1])
            for line in logged.splitlines() if "WCL_IMPORT_PROGRESS " in line
        ]
        self.assertEqual(updates[0]["status"], "queued")
        self.assertIn("download", [update["stage"] for update in updates])
        complete = next(update for update in updates if update["stage"] == "complete")
        self.assertEqual(complete["progress"]["percent"], 100)
        self.assertNotIn(CLIENT_ID, logged)
        self.assertNotIn(CLIENT_SECRET, logged)

    def test_analysis_progress_is_fractional_then_completes_at_100(self):
        second_started = threading.Event()
        release_second = threading.Event()

        class TwoFightClient(FakeClient):
            def download_report(self, report_id, destination, progress):
                report, events = fixture_report(report_id, fights=2)
                destination = Path(destination)
                destination.write_text(json.dumps({
                    "format": "wow-log-tools-warcraft-logs-v1",
                    "report": report,
                    "eventsByFight": events,
                }), encoding="utf-8")
                metadata_path(destination).write_text(json.dumps({
                    "format": "wow-log-tools-metadata-v1",
                    "report": {key: report[key]
                               for key in ("code", "title", "startTime", "endTime")},
                    "fights": report["fights"],
                }), encoding="utf-8")

        module = importlib.import_module(ReportImportJobManager.__module__)
        original = module.analyze_encounter

        def pause_before_second(destination, encounter, **kwargs):
            if encounter.external_id == 2:
                second_started.set()
                if not release_second.wait(2):
                    raise AssertionError("test did not release the second fight")
            return original(destination, encounter, **kwargs)

        manager = ReportImportJobManager(
            self.storage,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=TwoFightClient,
        )
        try:
            with mock.patch.object(
                    module, "analyze_encounter", side_effect=pause_before_second):
                accepted = manager.submit("ImportProgress")
                self.assertTrue(second_started.wait(2))
                running = manager.get(accepted["job_id"])
                self.assertEqual(running["status"], "running")
                self.assertEqual(running["stage"], "analysis")
                self.assertEqual(running["progress"]["completed_fights"], 1)
                self.assertEqual(running["progress"]["total_fights"], 2)
                self.assertEqual(running["progress"]["percent"], 50)
                manager._update(accepted["job_id"], percent=100)
                self.assertEqual(
                    manager.get(accepted["job_id"])["progress"]["percent"], 99)
                release_second.set()
                completed = wait_for(manager, accepted["job_id"])
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["progress"]["percent"], 100)
        finally:
            release_second.set()
            manager.close()

    def test_existing_complete_cache_skips_credentials_and_download(self):
        first = self.manager.submit("ImportCache1")
        self.assertEqual(wait_for(self.manager, first["job_id"])["status"], "succeeded")

        class MustNotDownload(FakeClient):
            def __init__(self, *_args):
                raise AssertionError("cached report must not construct an API client")

        manager = ReportImportJobManager(
            self.storage,
            credentials_loader=lambda: (_ for _ in ()).throw(
                AssertionError("cached report must not load credentials")
            ),
            client_factory=MustNotDownload,
        )
        try:
            accepted = manager.submit("ImportCache1")
            job = wait_for(manager, accepted["job_id"])
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(job["progress"]["percent"], 100)
            self.assertTrue(job["result"]["cache_hit"])
        finally:
            manager.close()

    def test_checkpoint_bypasses_cache_and_rate_limit_is_retryable(self):
        first = self.manager.submit("ImportRate1")
        self.assertEqual(wait_for(self.manager, first["job_id"])["status"], "succeeded")
        cache = self.storage / "warcraft-logs-ImportRate1.wcl.json"
        download_checkpoint_path(cache).write_text("{}", encoding="utf-8")

        class LimitedClient(FakeClient):
            def download_report(self, report_id, destination, progress):
                raise WarcraftLogsRateLimitError(45)

        manager = ReportImportJobManager(
            self.storage,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=LimitedClient,
        )
        try:
            accepted = manager.submit("ImportRate1")
            job = wait_for(manager, accepted["job_id"])
            self.assertEqual(job["status"], "rate_limited")
            self.assertTrue(job["result"]["retryable"])
            self.assertEqual(job["result"]["retry_after_seconds"], 45)
            self.assertTrue(job["result"]["checkpoint_saved"])
            self.assertEqual(job["errors"][0]["code"], "RATE_LIMITED")
            self.assertIsNone(job["progress"]["percent"])
        finally:
            manager.close()

    def test_missing_backend_credentials_fail_without_accepting_request_secrets(self):
        manager = ReportImportJobManager(
            self.storage, credentials_loader=lambda: ("", ""), client_factory=FakeClient
        )
        try:
            accepted = manager.submit("Import002")
            job = wait_for(manager, accepted["job_id"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["errors"][0]["code"], "CONFIGURATION_ERROR")
            self.assertNotIn("credentials_loader", json.dumps(job))
        finally:
            manager.close()

    def test_one_fight_failure_is_reported_without_stopping_later_fights(self):
        class TwoFightClient(FakeClient):
            def download_report(self, report_id, destination, progress):
                report, events = fixture_report(report_id, fights=2)
                destination = Path(destination)
                destination.write_text(json.dumps({
                    "format": "wow-log-tools-warcraft-logs-v1",
                    "report": report,
                    "eventsByFight": events,
                }), encoding="utf-8")
                metadata_path(destination).write_text(json.dumps({
                    "format": "wow-log-tools-metadata-v1",
                    "report": {key: report[key]
                               for key in ("code", "title", "startTime", "endTime")},
                    "fights": report["fights"],
                }), encoding="utf-8")

        manager = ReportImportJobManager(
            self.storage,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=TwoFightClient,
        )
        module = importlib.import_module(ReportImportJobManager.__module__)
        original = module.analyze_encounter

        def analyze(destination, encounter, **kwargs):
            if encounter.external_id == 1:
                raise ValueError("fixture analysis failure")
            return original(destination, encounter, **kwargs)

        try:
            with mock.patch.object(module, "analyze_encounter", side_effect=analyze):
                accepted = manager.submit("Import006")
                job = wait_for(manager, accepted["job_id"])
            self.assertEqual(job["status"], "partial")
            self.assertEqual(job["result"]["analyzed_fights"], 1)
            self.assertEqual(job["result"]["failed_fights"], 1)
            self.assertEqual(job["progress"]["percent"], 100)
            self.assertEqual(job["progress"]["completed_fights"], 2)
            self.assertEqual(job["progress"]["total_fights"], 2)
            self.assertEqual([item["status"] for item in job["result"]["fights"]],
                             ["failed", "succeeded"])
            self.assertEqual(job["errors"][0]["code"], "ANALYSIS_FAILED")
        finally:
            manager.close()

    def test_queue_is_bounded(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingClient(FakeClient):
            def download_report(self, report_id, destination, progress):
                started.set()
                release.wait(2)
                return super().download_report(report_id, destination, progress)

        manager = ReportImportJobManager(
            self.storage,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=BlockingClient,
            max_active=2,
        )
        try:
            manager.submit("Import003")
            self.assertTrue(started.wait(1))
            queued = manager.submit("Import004")
            self.assertEqual(queued["status"], "queued")
            self.assertIsNone(queued["progress"]["percent"])
            with self.assertRaises(ImportJobError) as raised:
                manager.submit("Import005")
            self.assertEqual(raised.exception.code, "IMPORT_CAPACITY_REACHED")
            self.assertEqual(raised.exception.status, 429)
            release.set()
            self.assertEqual(wait_for(manager, queued["job_id"])["status"], "succeeded")
        finally:
            release.set()
            manager.close(3)

    def test_reset_removes_only_guild_report_data_and_clears_job_history(self):
        accepted = self.manager.submit("ImportReset1")
        self.assertEqual(wait_for(self.manager, accepted["job_id"])["status"], "succeeded")
        other_guild = self.root / "guilds" / "987654321"
        other_guild.mkdir(parents=True)
        untouched = other_guild / "warcraft-logs-ImportReset1.wcl.json"
        untouched.write_text("other guild", encoding="utf-8")

        result = self.manager.reset_report_data()

        cache = self.storage / "warcraft-logs-ImportReset1.wcl.json"
        self.assertEqual(result["reports_removed"], 1)
        self.assertFalse(cache.exists())
        self.assertFalse(metadata_path(cache).exists())
        self.assertFalse(download_checkpoint_path(cache).exists())
        self.assertFalse(analysis_directory(cache).exists())
        self.assertTrue(untouched.is_file())
        with self.assertRaises(ImportJobError) as raised:
            self.manager.get(accepted["job_id"])
        self.assertEqual(raised.exception.code, "IMPORT_JOB_NOT_FOUND")

    def test_reset_removes_orphaned_and_temporary_report_artifacts(self):
        report_id = "Orphan001"
        cache = self.storage / f"warcraft-logs-{report_id}.wcl.json"
        artifacts = (
            cache.with_name(cache.name + ".tmp"),
            cache.with_name(cache.name + ".part"),
            cache.with_name(cache.name + ".download.json.tmp"),
            cache.with_name(cache.name + ".download.json.part"),
            self.storage / f"warcraft-logs-{report_id}.meta.json",
            self.storage / f"warcraft-logs-{report_id}.meta.json.tmp",
        )
        for artifact in artifacts:
            artifact.write_text("partial", encoding="utf-8")
        orphan_analysis = self.storage / f"warcraft-logs-{report_id}.analysis"
        orphan_analysis.mkdir()
        (orphan_analysis / "fight-1.json.gz.part").write_text(
            "partial", encoding="utf-8",
        )
        unrelated = self.storage / "keep-me.txt"
        unrelated.write_text("unrelated", encoding="utf-8")

        result = self.manager.reset_report_data()

        self.assertEqual(result["reports_removed"], 1)
        self.assertTrue(all(not artifact.exists() for artifact in artifacts))
        self.assertFalse(orphan_analysis.exists())
        self.assertTrue(unrelated.is_file())

    def test_reset_refuses_while_an_import_is_active(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingClient(FakeClient):
            def download_report(self, report_id, destination, progress):
                started.set()
                release.wait(2)
                return super().download_report(report_id, destination, progress)

        manager = ReportImportJobManager(
            self.storage, credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=BlockingClient,
        )
        try:
            manager.submit("ImportReset2")
            self.assertTrue(started.wait(1))
            with self.assertRaises(ImportJobError) as raised:
                manager.reset_report_data()
            self.assertEqual(raised.exception.code, "IMPORTS_ACTIVE")
            release.set()
        finally:
            release.set()
            manager.close(3)

    def test_http_reset_is_scoped_to_the_requested_guild(self):
        accepted = self.manager.submit("ImportReset3")
        self.assertEqual(wait_for(self.manager, accepted["job_id"])["status"], "succeeded")
        other_storage = self.root / "guilds" / "987654321"
        other_storage.mkdir(parents=True)
        other_cache = other_storage / "warcraft-logs-ImportReset3.wcl.json"
        other_cache.write_text("other guild", encoding="utf-8")
        server = create_server(
            self.root, port=0, import_jobs_by_guild={GUILD_ID: self.manager},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/v1/imports/reset",
            data=b"{}",
            headers={"Content-Type": "application/json",
                     "X-Carter-Guild-ID": GUILD_ID},
            method="POST",
        )
        try:
            with urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result["status"], "reset")
            self.assertFalse((self.storage / "warcraft-logs-ImportReset3.wcl.json").exists())
            self.assertTrue(other_cache.is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_post_returns_202_and_get_returns_terminal_job(self):
        server = create_server(
            self.root, port=0, import_jobs_by_guild={GUILD_ID: self.manager},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/v1"
        try:
            request = Request(
                base + "/imports",
                data=json.dumps({"report_url": "Import005", "refresh": True}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Carter-Guild-ID": GUILD_ID},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
                accepted = json.load(response)
            self.assertNotIn(str(self.root), json.dumps(accepted))
            self.assertEqual(accepted["status"], "queued")
            self.assertIsNone(accepted["progress"]["percent"])
            deadline = time.monotonic() + 3
            while True:
                job_request = Request(
                    base + "/imports/" + accepted["job_id"],
                    headers={"X-Carter-Guild-ID": GUILD_ID},
                )
                with urlopen(job_request) as response:
                    job = json.load(response)
                if job["status"] not in ("queued", "running"):
                    break
                if time.monotonic() >= deadline:
                    self.fail("HTTP import job did not finish")
                time.sleep(0.01)
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(job["stage"], "complete")
            self.assertEqual(job["progress"]["percent"], 100)
            self.assertEqual(job["progress"]["completed_fights"], 1)
            self.assertEqual(job["progress"]["total_fights"], 1)
            self.assertTrue(job["refresh"])
            self.assertTrue(
                (self.storage / "warcraft-logs-Import005.wcl.json").is_file())
            self.assertFalse(
                (self.root / "guilds" / "987654321" /
                 "warcraft-logs-Import005.wcl.json").exists())

            other_job = Request(
                base + "/imports/" + accepted["job_id"],
                headers={"X-Carter-Guild-ID": "987654321"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(other_job)
            self.assertEqual(raised.exception.code, 404)
            self.assertEqual(
                json.load(raised.exception)["error"]["code"], "IMPORT_JOB_NOT_FOUND")

            bad = Request(
                base + "/imports", data=b"[]",
                headers={"Content-Type": "application/json",
                         "X-Carter-Guild-ID": GUILD_ID}, method="POST"
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(bad)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(json.load(raised.exception)["error"]["code"], "INVALID_JSON")

            client_path = Request(
                base + "/imports",
                data=json.dumps({"report_url": "Import007", "destination": "/tmp/client"}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Carter-Guild-ID": GUILD_ID}, method="POST"
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(client_path)
            self.assertEqual(raised.exception.code, 400)
            rejected = json.load(raised.exception)
            self.assertEqual(rejected["error"]["code"], "INVALID_PARAMETER")
            self.assertNotIn("/tmp/client", json.dumps(rejected))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
