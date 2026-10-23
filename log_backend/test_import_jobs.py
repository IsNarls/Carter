import json
import tempfile
import threading
import time
import unittest
import importlib
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if __package__:
    from .analysis_api import create_server
    from .import_jobs import ImportJobError, ReportImportJobManager
    from .warcraft_logs_importer import analysis_directory, metadata_path
else:
    from analysis_api import create_server
    from import_jobs import ImportJobError, ReportImportJobManager
    from warcraft_logs_importer import analysis_directory, metadata_path


CLIENT_ID = "private-client-id-for-test"
CLIENT_SECRET = "private-client-secret-for-test"


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
        self.manager = ReportImportJobManager(
            self.root,
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
        serialized = json.dumps(job)
        self.assertNotIn(CLIENT_ID, serialized)
        self.assertNotIn(CLIENT_SECRET, serialized)
        cache = self.root / "warcraft-logs-Import001.wcl.json"
        self.assertTrue((analysis_directory(cache) / "fight-1.json.gz").is_file())
        self.assertNotIn("report_url", job)

    def test_missing_backend_credentials_fail_without_accepting_request_secrets(self):
        manager = ReportImportJobManager(
            self.root, credentials_loader=lambda: ("", ""), client_factory=FakeClient
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
            self.root,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=TwoFightClient,
        )
        module = importlib.import_module(ReportImportJobManager.__module__)
        original = module.analyze_encounter

        def analyze(destination, encounter):
            if encounter.external_id == 1:
                raise ValueError("fixture analysis failure")
            return original(destination, encounter)

        try:
            with mock.patch.object(module, "analyze_encounter", side_effect=analyze):
                accepted = manager.submit("Import006")
                job = wait_for(manager, accepted["job_id"])
            self.assertEqual(job["status"], "partial")
            self.assertEqual(job["result"]["analyzed_fights"], 1)
            self.assertEqual(job["result"]["failed_fights"], 1)
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
            self.root,
            credentials_loader=lambda: (CLIENT_ID, CLIENT_SECRET),
            client_factory=BlockingClient,
            max_active=1,
        )
        try:
            manager.submit("Import003")
            self.assertTrue(started.wait(1))
            with self.assertRaises(ImportJobError) as raised:
                manager.submit("Import004")
            self.assertEqual(raised.exception.code, "IMPORT_CAPACITY_REACHED")
            self.assertEqual(raised.exception.status, 429)
        finally:
            release.set()
            manager.close(3)

    def test_http_post_returns_202_and_get_returns_terminal_job(self):
        server = create_server(self.root, port=0, import_jobs=self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/v1"
        try:
            request = Request(
                base + "/imports",
                data=json.dumps({"report_url": "Import005"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
                accepted = json.load(response)
            self.assertNotIn(str(self.root), json.dumps(accepted))
            deadline = time.monotonic() + 3
            while True:
                with urlopen(base + "/imports/" + accepted["job_id"]) as response:
                    job = json.load(response)
                if job["status"] not in ("queued", "running"):
                    break
                if time.monotonic() >= deadline:
                    self.fail("HTTP import job did not finish")
                time.sleep(0.01)
            self.assertEqual(job["status"], "succeeded")

            bad = Request(
                base + "/imports", data=b"[]",
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(bad)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(json.load(raised.exception)["error"]["code"], "INVALID_JSON")

            client_path = Request(
                base + "/imports",
                data=json.dumps({"report_url": "Import007", "destination": "/tmp/client"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST"
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
