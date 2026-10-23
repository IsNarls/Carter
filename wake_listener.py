"""Persistent local listener; Gemini/OpenAI are closed until a permitted user wakes Carter."""
import asyncio
import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from discord_receive import PCMReceiveSink
from wakeword import WakeWordDetector

log = logging.getLogger("carter.wake")


@dataclass(frozen=True)
class WakeSettings:
    enabled: bool = True
    word: str = "Carter"
    model_path: str = str(Path(__file__).resolve().parent / "models" / "vosk-model-small-en-us-0.15")
    auto_join: bool = True


class WakeListener:
    def __init__(self, session, allowed_users, settings, on_wake, factory=WakeWordDetector):
        self.session, self.allowed_users, self.settings = session, frozenset(allowed_users), settings
        self.on_wake, self.factory = on_wake, factory
        self.voice = self.task = self.detector = self.executor = None
        self.running = False
        self.last_error = None
        self.generation = 0
        self.cooldown_until = 0
        self.input = queue.Queue(maxsize=100)
        self.guard = threading.Lock()
        self.pending_owner = None
        self.pending = deque(maxlen=250)  # Five seconds while connecting; never written to disk.
        self.history = OrderedDict()
        self.silence_flushed = set()
        self.reset_requested = False
        self.receive_fault = None
        self.dropped_non_owner_frames = 0

    @property
    def state(self):
        if not self.settings.enabled:
            return "disabled"
        if self.last_error:
            return "unavailable"
        if not self.running:
            return "off"
        if self.pending_owner is not None:
            return "connecting"
        return "conversation" if self.session.active else "armed"

    def _clear_queue(self):
        while True:
            try:
                self.input.get_nowait()
            except queue.Empty:
                break

    def reset(self, cooldown=3):
        # Invalidate queued detections before stop/leave/manual activation takes effect.
        with self.guard:
            self.generation += 1
            self.pending_owner = None
            self.pending.clear()
            self.history.clear()
            self.silence_flushed.clear()
            self._clear_queue()
            self.reset_requested = True
            self.cooldown_until = time.monotonic() + cooldown

    def capture(self, user_id, pcm):
        if len(pcm) != 3840 or user_id not in self.allowed_users:
            return
        with self.guard:
            if not self.running:
                return
            now = time.monotonic()
            if self.pending_owner is not None:
                if user_id == self.pending_owner:
                    self.pending.append((now, pcm))
            elif self.session.active:
                # The shared Discord receiver listens for every authorized user
                # so any one of them can wake Carter while it is armed. Once a
                # conversation starts, ownership is exclusive: another
                # authorized speaker must never feed or interrupt that session.
                if user_id == self.session.owner_id:
                    self.session.capture(user_id, pcm)
                else:
                    self.dropped_non_owner_frames += 1
            elif now >= self.cooldown_until:
                try:
                    self.input.put_nowait((self.generation, now, user_id, pcm))
                except queue.Full:
                    # Do not decode stale speech after CPU congestion.
                    self._clear_queue()
                    self.reset_requested = True

    def _fault(self, code):
        self.receive_fault = code

    async def _native(self, method, *args):
        # Keep native jobs independent of task cancellation. An in-flight Vosk
        # call cannot be cancelled; close must remain queued behind it on this
        # single worker. Polling also avoids losing executor completion during
        # receiver-task cancellation on Python 3.13.
        future = self.executor.submit(method, *args)
        while not future.done():
            await asyncio.sleep(0.005)
        return future.result()

    async def start(self, voice):
        if self.running and self.voice is voice:
            return
        await self.stop()
        if not self.settings.enabled:
            return
        self.voice = voice
        self.last_error = self.receive_fault = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="carter-wake")
        try:
            self.detector = await self._native(self.factory, self.settings.model_path, self.settings.word)
            self.reset(cooldown=0)
            self.voice.listen(PCMReceiveSink(self.capture, self.allowed_users, on_error=self._fault))
            self.running = True
            self.task = asyncio.create_task(self._run(), name="carter-wake-listener")
            log.info("Local wake listener armed for '%s'", self.settings.word)
        except BaseException as error:
            self.last_error = type(error).__name__
            await self.stop()
            raise

    async def _run(self):
        was_active = False
        try:
            while self.running:
                if self.receive_fault:
                    self.last_error = self.receive_fault
                    raise RuntimeError("wake_receive_failed")
                if not self.voice.is_connected() or not self.voice.is_listening():
                    self.last_error = "voice_disconnected"
                    return
                if self.session.active or (self.session.task and not self.session.task.done()):
                    was_active = True
                    await asyncio.sleep(0.05)
                    continue
                if was_active:
                    self.reset()
                    was_active = False
                    log.info("Conversation ended; local wake listener armed")
                if self.reset_requested:
                    self.reset_requested = False
                    with self.guard:
                        self.history.clear()
                        self.silence_flushed.clear()
                    await self._native(self.detector.reset)
                synthetic_silence = False
                try:
                    generation, captured, user_id, pcm = self.input.get_nowait()
                except queue.Empty:
                    # Discord stops transmitting at silence. Vosk still needs
                    # a short silence tail to finish a standalone spoken name.
                    now = time.monotonic()
                    user_id = next((speaker for speaker, frames in self.history.items()
                                    if speaker not in self.silence_flushed and frames
                                    and now - frames[-1][0] >= 0.3), None)
                    if user_id is None:
                        await asyncio.sleep(0.01)
                        continue
                    self.silence_flushed.add(user_id)
                    generation, captured, pcm = self.generation, now, bytes(48000 * 4)
                    synthetic_silence = True
                if generation != self.generation or time.monotonic() - captured > 1:
                    continue
                frames = self.history.setdefault(user_id, deque(maxlen=50))
                self.history.move_to_end(user_id)
                while len(self.history) > 8:
                    evicted, _ = self.history.popitem(last=False)
                    self.silence_flushed.discard(evicted)
                if not synthetic_silence:
                    self.silence_flushed.discard(user_id)
                    frames.append((captured, pcm))
                detected = await self._native(self.detector.process, user_id, pcm)
                if not detected or generation != self.generation or time.monotonic() < self.cooldown_until:
                    continue
                with self.guard:
                    self.pending_owner = user_id
                    self.pending.extend((stamp, data) for stamp, data in frames
                                        if captured - stamp < 1.5)
                    # Preserve packets received after the native detector started this frame.
                    while True:
                        try:
                            gen, stamp, speaker, data = self.input.get_nowait()
                        except queue.Empty:
                            break
                        if gen == generation and speaker == user_id:
                            self.pending.append((stamp, data))
                success = False
                try:
                    success = await self.on_wake(user_id, self.voice, generation)
                    success = success and self.session.active and self.session.owner_id == user_id
                    if success:
                        with self.guard:
                            self.session.seed_audio(self.pending, source_user_id=user_id)
                            self.pending.clear()
                            self.pending_owner = None
                        was_active = True
                finally:
                    if not success:
                        self.reset(cooldown=30)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = self.last_error or type(error).__name__
            log.warning("Local wake listener unavailable: %s", self.last_error)
        finally:
            self.running = False
            await self._stop_receiver()

    async def _stop_receiver(self):
        if self.voice:
            self.voice.stop_listening()
            try:
                await self.voice.wait_receive_cleanup()
            except Exception:
                log.warning("Wake receiver cleanup incomplete")

    async def stop(self):
        self.running = False
        self.reset()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self._stop_receiver()
        if self.detector:
            await self._native(self.detector.close)
            self.detector = None
        if self.executor:
            self.executor.shutdown(wait=False)
            self.executor = None
        self.voice = None
