"""Small, credential-safe adapters for live PCM speech providers.

Gemini: https://ai.google.dev/gemini-api/docs/live-api/capabilities
OpenAI: https://developers.openai.com/api/docs/guides/realtime-conversations
No adapter reads environment files, selects a fallback, or records audio.
"""
import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("carter.providers")


@dataclass(frozen=True)
class ProviderSettings:
    gemini_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-live"
    gemini_text_model: str = "gemini-3.5-flash-lite"
    gemini_text_thinking_level: str = "minimal"
    gemini_text_timeout: float = 20.0
    openai_key: str = field(default="", repr=False)
    openai_model: str = "gpt-realtime-2.1"
    gemini_voice: str = "Kore"
    openai_voice: str = "marin"
    tool_declarations: tuple[dict, ...] = field(default_factory=tuple, repr=False)
    instructions: str = (
        "You are Carter, a helpful Discord voice assistant. "
        "Answer briefly in natural speech. When the raid-analysis tool is available, use it "
        "for raid-log facts and never invent unavailable results. Do not claim to execute "
        "actions that you did not perform. "
        "The selected user's microphone may also contain unrelated room or voice-channel "
        "conversation. Each activation is exactly one user request and one response. "
        "Never invite or wait for a follow-up; Carter closes the AI exchange after your "
        "first completed response. Never answer speech directed to another person. "
        "Respond only in English, regardless of the "
        "language used in the question or source data. Preserve player, guild, encounter, "
        "and ability names exactly rather than translating proper names."
    )
    connect_timeout: float = 15.0
    io_timeout: float = 10.0
    receive_timeout: float = 60.0


@dataclass(frozen=True)
class ProviderEvent:
    kind: Literal[
        "audio", "interrupted", "turn_complete", "speech_started", "usage", "tool_call",
    ]
    data: bytes = b""
    sample_rate: int = 24000
    usage: dict | None = None
    tool_calls: tuple[dict, ...] = ()


class ProviderError(Exception):
    """A safe public error; never contains an upstream message or credentials."""

    def __init__(self, code, *, quota=False, transient=False):
        super().__init__(code)
        self.code = code
        self.quota = quota
        self.transient = transient


async def generate_text(settings, prompt):
    """Run one bounded Gemini text turn, retrying one transient failure."""
    if not settings.gemini_key:
        raise ProviderError("missing_key")
    model = _safe_model_label(settings.gemini_text_model)
    for attempt in range(2):
        started = time.monotonic()
        log.info("Gemini text request started: model=%s attempt=%s", model, attempt + 1)
        try:
            answer = await _generate_text_once(settings, prompt)
        except ProviderError as error:
            retry = bool(error.transient and not attempt)
            log.warning(
                "Gemini text request failed: model=%s attempt=%s code=%s "
                "elapsed=%.2fs retry=%s",
                model, attempt + 1, error.code, time.monotonic() - started, retry,
            )
            if not retry:
                raise
            await asyncio.sleep(1)
        else:
            log.info(
                "Gemini text request completed: model=%s attempt=%s elapsed=%.2fs",
                model, attempt + 1, time.monotonic() - started,
            )
            return answer


def _safe_model_label(value):
    """Return a log-safe model label without ever including arbitrary env text."""
    value = str(value or '')
    return value if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', value) else 'configured_model'


async def _generate_text_once(settings, prompt):
    client = None
    try:
        genai, _ = _gemini_modules()
        client = genai.Client(api_key=settings.gemini_key)
        response = await asyncio.wait_for(client.aio.models.generate_content(
            model=settings.gemini_text_model,
            contents=prompt,
            config={
                "max_output_tokens": 1024,
                "system_instruction": settings.instructions,
                "thinking_config": {
                    "thinking_level": settings.gemini_text_thinking_level,
                },
            },
        ), settings.gemini_text_timeout)
        answer = response.text.strip() if isinstance(response.text, str) else ""
        if not answer:
            raise ProviderError("empty_response")
        return answer
    except BaseException as error:
        if isinstance(error, asyncio.CancelledError):
            raise
        raise _provider_error(error) from None
    finally:
        if client is not None:
            await _quiet_cleanup((client.aio.aclose, settings.io_timeout))


class SpeechProvider(Protocol):
    name: str
    model: str
    input_rate: int

    async def connect(self): ...
    async def start_audio(self): ...
    async def send_audio(self, pcm: bytes): ...
    def receive_audio(self) -> AsyncIterator[ProviderEvent]: ...
    async def end_audio(self): ...
    async def send_tool_results(self, results): ...
    async def interrupt(self): ...
    async def close(self): ...


def _provider_error(error):
    if isinstance(error, ProviderError):
        return error
    response = getattr(error, "response", None)
    code = (getattr(error, "code", None) or getattr(error, "status", None)
            or getattr(response, "status_code", None) or getattr(response, "status", None))
    # Inspect only for classification; the provider's original text never escapes.
    status = str(getattr(error, "status", "")).upper()
    message = str(error).upper()
    quota_close = code == 1008 and any(marker in message for marker in (
        "QUOTA EXCEEDED", "EXCEEDED YOUR CURRENT QUOTA", "RESOURCE HAS BEEN EXHAUSTED",
    ))
    if code == 429 or status == "RESOURCE_EXHAUSTED" or "RESOURCE_EXHAUSTED" in message or quota_close:
        return ProviderError("quota_exhausted", quota=True)
    if code in (401, 403):
        return ProviderError("authentication_failed")
    if code in (400, 404, 1007, 1008):
        return ProviderError("configuration_rejected")
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return ProviderError("provider_timeout", transient=True)
    if code in (500, 502, 503, 504, 1006, 1011, 1012, 1013):
        return ProviderError("provider_unavailable", transient=True)
    if isinstance(error, (aiohttp.ClientConnectionError, OSError)):
        return ProviderError("connection_lost", transient=True)
    return ProviderError("provider_failed")


async def _quiet_cleanup(*operations):
    """Complete bounded cleanup even when the caller is being cancelled."""
    async def finish():
        for operation, timeout in operations:
            try:
                await asyncio.wait_for(operation(), timeout)
            except Exception:
                pass

    task = asyncio.create_task(finish())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _validate_pcm(pcm):
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise ProviderError("invalid_pcm")


def _gemini_modules():
    from google import genai
    from google.genai import types
    return genai, types


class GeminiProvider:
    name = "gemini"
    input_rate = 16000

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.gemini_model
        self._client = self._context = self._session = self._types = None
        self._received_server_message = False

    async def connect(self):
        if self._client is not None:
            raise ProviderError("already_connected")
        if not self.settings.gemini_key:
            raise ProviderError("missing_key")
        try:
            genai, self._types = _gemini_modules()
            self._client = genai.Client(api_key=self.settings.gemini_key)
            config = {
                "response_modalities": ["AUDIO"],
                "max_output_tokens": 512,
                "system_instruction": self.settings.instructions,
                "speech_config": {
                    "language_code": "en",
                    "voice_config": {"prebuilt_voice_config": {
                        "voice_name": self.settings.gemini_voice,
                    }},
                },
                "realtime_input_config": {
                    "automatic_activity_detection": {"disabled": True},
                },
            }
            if self.settings.tool_declarations:
                config["tools"] = [{
                    "function_declarations": list(self.settings.tool_declarations),
                }]
            self._context = self._client.aio.live.connect(
                model=self.model,
                config=config,
            )
            self._session = await asyncio.wait_for(
                self._context.__aenter__(), self.settings.connect_timeout,
            )
            log.info("Gemini Live WebSocket connected")
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise _provider_error(error) from None

    def _connected(self):
        if self._session is None:
            raise ProviderError("not_connected")
        return self._session

    async def _send(self, **kwargs):
        try:
            await asyncio.wait_for(
                self._connected().send_realtime_input(**kwargs), self.settings.io_timeout,
            )
        except Exception as error:
            raise _provider_error(error) from None

    async def send_audio(self, pcm):
        _validate_pcm(pcm)
        self._connected()
        await self._send(audio=self._types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

    async def start_audio(self):
        await self._send(activity_start=self._types.ActivityStart())

    async def end_audio(self):
        await self._send(activity_end=self._types.ActivityEnd())

    async def send_tool_results(self, results):
        responses = []
        for result in results:
            if not isinstance(result, dict):
                raise ProviderError("invalid_tool_result")
            responses.append(self._types.FunctionResponse(
                id=str(result.get("id") or ""),
                name=str(result.get("name") or ""),
                response=result.get("response") if isinstance(result.get("response"), dict)
                else {"error": "invalid_tool_result"},
            ))
        if not responses:
            raise ProviderError("invalid_tool_result")
        try:
            await asyncio.wait_for(
                self._connected().send_tool_response(function_responses=responses),
                self.settings.io_timeout,
            )
        except Exception as error:
            raise _provider_error(error) from None

    async def receive_audio(self):
        session = self._connected()
        try:
            while self._session is session:
                turn = session.receive().__aiter__()
                received = False
                while self._session is session:
                    try:
                        response = await asyncio.wait_for(
                            anext(turn), self.settings.receive_timeout,
                        )
                    except (TimeoutError, asyncio.TimeoutError):
                        log.warning("Gemini Live receive timeout before next server message")
                        raise ProviderError("provider_timeout", transient=True) from None
                    except StopAsyncIteration:
                        break
                    received = True
                    if not self._received_server_message:
                        self._received_server_message = True
                        log.info("Gemini Live first server message received")
                    if response.go_away:
                        raise ProviderError("session_expiring", transient=True)
                    tool_call = response.tool_call
                    if tool_call and tool_call.function_calls:
                        calls = []
                        for call in tool_call.function_calls:
                            arguments = dict(call.args) if call.args is not None else {}
                            calls.append({
                                "id": str(call.id or ""),
                                "name": str(call.name or ""),
                                "arguments": arguments,
                            })
                        yield ProviderEvent("tool_call", tool_calls=tuple(calls))
                    content = response.server_content
                    if content:
                        if content.interrupted:
                            yield ProviderEvent("interrupted")
                        # An interrupted message can contain audio that must be discarded.
                        elif content.model_turn:
                            for part in content.model_turn.parts or []:
                                blob = part.inline_data
                                if blob and isinstance(blob.data, bytes) and blob.data:
                                    if not blob.mime_type or blob.mime_type.startswith("audio/pcm"):
                                        yield ProviderEvent("audio", blob.data)
                        if content.turn_complete:
                            yield ProviderEvent("turn_complete")
                    metadata = response.usage_metadata
                    if metadata:
                        names = ("total_token_count", "prompt_token_count", "response_token_count",
                                 "cached_content_token_count", "thoughts_token_count")
                        usage = {name: getattr(metadata, name) for name in names
                                 if isinstance(getattr(metadata, name, None), int)}
                        if usage:
                            yield ProviderEvent("usage", usage=usage)
                if not received and self._session is session:
                    raise ProviderError("connection_closed", transient=True)
        except Exception as error:
            raise _provider_error(error) from None

    async def interrupt(self):
        # Automatic VAD handles barge-in. A standalone hard cancel closes the
        # session because the Live API has no separate documented cancel event.
        await self.close()

    async def close(self):
        context, client = self._context, self._client
        self._session = self._context = self._client = None
        operations = []
        if context is not None:
            operations.append((lambda: context.__aexit__(None, None, None), self.settings.io_timeout))
        if client is not None:
            operations.append((client.aio.aclose, self.settings.io_timeout))
        if operations:
            await _quiet_cleanup(*operations)


class OpenAIProvider:
    name = "openai"
    input_rate = 24000

    def __init__(self, settings):
        self.settings = settings
        self.model = settings.openai_model
        self._http = self._ws = None
        self._has_audio = self._responding = False

    async def connect(self):
        if self._http is not None:
            raise ProviderError("already_connected")
        if not self.settings.openai_key:
            raise ProviderError("missing_key")
        try:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
            url = "wss://api.openai.com/v1/realtime?" + urlencode({"model": self.model})
            self._ws = await asyncio.wait_for(self._http.ws_connect(
                url, headers={"Authorization": "Bearer " + self.settings.openai_key},
                heartbeat=20, max_msg_size=1024 * 1024,
            ), self.settings.connect_timeout)
            await self._expect("session.created")
            await self._send({
                "type": "session.update",
                "session": {
                    "type": "realtime", "output_modalities": ["audio"],
                    "instructions": self.settings.instructions,
                    "max_output_tokens": 512,
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "turn_detection": {
                                "type": "server_vad", "silence_duration_ms": 500,
                                "create_response": True, "interrupt_response": True,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "voice": self.settings.openai_voice,
                        },
                    },
                },
            })
            await self._expect("session.updated")
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise _provider_error(error) from None

    async def _read(self, timeout):
        if self._ws is None:
            raise ProviderError("not_connected")
        message = await asyncio.wait_for(self._ws.receive(), timeout)
        if message.type != aiohttp.WSMsgType.TEXT:
            raise ProviderError("connection_closed", transient=True)
        event = message.json()
        if not isinstance(event, dict):
            raise ProviderError("invalid_provider_event")
        if event.get("type") == "error":
            detail = event.get("error") or {}
            code = detail.get("code")
            if code == "response_cancel_not_active":
                return {}
            if code in ("rate_limit_exceeded", "insufficient_quota", "quota_exceeded"):
                raise ProviderError("quota_exhausted", quota=True)
            raise ProviderError("request_rejected")
        return event

    async def _expect(self, event_type):
        # Bound the whole handshake, including unexpected setup notifications.
        async def wait():
            while True:
                if (await self._read(self.settings.connect_timeout)).get("type") == event_type:
                    return
        await asyncio.wait_for(wait(), self.settings.connect_timeout)

    async def _send(self, event):
        if self._ws is None:
            raise ProviderError("not_connected")
        try:
            await asyncio.wait_for(self._ws.send_json(event), self.settings.io_timeout)
        except Exception as error:
            raise _provider_error(error) from None

    async def send_audio(self, pcm):
        _validate_pcm(pcm)
        await self._send({"type": "input_audio_buffer.append",
                          "audio": base64.b64encode(pcm).decode("ascii")})
        self._has_audio = True

    async def start_audio(self):
        # OpenAI's configured server VAD starts automatically on appended PCM.
        return None

    async def end_audio(self):
        if self._has_audio:
            # Discord stops sending packets at silence. Feed enough silence for
            # server VAD to commit; manual commit would race automatic commits.
            silence = bytes(self.input_rate * 2 * 600 // 1000)
            await self._send({"type": "input_audio_buffer.append",
                              "audio": base64.b64encode(silence).decode("ascii")})
            self._has_audio = False

    async def send_tool_results(self, results):
        # Carter advertises analysis tools only on the primary Gemini session.
        raise ProviderError("tool_unsupported")

    async def receive_audio(self):
        try:
            while self._ws is not None:
                event = await self._read(self.settings.receive_timeout)
                kind = event.get("type")
                if kind == "response.created":
                    self._responding = True
                elif kind == "response.output_audio.delta":
                    yield ProviderEvent("audio", base64.b64decode(event["delta"], validate=True))
                elif kind == "input_audio_buffer.speech_started":
                    yield ProviderEvent("speech_started")
                elif kind == "response.done":
                    self._responding = False
                    response = event.get("response") or {}
                    if response.get("status") == "failed":
                        error = (response.get("status_details") or {}).get("error") or {}
                        quota = error.get("code") in ("rate_limit_exceeded", "insufficient_quota")
                        raise ProviderError("quota_exhausted" if quota else "response_failed", quota=quota)
                    if response.get("status") == "cancelled":
                        yield ProviderEvent("interrupted")
                    else:
                        yield ProviderEvent("turn_complete")
                    usage = response.get("usage")
                    if isinstance(usage, dict):
                        yield ProviderEvent("usage", usage={key: value for key, value in usage.items()
                                                           if isinstance(value, int)})
        except Exception as error:
            raise _provider_error(error) from None

    async def interrupt(self):
        if self._responding:
            await self._send({"type": "response.cancel"})
            self._responding = False

    async def close(self):
        ws, http = self._ws, self._http
        self._ws = self._http = None
        self._has_audio = self._responding = False
        operations = []
        if ws is not None:
            operations.append((ws.close, self.settings.io_timeout))
        if http is not None:
            operations.append((http.close, self.settings.io_timeout))
        if operations:
            await _quiet_cleanup(*operations)


def create_provider(name, settings):
    factories = {"gemini": GeminiProvider, "openai": OpenAIProvider}
    try:
        factory = factories[name]
    except KeyError:
        raise ValueError("Provider must be gemini or openai") from None
    return factory(settings)
