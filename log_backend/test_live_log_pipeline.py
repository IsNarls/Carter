import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

if __package__:
    from . import live_log_pipeline as pipeline
else:
    import live_log_pipeline as pipeline


_atomic_json = pipeline._atomic_json
_complete_batch = pipeline._complete_batch
_endpoint = pipeline._endpoint
_identity = pipeline._identity
_initial_cursor = pipeline._initial_cursor
_load_state = pipeline._load_state
_state_payload = pipeline._state_payload
_validate_transport = pipeline._validate_transport


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class LiveLogPipelineTests(unittest.TestCase):
    def test_complete_batch_does_not_advance_over_partial_line(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            path = Path(temporary) / 'combat.txt'
            path.write_bytes(b'one\ntwo\npartial')
            with path.open('rb') as stream:
                self.assertEqual(_complete_batch(stream), b'one\ntwo\n')
                self.assertEqual(stream.tell(), 8)
                self.assertEqual(_complete_batch(stream), b'')
                self.assertEqual(stream.tell(), 8)

    def test_cursor_state_round_trip_contains_no_token(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            path = Path(temporary) / 'state.json'
            expected = {
                'version': 2,
                'file_id': 'physical-file-123',
                'stream_id': 'logical-stream-123',
                'offset': 42,
            }
            _atomic_json(path, expected)
            self.assertEqual(_load_state(path), expected)
            self.assertNotIn('token', path.read_text(encoding='utf-8').lower())

    def test_restart_resumes_same_physical_file_and_logical_stream(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            log = Path(temporary) / 'combat.txt'
            log.write_bytes(b'one\ntwo\n')
            file_id, stream_id, _ = _initial_cursor(log, None)
            state = _state_payload(file_id, stream_id, 4)

            resumed_file, resumed_stream, offset = _initial_cursor(log, state)

            self.assertEqual(resumed_file, file_id)
            self.assertEqual(resumed_stream, stream_id)
            self.assertEqual(offset, 4)

    def test_same_file_truncation_starts_new_stream_at_zero(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            log = Path(temporary) / 'combat.txt'
            log.write_bytes(b'one\ntwo\n')
            file_id = _identity(log)
            old_stream = 'a' * 32
            state = _state_payload(file_id, old_stream, log.stat().st_size)
            with log.open('wb') as output:
                output.write(b'new\n')
            self.assertEqual(_identity(log), file_id)

            resumed_file, resumed_stream, offset = _initial_cursor(log, state)

            self.assertEqual(resumed_file, file_id)
            self.assertNotEqual(resumed_stream, old_stream)
            self.assertEqual(offset, 0)

    def test_replacement_starts_new_stream_at_zero(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            root = Path(temporary)
            log = root / 'combat.txt'
            replacement = root / 'replacement.txt'
            log.write_bytes(b'old-data\n')
            old_file = _identity(log)
            old_stream = 'b' * 32
            state = _state_payload(old_file, old_stream, log.stat().st_size)
            replacement.write_bytes(b'new-data\n')
            os.replace(replacement, log)

            new_file, new_stream, offset = _initial_cursor(log, state)

            self.assertNotEqual(new_file, old_file)
            self.assertNotEqual(new_stream, old_stream)
            self.assertEqual(offset, 0)

    def test_version_one_state_migrates_without_replaying(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            log = Path(temporary) / 'combat.txt'
            log.write_bytes(b'one\ntwo\n')
            file_id = _identity(log)
            state_path = Path(temporary) / 'state.json'
            _atomic_json(state_path, {'version': 1, 'stream_id': file_id, 'offset': 4})

            state = _load_state(state_path)
            resumed_file, resumed_stream, offset = _initial_cursor(log, state)

            self.assertEqual((resumed_file, resumed_stream, offset), (file_id, file_id, 4))

    def test_origin_and_api_base_resolve_to_same_endpoint(self):
        expected = 'https://carter.example/api/v1/live/ingest'
        self.assertEqual(_endpoint('https://carter.example'), expected)
        self.assertEqual(_endpoint('https://carter.example/'), expected)
        self.assertEqual(_endpoint('https://carter.example/api/v1'), expected)
        self.assertEqual(_endpoint(expected), expected)

    def test_endpoint_rejects_ambiguous_or_credentialed_urls(self):
        for value in (
                'https://carter.example/other',
                'https://carter.example/api/v1?debug=1',
                'https://user:password@carter.example',
                'carter.example'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _endpoint(value)

    def test_plain_http_requires_loopback_or_explicit_opt_in(self):
        expected = 'http://127.0.0.2:8765/api/v1/live/ingest'
        self.assertEqual(_validate_transport('http://127.0.0.2:8765', False), expected)
        with self.assertRaises(ValueError):
            _validate_transport('http://carter.example', False)
        self.assertEqual(
            _validate_transport('http://carter.example/api/v1', True),
            'http://carter.example/api/v1/live/ingest',
        )

    def test_redirect_handler_refuses_every_redirect_status(self):
        handler = pipeline._NoRedirectHandler()
        request = Request('https://carter.example/api/v1/live/ingest')
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertIsNone(handler.redirect_request(
                    request, None, status, 'redirect', {}, 'https://other.example/steal',
                ))

    def test_send_batch_uses_normalized_endpoint_and_live_headers(self):
        response = FakeResponse(b'{"accepted":1,"next_offset":5}')
        with patch.object(pipeline._HTTP_OPENER, 'open', return_value=response) as opened:
            status, payload = pipeline.send_batch(
                'https://carter.example/api/v1', '123456789', 't' * 40,
                'stream-123', 0, b'line\n', timeout=7,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload['next_offset'], 5)
        request = opened.call_args.args[0]
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, 'https://carter.example/api/v1/live/ingest')
        self.assertEqual(headers['x-carter-guild-id'], '123456789')
        self.assertEqual(headers['x-carter-live-token'], 't' * 40)
        self.assertEqual(headers['x-carter-live-stream-id'], 'stream-123')
        self.assertEqual(headers['x-carter-live-offset'], '0')
        opened.assert_called_once_with(request, timeout=7)


if __name__ == '__main__':
    unittest.main()
