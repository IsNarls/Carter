"""Offline contract tests for the bounded analysis HTTP/image adapter."""

import base64
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from analysis_client import (
    AnalysisClient, AnalysisClientError, AnalysisClientSettings,
    answer_without_image_bytes, render_answer_image,
)


class FakeContent:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, _size):
        yield self.body


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.body = json.dumps(payload).encode()
        self.content_length = len(self.body)
        self.content = FakeContent(self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


class SettingsTests(unittest.TestCase):
    def test_loopback_default_and_https_remote_are_allowed(self):
        AnalysisClientSettings(True).validated()
        AnalysisClientSettings(True, "https://analysis.example/api/v1").validated()

    def test_plain_remote_http_and_credentials_in_url_are_rejected(self):
        for url in (
            "http://analysis.example/api/v1",
            "https://user:password@analysis.example/api/v1",
            "file:///tmp/api/v1",
            "https://analysis.example/not-v1",
        ):
            with self.assertRaises(ValueError):
                AnalysisClientSettings(True, url).validated()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_path_and_parameters(self):
        session = FakeSession([FakeResponse(200, {"players": []})])
        client = AnalysisClient(AnalysisClientSettings(True), session)
        await client.ranking("AbCdEf123456", 7, "dps", 5)
        method, url, kwargs = session.requests[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/reports/AbCdEf123456/fights/7/rankings/dps"))
        self.assertEqual(kwargs["params"], {"limit": 5})
        self.assertIsNone(kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])

    async def test_guild_client_attaches_scope_header_to_every_request(self):
        session = FakeSession([FakeResponse(200, {"reports": []})])
        client = AnalysisClient(AnalysisClientSettings(True), session).for_guild(123456789)

        await client.reports()

        self.assertEqual(
            session.requests[0][2]["headers"], {"X-Carter-Guild-ID": "123456789"},
        )

    async def test_global_fight_index_uses_guild_scope(self):
        session = FakeSession([FakeResponse(200, {"fights": []})])
        client = AnalysisClient(AnalysisClientSettings(True), session).for_guild(123456789)

        await client.all_fights()

        self.assertTrue(session.requests[0][1].endswith("/fights"))
        self.assertEqual(
            session.requests[0][2]["headers"], {"X-Carter-Guild-ID": "123456789"},
        )

    async def test_live_updates_are_bounded_and_guild_scoped(self):
        session = FakeSession([FakeResponse(200, {"updates": [], "cursor": 44})])
        client = AnalysisClient(AnalysisClientSettings(True), session).for_guild(123456789)

        await client.live_updates(40, 100)

        method, url, kwargs = session.requests[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/live/updates"))
        self.assertEqual(kwargs["params"], {"since": 40, "limit": 100})
        self.assertEqual(
            kwargs["headers"], {"X-Carter-Guild-ID": "123456789"},
        )

    async def test_selected_announcement_routes_are_guild_scoped(self):
        session = FakeSession([FakeResponse(200, {}) for _ in range(4)])
        client = AnalysisClient(AnalysisClientSettings(True), session).for_guild(123456789)
        await client.live_announcement_source('companion')
        await client.live_announcement_source()
        await client.live_announcement_status()
        await client.live_announcement_updates(12, 100)
        self.assertEqual(session.requests[0][0], 'POST')
        self.assertEqual(session.requests[0][2]['json'], {'source': 'companion'})
        self.assertTrue(session.requests[-1][1].endswith('/live/announcements'))
        self.assertEqual(session.requests[-1][2]['params'], {'since': 12, 'limit': 100})
        for _, _, kwargs in session.requests:
            self.assertEqual(kwargs['headers'], {'X-Carter-Guild-ID': '123456789'})

    async def test_structured_error_is_sanitized(self):
        session = FakeSession([FakeResponse(409, {
            "error": {"code": "AMBIGUOUS_PLAYER", "message": "private detail",
                      "candidates": [{"name": "Alex"}]},
        })])
        client = AnalysisClient(AnalysisClientSettings(True), session)
        with self.assertRaises(AnalysisClientError) as caught:
            await client.players("AbCdEf123456", 7, "Alex")
        self.assertEqual(caught.exception.code, "AMBIGUOUS_PLAYER")
        self.assertNotIn("private detail", str(caught.exception))
        self.assertEqual(caught.exception.details["candidates"][0]["name"], "Alex")

    async def test_disabled_client_never_opens_transport(self):
        session = FakeSession([])
        client = AnalysisClient(AnalysisClientSettings(False), session)
        with self.assertRaisesRegex(AnalysisClientError, "analysis_disabled"):
            await client.health()
        self.assertFalse(session.requests)

    async def test_import_uses_backend_owned_report_url_contract(self):
        session = FakeSession([FakeResponse(202, {"job_id": "a" * 32})])
        client = AnalysisClient(AnalysisClientSettings(True), session)
        await client.import_report("https://www.warcraftlogs.com/reports/AbCdEf123456")
        method, url, kwargs = session.requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/imports"))
        self.assertEqual(kwargs["json"], {
            "report_url": "https://www.warcraftlogs.com/reports/AbCdEf123456",
            "refresh": False,
        })

    async def test_guild_reset_uses_the_guild_scoped_backend_endpoint(self):
        session = FakeSession([FakeResponse(200, {"status": "reset"})])
        client = AnalysisClient(AnalysisClientSettings(True), session).for_guild(123456789)

        await client.reset_import_data()

        method, url, kwargs = session.requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/imports/reset"))
        self.assertEqual(kwargs["json"], {})
        self.assertEqual(
            kwargs["headers"], {"X-Carter-Guild-ID": "123456789"},
        )


class ImageTests(unittest.TestCase):
    def payload(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>safe</text></svg>'
        return {
            "report_id": "AbCdEf123456", "fight_id": 7, "death_id": "death-1",
            "answer": {"text": "Alex died."},
            "analysis": {"player": {"name": "Alex"}, "time_seconds": 42.0},
            "image": {"status": "available", "mime_type": "image/svg+xml",
                      "encoding": "base64", "data": base64.b64encode(svg).decode(),
                      "alt_text": "Alex death position"},
        }

    def test_svg_is_rendered_to_bounded_png(self):
        def render(*, bytestring, write_to, **_kwargs):
            self.assertIn(b"<svg", bytestring)
            write_to.write(b"\x89PNG\r\n\x1a\nfixture")

        module = SimpleNamespace(svg2png=render)
        with patch.dict(sys.modules, {"cairosvg": module}):
            image = render_answer_image(self.payload())
        self.assertEqual(image.filename, "carter-death-AbCdEf123456-7.png")
        self.assertEqual(image.alt_text, "Alex death position")

    def test_installed_renderer_produces_real_png(self):
        image = render_answer_image(self.payload())
        self.assertTrue(image.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(image.data), 8 * 1024 * 1024)

    def test_available_gif_is_preferred_over_static_map(self):
        payload = self.payload()
        gif = b"GIF89a" + b"focused-animation"
        payload["animation"] = {
            "status": "available", "mime_type": "image/gif", "encoding": "base64",
            "data": base64.b64encode(gif).decode(), "alt_text": "Five seconds to death",
        }
        image = render_answer_image(payload)
        self.assertEqual(image.data, gif)
        self.assertEqual(image.filename, "carter-death-AbCdEf123456-7.gif")
        self.assertEqual(image.alt_text, "Five seconds to death")

    def test_tool_context_never_contains_base64(self):
        payload = self.payload()
        payload["animation"] = {
            "status": "available", "mime_type": "image/gif", "encoding": "base64",
            "data": base64.b64encode(b"GIF89asecret").decode(), "frames": 26,
        }
        context = answer_without_image_bytes(payload)
        self.assertEqual(context["text"], "Alex died.")
        self.assertNotIn(payload["image"]["data"], json.dumps(context))
        self.assertNotIn(payload["animation"]["data"], json.dumps(context))
        self.assertEqual(context["animation"]["frames"], 26)

    def test_doctype_and_bad_base64_are_rejected(self):
        payload = self.payload()
        payload["image"]["data"] = "not base64!"
        with self.assertRaisesRegex(AnalysisClientError, "invalid_image"):
            render_answer_image(payload)

    def test_svg_external_resource_references_are_rejected(self):
        payload = self.payload()
        payload["image"]["data"] = base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        ).decode()
        with self.assertRaisesRegex(AnalysisClientError, "invalid_image"):
            render_answer_image(payload)
        payload = self.payload()
        payload["image"]["data"] = base64.b64encode(
            b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>',
        ).decode()
        with self.assertRaisesRegex(AnalysisClientError, "invalid_image"):
            render_answer_image(payload)
