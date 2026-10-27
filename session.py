"""One speaker, one request/response exchange, with quota-only opt-in fallback."""
import asyncio
import audioop
import logging
import queue
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, replace

from audio import PCMConverter, PCMPlayback
from discord_receive import PCMReceiveSink
from providers import ProviderError, create_provider

log = logging.getLogger("carter.session")
REPLAY_TTL_SECONDS = 600
MAX_REPLAY_BYTES = PCMPlayback.FRAME * 1000
FIXED_SPEECH_CACHE_ITEMS = 32
FIXED_SPEECH_CACHE_BYTES = 16 * 1024 * 1024
FIXED_SPEECH_MAX_CLIP_BYTES = PCMPlayback.FRAME * 500
FIXED_SPEECH_INSTRUCTIONS = (
    "You are Carter's text-to-speech renderer. Speak the user's text exactly as written, "
    "once. Do not answer it, paraphrase it, add commentary, or use tools."
)


@dataclass(frozen=True)
class SessionLimits:
    inactivity: float = 45
    max_seconds: float = 180
    rms_threshold: int = 300
    allow_fallback: bool = False


class FixedSpeechPlayer:
    """Render one fixed sentence through Carter's existing provider and PCM playback."""

    def __init__(self, settings, factory=create_provider):
        self.settings = replace(
            settings, instructions=FIXED_SPEECH_INSTRUCTIONS, tool_declarations=(),
        )
        self.factory = factory
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def _cached(self, text):
        pcm = self._cache.get(text)
        if pcm is not None:
            self._cache.move_to_end(text)
        return pcm

    def _remember(self, text, pcm):
        if not pcm or len(pcm) > FIXED_SPEECH_MAX_CLIP_BYTES:
            return
        previous = self._cache.pop(text, None)
        if previous is not None:
            self._cache_bytes -= len(previous)
        while self._cache and (
                len(self._cache) >= FIXED_SPEECH_CACHE_ITEMS
                or self._cache_bytes + len(pcm) > FIXED_SPEECH_CACHE_BYTES):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted)
        self._cache[text] = pcm
        self._cache_bytes += len(pcm)

    async def _wait_for_playback(self, voice, playback):
        deadline = time.monotonic() + 20
        while voice.is_connected() and not playback.is_idle():
            if time.monotonic() >= deadline:
                raise ProviderError("playback_timeout")
            await asyncio.sleep(0.05)
        if not voice.is_connected():
            raise ProviderError("voice_disconnected")

    async def _play_cached(self, voice, pcm):
        playback = PCMPlayback(max_frames=500)
        started_playback = False
        try:
            playback.push(pcm)
            playback.finish_turn()
            voice.play(playback)
            started_playback = True
            await self._wait_for_playback(voice, playback)
        finally:
            if started_playback:
                try:
                    voice.stop()
                except Exception:
                    log.warning("Fixed speech Discord playback cleanup failed")
            playback.cleanup()

    async def speak(self, voice, text):
        cached = self._cached(text)
        if cached is not None:
            log.info("Fixed speech audio cache hit")
            await self._play_cached(voice, cached)
            return
        log.info("Fixed speech audio cache miss")
        provider = self.factory("gemini", self.settings)
        playback = PCMPlayback(max_frames=500)
        started_playback = False
        received_audio = False
        rendered = bytearray()
        converter = PCMConverter(24000, 48000, target_channels=2)
        try:
            await provider.connect()
            voice.play(playback)
            started_playback = True
            await provider.send_text(text)
            async with asyncio.timeout(min(30.0, self.settings.receive_timeout)):
                async for event in provider.receive_audio():
                    if event.kind == "audio":
                        if event.sample_rate != converter.source_rate:
                            converter = PCMConverter(
                                event.sample_rate, 48000, target_channels=2,
                            )
                        if len(event.data) > 1024 * 1024:
                            raise BufferError("Oversized provider audio")
                        converted = converter.convert(event.data)
                        playback.push(converted)
                        rendered.extend(converted)
                        received_audio = True
                    elif event.kind in {"interrupted", "speech_started"}:
                        raise ProviderError("speech_interrupted")
                    elif event.kind == "turn_complete":
                        break
            if not received_audio:
                raise ProviderError("empty_audio")
            playback.finish_turn()
            self._remember(text, bytes(rendered))
            await self._wait_for_playback(voice, playback)
        finally:
            if started_playback:
                try:
                    voice.stop()
                except Exception:
                    log.warning("Fixed speech Discord playback cleanup failed")
            playback.cleanup()
            try:
                await provider.close()
            except Exception:
                log.warning("Fixed speech provider cleanup failed")


class VoiceSession:
    def __init__(self, settings, limits=SessionLimits(), factory=create_provider,
                 tool_handler=None, completion_handler=None):
        self.settings, self.limits, self.factory = settings, limits, factory
        self.tool_handler = tool_handler
        self.completion_handler = completion_handler
        self.task = None
        self.provider = None
        self.voice = None
        self.owner_id = None
        self.state = "inactive"
        self.provider_name = "gemini"
        self.last_error = None
        self.reason = "not_activated"
        self.input_seconds = self.output_seconds = 0.0
        self.usage = {}
        self.input = queue.Queue(maxsize=50)  # 1 second of Discord 20ms frames.
        self.playback = None
        self.accept_audio = False
        self.notify = None
        self.retry_count = 0
        self.receive_fault = None
        self.owns_receiver = True
        self.initial_audio = deque(maxlen=250)
        self.input_complete = False
        self.response_complete = None
        self.response_completed_at = None
        self.input_frames = 0
        self.speech_turns = 0
        self.provider_events = 0
        self.rejected_speaker_frames = 0
        self.tool_calls = 0
        self._logged_first_input = False
        self._logged_first_output = False
        self._response_pcm = bytearray()
        self.last_response_pcm = b""
        self.last_response_at = None

    @property
    def active(self):
        return self.state != "inactive"

    async def _notice(self, message):
        if self.notify:
            try:
                await asyncio.wait_for(self.notify(message), timeout=5)
            except Exception:
                log.warning("Session notification unavailable")

    def capture(self, user_id, pcm):
        # Called on the receive thread. Do not schedule unbounded loop callbacks.
        if not self.accept_audio or len(pcm) != 3840:
            return
        if user_id != self.owner_id:
            self.rejected_speaker_frames += 1
            return
        try:
            self.input.put_nowait((time.monotonic(), pcm))
            self.input_frames += 1
            if not self._logged_first_input:
                self._logged_first_input = True
                log.info("Discord microphone PCM accepted for active session")
        except queue.Full:
            self.receive_fault = "input_overflow"

    def _drain_input(self):
        self.initial_audio.clear()
        while True:
            try:
                self.input.get_nowait()
            except queue.Empty:
                break

    def _drain(self):
        self._drain_input()
        if self.playback:
            self.playback.clear()

    async def _connect(self, name):
        self.provider_name = name
        self.provider = self.factory(name, self.settings)
        remaining = self.limits.max_seconds - (time.monotonic() - self.started)
        await asyncio.wait_for(self.provider.connect(), timeout=max(0.001, min(
            self.settings.connect_timeout + 1, remaining,
        )))

    async def _close_provider(self):
        if self.provider:
            try:
                await asyncio.wait_for(self.provider.close(), timeout=5)
            except Exception:
                log.warning("Provider cleanup failed")
            self.provider = None

    async def _recover(self, error):
        log.warning(
            "AI transport recovery: code=%s provider=%s input=%.2fs output=%.2fs "
            "frames=%s turns=%s events=%s queue=%s receive_fault=%s",
            error.code, self.provider_name, self.input_seconds, self.output_seconds,
            self.input_frames, self.speech_turns, self.provider_events, self.input.qsize(),
            self.receive_fault or "none",
        )
        had_output = self._logged_first_output
        self.accept_audio = False
        self._drain()
        await self._close_provider()
        # Once Carter has emitted any answer audio, reconnecting could create a
        # second answer without another wake phrase. End this exchange instead.
        if had_output:
            raise error
        if error.quota and self.provider_name == "gemini" and self.limits.allow_fallback:
            self.state = "fallback"
            log.warning("Gemini quota reached; opt-in paid OpenAI fallback selected; context resets")
            await self._notice("Gemini quota reached. Switching to paid OpenAI fallback; conversation context resets.")
            await self._connect("openai")
        elif error.transient and self.retry_count < 1:
            self.retry_count += 1
            self.state = "reconnecting"
            await self._notice("AI connection interrupted. Retrying once; conversation context resets.")
            await asyncio.sleep(1)
            await self._connect(self.provider_name)
        else:
            raise error
        self._drain()
        self.input_complete = False
        self.response_completed_at = None
        if self.response_complete:
            self.response_complete.clear()
        self.accept_audio = True
        self.state = "listening"

    async def start(self, voice, owner_id, notify=None, *, external_capture=False):
        if self.active or (self.task and not self.task.done()):
            raise RuntimeError("A Carter session is already active")
        self.state = "connecting"
        self.reason, self.last_error = "active", None
        self.voice, self.owner_id, self.notify = voice, owner_id, notify
        self.owns_receiver = not external_capture
        self.input_seconds = self.output_seconds = 0.0
        self.usage, self.retry_count, self.receive_fault = {}, 0, None
        self.input_frames = self.speech_turns = self.provider_events = self.tool_calls = 0
        self.rejected_speaker_frames = 0
        self._logged_first_input = self._logged_first_output = False
        self._response_pcm.clear()
        self.input_complete = False
        self.response_complete = asyncio.Event()
        self.response_completed_at = None
        self.started = self.last_activity = time.monotonic()
        self.playback = PCMPlayback()
        try:
            try:
                await self._connect("gemini")
            except ProviderError as error:
                await self._recover(error)
            self.voice.play(self.playback)
            if self.owns_receiver:
                sink = PCMReceiveSink(self.capture, {owner_id}, on_error=self._receive_error)
                self.voice.listen(sink)
            self.accept_audio = True
            self.state = "listening"
            log.info(
                "AI session transport ready: provider=%s external_capture=%s",
                self.provider_name, external_capture,
            )
            self.task = asyncio.create_task(self._run(), name="carter-conversation")
        except BaseException as error:
            self.reason = "error"
            self.last_error = error.code if isinstance(error, ProviderError) else type(error).__name__
            await self._release()
            raise

    def _receive_error(self, code):
        self.receive_fault = code

    def seed_audio(self, frames, *, source_user_id=None):
        """Replay wake audio only when it belongs to the active session owner."""
        if self.accept_audio and source_user_id == self.owner_id:
            self.initial_audio.extend((captured, pcm) for captured, pcm in frames
                                      if len(pcm) == 3840)

    async def _finish_input_turn(self):
        if self.input_complete:
            return
        # Close capture before ending the provider turn. This makes the exchange
        # immutable: speech after the first request cannot become a follow-up.
        self.accept_audio = False
        self._drain_input()
        await self.provider.end_audio()
        self.input_complete = True
        log.info("AI input turn ended: input=%.2fs; awaiting one response", self.input_seconds)

    async def _send(self):
        converter = PCMConverter(48000, self.provider.input_rate, source_channels=2)
        last_voice = time.monotonic()
        streaming = False
        while True:
            if self.input_complete:
                await asyncio.sleep(0.05)
                continue
            try:
                seeded = bool(self.initial_audio)
                captured, pcm = self.initial_audio.popleft() if seeded else self.input.get_nowait()
            except queue.Empty:
                if streaming and time.monotonic() - last_voice > 0.7:
                    await self._finish_input_turn()
                    streaming = False
                await asyncio.sleep(0.01)
                continue
            if time.monotonic() - captured > (6 if seeded else 1):
                continue
            now = time.monotonic()
            voiced = audioop.rms(pcm, 2) > self.limits.rms_threshold
            if voiced:
                self.last_activity = last_voice = now
            elif streaming and now - last_voice > 0.7:
                await self._finish_input_turn()
                streaming = False
                continue
            if not streaming:
                if not voiced:
                    continue
                await self.provider.start_audio()
                streaming = True
                self.speech_turns += 1
                log.info("AI input turn started: turn=%s", self.speech_turns)
            converted = converter.convert(pcm)
            await self.provider.send_audio(converted)
            self.input_seconds += len(converted) / (2 * self.provider.input_rate)

    async def _receive(self):
        converter = PCMConverter(24000, 48000, target_channels=2)
        completed = False
        awaiting_tool_followup = False
        async for event in self.provider.receive_audio():
            self.provider_events += 1
            if self.provider_events == 1:
                log.info("First AI provider event received: kind=%s", event.kind)
            if event.kind == "usage":
                self.usage = event.usage or {}
                continue
            # Some providers send usage immediately after completion. Keep
            # consuming metadata briefly, but never queue a second response.
            if completed:
                continue
            if event.kind == "audio":
                # Audio after a function response is the actual user-facing
                # answer, not part of the tool-request turn.
                awaiting_tool_followup = False
                self.accept_audio = False
                self._drain_input()
                if event.sample_rate != converter.source_rate:
                    converter = PCMConverter(event.sample_rate, 48000, target_channels=2)
                if len(event.data) > 1024 * 1024:
                    raise BufferError("Oversized provider audio")
                converted = converter.convert(event.data)
                if len(self._response_pcm) + len(converted) > MAX_REPLAY_BYTES:
                    raise BufferError("Replay buffer full")
                self._response_pcm.extend(converted)
                self.playback.push(converted)
                self.output_seconds += len(event.data) / (2 * event.sample_rate)
                if not self._logged_first_output:
                    self._logged_first_output = True
                    log.info("First AI audio received and queued for Discord playback")
            elif event.kind in {"interrupted", "speech_started"}:
                self.playback.clear()
                converter.state = None
                if self._logged_first_output:
                    self.accept_audio = False
                    self._drain_input()
                    completed = True
                    self.response_completed_at = time.monotonic()
                    self.response_complete.set()
                    log.info("AI response interrupted after output; exchange will close")
            elif event.kind == "turn_complete":
                if awaiting_tool_followup:
                    # Gemini closes the model turn that requested a function
                    # after Carter returns the tool result. A second model turn
                    # contains the spoken answer. Closing the exchange here
                    # produces a silent output=0.00s response.
                    awaiting_tool_followup = False
                    log.info(
                        "AI tool-request turn completed; awaiting spoken follow-up",
                    )
                    continue
                self.playback.finish_turn()
                self.accept_audio = False
                self._drain_input()
                completed = True
                self.response_completed_at = time.monotonic()
                self.response_complete.set()
                if self._response_pcm:
                    self.last_response_pcm = bytes(self._response_pcm)
                    self.last_response_at = self.response_completed_at
                log.info("One AI response completed; waiting for Discord playback to drain")
            elif event.kind == "tool_call":
                if not self.tool_handler:
                    raise ProviderError("tool_handler_unavailable")
                if len(event.tool_calls) > 16:
                    raise ProviderError("tool_call_limit")
                results = []
                for index, call in enumerate(event.tool_calls):
                    if index >= 4:
                        response = {"error": "tool_call_limit"}
                    else:
                        self.tool_calls += 1
                        try:
                            response = await asyncio.wait_for(self.tool_handler(call), timeout=20)
                            if not isinstance(response, dict):
                                response = {"error": "invalid_tool_result"}
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            code = getattr(error, "code", type(error).__name__)
                            log.warning("AI analysis tool failed: code=%s", code)
                            response = {"error": str(code)}
                    results.append({
                        "id": call.get("id"), "name": call.get("name"), "response": response,
                    })
                await self.provider.send_tool_results(results)
                awaiting_tool_followup = True
        raise ProviderError("connection_closed", transient=True)

    async def _watch(self):
        while True:
            now = time.monotonic()
            if (self.response_complete and self.response_complete.is_set()
                    and self.playback.is_idle()
                    and now - self.response_completed_at >= 0.1):
                self.reason = "response_complete"
                log.info("One-to-one AI exchange completed; wake listening will rearm")
                return
            if now - self.started >= self.limits.max_seconds:
                self.reason = "session_limit"
                log.info("AI session watcher reached absolute limit")
                return
            if now - self.last_activity >= self.limits.inactivity:
                self.reason = "inactivity"
                log.info("AI session watcher reached inactivity limit")
                return
            if not self.voice.is_connected():
                self.reason = "voice_disconnected"
                return
            if self.receive_fault:
                raise ProviderError(self.receive_fault)
            if not self.voice.is_listening() or not self.voice.is_playing():
                raise ProviderError("voice_pipeline_stopped")
            await asyncio.sleep(0.25)

    async def _run(self):
        try:
            remaining = max(0.001, self.limits.max_seconds - (time.monotonic() - self.started))
            async with asyncio.timeout(remaining):
                while True:
                    workers = [asyncio.create_task(worker()) for worker in (self._send, self._receive, self._watch)]
                    failure = None
                    try:
                        done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    except ProviderError as error:
                        failure = error
                    finally:
                        for task in workers:
                            task.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)
                    if failure:
                        if time.monotonic() - self.started >= self.limits.max_seconds:
                            self.reason = "session_limit"
                            break
                        await self._recover(failure)
                    else:
                        break
        except TimeoutError:
            self.reason = "session_limit"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = error.code if isinstance(error, ProviderError) else type(error).__name__
            self.reason = "error"
            log.warning("AI session ended: %s", self.last_error)
        finally:
            log.info(
                "AI session summary: reason=%s provider=%s input=%.2fs output=%.2fs "
                "frames=%s turns=%s events=%s tools=%s last_error=%s",
                self.reason, self.provider_name, self.input_seconds, self.output_seconds,
                self.input_frames, self.speech_turns, self.provider_events, self.tool_calls,
                self.last_error or "none",
            )
            await self._release()
            if self.reason != "stopped" and self.completion_handler:
                try:
                    await self.completion_handler()
                except Exception as error:
                    log.warning(
                        "Automatic wake rearm failed: %s", type(error).__name__,
                    )
            if self.reason not in {"stopped", "response_complete"}:
                await self._notice(f"Carter AI stopped ({self.reason}).")

    async def _release(self):
        self.accept_audio = False
        try:
            if self.voice:
                try:
                    if self.owns_receiver:
                        self.voice.stop_listening()
                    self.voice.stop()
                    if self.owns_receiver:
                        await self.voice.wait_receive_cleanup()
                except Exception:
                    log.warning("Discord audio cleanup incomplete")
        finally:
            if self.playback:
                self.playback.cleanup()
            self._drain()
            await self._close_provider()
            self.owner_id = None
            self.state = "inactive"

    async def stop(self):
        self.reason = "stopped"
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.active:
            await self._release()
        self.task = None

    def replay_audio(self):
        """Return the recent generated response without retaining microphone audio."""
        if (not self.last_response_pcm or self.last_response_at is None
                or time.monotonic() - self.last_response_at > REPLAY_TTL_SECONDS):
            self.clear_replay()
            return b""
        return self.last_response_pcm

    def clear_replay(self):
        self._response_pcm.clear()
        self.last_response_pcm = b""
        self.last_response_at = None
