"""One speaker, one bounded conversation, with quota-only opt-in fallback."""
import asyncio
import audioop
import logging
import queue
import time
from collections import deque
from dataclasses import dataclass

from audio import PCMConverter, PCMPlayback
from discord_receive import PCMReceiveSink
from providers import ProviderError, create_provider

log = logging.getLogger("carter.session")


@dataclass(frozen=True)
class SessionLimits:
    inactivity: float = 45
    max_seconds: float = 180
    rms_threshold: int = 300
    allow_fallback: bool = False


class VoiceSession:
    def __init__(self, settings, limits=SessionLimits(), factory=create_provider):
        self.settings, self.limits, self.factory = settings, limits, factory
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
        if not self.accept_audio or user_id != self.owner_id or len(pcm) != 3840:
            return
        try:
            self.input.put_nowait((time.monotonic(), pcm))
        except queue.Full:
            self.receive_fault = "input_overflow"

    def _drain(self):
        self.initial_audio.clear()
        while True:
            try:
                self.input.get_nowait()
            except queue.Empty:
                break
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
        self.accept_audio = False
        self._drain()
        await self._close_provider()
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
            self.task = asyncio.create_task(self._run(), name="carter-conversation")
        except BaseException as error:
            self.reason = "error"
            self.last_error = error.code if isinstance(error, ProviderError) else type(error).__name__
            await self._release()
            raise

    def _receive_error(self, code):
        self.receive_fault = code

    def seed_audio(self, frames):
        """Replay at most five seconds captured while a wake activation connected."""
        if self.accept_audio:
            self.initial_audio.extend((captured, pcm) for captured, pcm in frames
                                      if len(pcm) == 3840)

    async def _send(self):
        converter = PCMConverter(48000, self.provider.input_rate, source_channels=2)
        last_packet = time.monotonic()
        streaming = False
        while True:
            try:
                seeded = bool(self.initial_audio)
                captured, pcm = self.initial_audio.popleft() if seeded else self.input.get_nowait()
            except queue.Empty:
                if streaming and time.monotonic() - last_packet > 0.7:
                    await self.provider.end_audio()
                    streaming = False
                await asyncio.sleep(0.01)
                continue
            if time.monotonic() - captured > (6 if seeded else 1):
                continue
            if audioop.rms(pcm, 2) > self.limits.rms_threshold:
                self.last_activity = time.monotonic()
            converted = converter.convert(pcm)
            await self.provider.send_audio(converted)
            self.input_seconds += len(converted) / (2 * self.provider.input_rate)
            last_packet, streaming = time.monotonic(), True

    async def _receive(self):
        converter = PCMConverter(24000, 48000, target_channels=2)
        async for event in self.provider.receive_audio():
            if event.kind == "audio":
                if event.sample_rate != converter.source_rate:
                    converter = PCMConverter(event.sample_rate, 48000, target_channels=2)
                if len(event.data) > 1024 * 1024:
                    raise BufferError("Oversized provider audio")
                self.playback.push(converter.convert(event.data))
                self.output_seconds += len(event.data) / (2 * event.sample_rate)
            elif event.kind in {"interrupted", "speech_started"}:
                self.playback.clear()
                converter.state = None
            elif event.kind == "turn_complete":
                self.playback.finish_turn()
            elif event.kind == "usage":
                self.usage = event.usage or {}
        raise ProviderError("connection_closed", transient=True)

    async def _watch(self):
        while True:
            now = time.monotonic()
            if now - self.started >= self.limits.max_seconds:
                self.reason = "session_limit"
                return
            if now - self.last_activity >= self.limits.inactivity:
                self.reason = "inactivity"
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
            await self._release()
            if self.reason != "stopped":
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
