"""Provider contract tests; fake transports only, no keys or network calls."""
import asyncio
import base64
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from google.genai import types

from providers import (
    GeminiProvider, OpenAIProvider, ProviderError, ProviderSettings,
    _provider_error, create_provider, generate_text,
)


async def messages(*values):
    for value in values:
        yield value


def websocket_message(value):
    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: value)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = ProviderSettings(gemini_key="private-gemini", openai_key="private-openai")

    def gemini_transport(self):
        session = SimpleNamespace(
            send_realtime_input=AsyncMock(), send_tool_response=AsyncMock(), receive=Mock(),
        )
        context = SimpleNamespace(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())
        client = SimpleNamespace(aio=SimpleNamespace(
            live=SimpleNamespace(connect=Mock(return_value=context)), aclose=AsyncMock(),
        ))
        modules = (SimpleNamespace(Client=Mock(return_value=client)), types)
        return session, context, client, modules

    def openai_transport(self):
        ws = SimpleNamespace(
            receive=AsyncMock(side_effect=[
                websocket_message({"type": "session.created"}),
                websocket_message({"type": "session.updated"}),
            ]),
            send_json=AsyncMock(), close=AsyncMock(),
        )
        http = SimpleNamespace(ws_connect=AsyncMock(return_value=ws), close=AsyncMock())
        return ws, http

    def test_factory_never_connects_or_falls_back(self):
        with patch("providers.aiohttp.ClientSession") as http, patch("providers._gemini_modules") as sdk:
            self.assertIsInstance(create_provider("gemini", self.settings), GeminiProvider)
            self.assertIsInstance(create_provider("openai", self.settings), OpenAIProvider)
            with self.assertRaises(ValueError):
                create_provider("other", self.settings)
            http.assert_not_called()
            sdk.assert_not_called()
        self.assertNotIn("private-gemini", repr(self.settings))
        self.assertNotIn("private-openai", repr(self.settings))

    async def test_missing_credentials_never_opens_transport(self):
        for name in ("gemini", "openai"):
            provider = create_provider(name, ProviderSettings())
            with self.assertRaisesRegex(ProviderError, "missing_key"):
                await provider.connect()
            await provider.close()

    def test_upstream_errors_are_sanitized_and_quota_is_specific(self):
        class UpstreamError(Exception):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code
        for code, message, quota in (
            (429, "private-key rate limit", True),
            (1008, "RESOURCE_EXHAUSTED: private-key", True),
            (1008, "Quota exceeded for quota metric: private-key", True),
            (403, "private-key refused", False),
            (404, "private-key missing model", False),
            (500, "private-key unavailable", False),
        ):
            result = _provider_error(UpstreamError(code, message))
            self.assertEqual(result.quota, quota)
            self.assertNotIn("private-key", str(result))
        self.assertTrue(_provider_error(TimeoutError("private-key")).transient)
        # The SDK's WebSocket handshake can expose HTTP errors through response.
        handshake = RuntimeError("private-key")
        handshake.response = SimpleNamespace(status_code=429)
        self.assertTrue(_provider_error(handshake).quota)

    async def test_gemini_setup_pcm_flush_and_idempotent_close(self):
        session, context, client, modules = self.gemini_transport()
        provider = GeminiProvider(self.settings)
        with patch("providers._gemini_modules", return_value=modules):
            await provider.connect()
        arguments = client.aio.live.connect.call_args.kwargs
        config = types.LiveConnectConfig.model_validate(arguments["config"])
        self.assertEqual(arguments["model"], self.settings.gemini_model)
        self.assertEqual(config.max_output_tokens, 512)
        self.assertEqual(config.speech_config.language_code, "en")
        self.assertTrue(config.realtime_input_config.automatic_activity_detection.disabled)
        await provider.start_audio()
        self.assertIsNotNone(session.send_realtime_input.call_args.kwargs["activity_start"])
        await provider.send_audio(b"\x01\x00")
        blob = session.send_realtime_input.call_args.kwargs["audio"]
        self.assertEqual(blob.mime_type, "audio/pcm;rate=16000")
        self.assertEqual(blob.data, b"\x01\x00")
        await provider.end_audio()
        self.assertIsNotNone(session.send_realtime_input.call_args.kwargs["activity_end"])
        with self.assertRaisesRegex(ProviderError, "invalid_pcm"):
            await provider.send_audio(b"\x01")
        await provider.close()
        await provider.close()
        context.__aexit__.assert_awaited_once()
        client.aio.aclose.assert_awaited_once()

    async def test_gemini_text_turn_returns_text_and_cleans_up(self):
        response = SimpleNamespace(text="Carter reply")
        client = SimpleNamespace(aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=AsyncMock(return_value=response)),
            aclose=AsyncMock(),
        ))
        modules = (SimpleNamespace(Client=Mock(return_value=client)), types)
        with patch("providers._gemini_modules", return_value=modules):
            answer = await generate_text(self.settings, "private prompt")
        self.assertEqual(answer, "Carter reply")
        call = client.aio.models.generate_content.await_args.kwargs
        self.assertEqual(call["model"], self.settings.gemini_text_model)
        self.assertEqual(call["contents"], "private prompt")
        self.assertEqual(call["config"]["system_instruction"], self.settings.instructions)
        client.aio.aclose.assert_awaited_once()

    async def test_gemini_text_empty_response_is_sanitized(self):
        client = SimpleNamespace(aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=AsyncMock(
                return_value=SimpleNamespace(text=None),
            )),
            aclose=AsyncMock(),
        ))
        modules = (SimpleNamespace(Client=Mock(return_value=client)), types)
        with patch("providers._gemini_modules", return_value=modules):
            with self.assertRaisesRegex(ProviderError, "empty_response"):
                await generate_text(self.settings, "prompt")

    async def test_gemini_text_retries_one_transient_failure(self):
        class TemporaryError(Exception):
            code = 503

        generate = AsyncMock(side_effect=[
            TemporaryError("private upstream detail"),
            SimpleNamespace(text="Recovered reply"),
        ])
        client = SimpleNamespace(aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=generate), aclose=AsyncMock(),
        ))
        modules = (SimpleNamespace(Client=Mock(return_value=client)), types)
        with patch("providers._gemini_modules", return_value=modules), \
                patch("providers.asyncio.sleep", AsyncMock()) as sleep:
            answer = await generate_text(self.settings, "prompt")
        self.assertEqual(answer, "Recovered reply")
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(client.aio.aclose.await_count, 2)
        sleep.assert_awaited_once_with(1)

    async def test_gemini_receives_multiple_turns_and_interruption(self):
        session, _, _, modules = self.gemini_transport()
        session.receive.side_effect = [
            messages(types.LiveServerMessage(server_content=types.LiveServerContent(
                model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(
                    data=b"\x01\x00", mime_type="audio/pcm;rate=24000",
                ))]), turn_complete=True,
            ))),
            messages(types.LiveServerMessage(
                server_content=types.LiveServerContent(interrupted=True),
                usage_metadata=types.UsageMetadata(total_token_count=10),
            )),
        ]
        provider = GeminiProvider(self.settings)
        with patch("providers._gemini_modules", return_value=modules):
            await provider.connect()
        events = provider.receive_audio()
        try:
            audio = await anext(events)
            self.assertEqual((audio.kind, audio.data, audio.sample_rate), ("audio", b"\x01\x00", 24000))
            self.assertEqual((await anext(events)).kind, "turn_complete")
            self.assertEqual((await anext(events)).kind, "interrupted")
            self.assertEqual((await anext(events)).usage["total_token_count"], 10)
        finally:
            await events.aclose()
            await provider.interrupt()
        self.assertIsNone(provider._session)

    async def test_gemini_advertises_executes_and_answers_function_calls(self):
        session, _, client, modules = self.gemini_transport()
        declaration = {
            "name": "query_raid_analysis",
            "description": "Query prepared raid evidence.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"intent": {"type": "STRING"}},
                "required": ["intent"],
            },
        }
        settings = replace(self.settings, tool_declarations=(declaration,))
        session.receive.return_value = messages(types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(
                id="call-1", name="query_raid_analysis", args={"intent": "summary"},
            )]),
        ))
        provider = GeminiProvider(settings)
        with patch("providers._gemini_modules", return_value=modules):
            await provider.connect()
        config = types.LiveConnectConfig.model_validate(
            client.aio.live.connect.call_args.kwargs["config"],
        )
        self.assertEqual(
            config.tools[0].function_declarations[0].name, "query_raid_analysis",
        )
        events = provider.receive_audio()
        try:
            event = await anext(events)
            self.assertEqual(event.kind, "tool_call")
            self.assertEqual(event.tool_calls[0], {
                "id": "call-1", "name": "query_raid_analysis",
                "arguments": {"intent": "summary"},
            })
            await provider.send_tool_results([{
                "id": "call-1", "name": "query_raid_analysis",
                "response": {"summary": {"name": "Example"}},
            }])
        finally:
            await events.aclose()
            await provider.close()
        response = session.send_tool_response.await_args.kwargs["function_responses"][0]
        self.assertEqual(response.id, "call-1")
        self.assertEqual(response.name, "query_raid_analysis")
        self.assertEqual(response.response["summary"]["name"], "Example")

    async def test_gemini_failed_setup_cleans_resources_without_secrets(self):
        _, context, client, modules = self.gemini_transport()
        context.__aenter__.side_effect = RuntimeError("private-gemini")
        provider = GeminiProvider(self.settings)
        with patch("providers._gemini_modules", return_value=modules):
            with self.assertRaisesRegex(ProviderError, "provider_failed") as caught:
                await provider.connect()
        self.assertNotIn("private-gemini", str(caught.exception))
        client.aio.aclose.assert_awaited_once()
        self.assertIsNone(provider._session)

    async def test_gemini_go_away_requests_explicit_reconnect(self):
        session, _, _, modules = self.gemini_transport()
        session.receive.return_value = messages(types.LiveServerMessage(go_away=types.LiveServerGoAway()))
        provider = GeminiProvider(self.settings)
        with patch("providers._gemini_modules", return_value=modules):
            await provider.connect()
        with self.assertRaisesRegex(ProviderError, "session_expiring") as caught:
            await anext(provider.receive_audio())
        self.assertTrue(caught.exception.transient)
        await provider.close()

    async def test_openai_setup_pcm_serialization_and_silence_flush(self):
        ws, http = self.openai_transport()
        provider = OpenAIProvider(self.settings)
        with patch("providers.aiohttp.ClientSession", return_value=http):
            await provider.connect()
        setup = ws.send_json.call_args.args[0]
        self.assertEqual(setup["type"], "session.update")
        self.assertEqual(setup["session"]["audio"]["input"]["format"]["rate"], 24000)
        self.assertTrue(setup["session"]["audio"]["input"]["turn_detection"]["interrupt_response"])
        await provider.send_audio(b"\x01\x00")
        self.assertEqual(base64.b64decode(ws.send_json.call_args.args[0]["audio"]), b"\x01\x00")
        await provider.start_audio()
        await provider.end_audio()
        self.assertEqual(len(base64.b64decode(ws.send_json.call_args.args[0]["audio"])), 28800)
        calls = ws.send_json.await_count
        await provider.end_audio()
        self.assertEqual(ws.send_json.await_count, calls)
        await provider.close()
        await provider.close()
        ws.close.assert_awaited_once()
        http.close.assert_awaited_once()

    async def test_openai_audio_barge_in_completion_and_usage(self):
        ws, http = self.openai_transport()
        provider = OpenAIProvider(self.settings)
        with patch("providers.aiohttp.ClientSession", return_value=http):
            await provider.connect()
        ws.receive.side_effect = [websocket_message(value) for value in [
            {"type": "response.created"},
            {"type": "response.output_audio.delta", "delta": "AQA="},
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.done", "response": {"status": "cancelled"}},
            {"type": "response.done", "response": {
                "status": "completed", "usage": {"total_tokens": 5, "unsafe": "private-key"},
            }},
        ]]
        events = provider.receive_audio()
        try:
            self.assertEqual((await anext(events)).data, b"\x01\x00")
            await provider.interrupt()
            ws.send_json.assert_awaited_with({"type": "response.cancel"})
            self.assertEqual((await anext(events)).kind, "speech_started")
            self.assertEqual((await anext(events)).kind, "interrupted")
            self.assertEqual((await anext(events)).kind, "turn_complete")
            self.assertEqual((await anext(events)).usage, {"total_tokens": 5})
        finally:
            await events.aclose()
            await provider.close()

    async def test_openai_quota_error_and_failed_handshake_cleanup(self):
        ws, http = self.openai_transport()
        ws.receive.side_effect = [websocket_message({"type": "error", "error": {
            "code": "rate_limit_exceeded", "message": "private-openai",
        }})]
        provider = OpenAIProvider(self.settings)
        with patch("providers.aiohttp.ClientSession", return_value=http):
            with self.assertRaisesRegex(ProviderError, "quota_exhausted") as caught:
                await provider.connect()
        self.assertTrue(caught.exception.quota)
        self.assertNotIn("private-openai", str(caught.exception))
        ws.close.assert_awaited_once()
        http.close.assert_awaited_once()

    async def test_receive_timeout_is_transient_and_cleanup_finishes_on_cancellation(self):
        ws, http = self.openai_transport()
        provider = OpenAIProvider(replace(self.settings, receive_timeout=0.01))
        with patch("providers.aiohttp.ClientSession", return_value=http):
            await provider.connect()
        ws.receive.side_effect = None
        async def stalled_read():
            await asyncio.Event().wait()
        ws.receive.side_effect = stalled_read
        with self.assertRaisesRegex(ProviderError, "provider_timeout") as caught:
            await anext(provider.receive_audio())
        self.assertTrue(caught.exception.transient)
        started, finish = asyncio.Event(), asyncio.Event()
        async def closing():
            started.set()
            await finish.wait()
        ws.close.side_effect = closing
        task = asyncio.create_task(provider.close())
        await started.wait()
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        http.close.assert_awaited_once()
        self.assertIsNone(provider._ws)


if __name__ == "__main__":
    unittest.main()
