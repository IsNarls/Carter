"""Bounded PCM playback and stateful resampling; no audio is written to disk."""
import audioop
from collections import deque
from threading import Lock

import discord


class PCMConverter:
    def __init__(self, source_rate, target_rate, source_channels=1, target_channels=1):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.source_channels = source_channels
        self.target_channels = target_channels
        self.state = None

    def convert(self, pcm):
        if len(pcm) % (2 * self.source_channels):
            raise ValueError("Unaligned PCM")
        if self.source_channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        pcm, self.state = audioop.ratecv(
            pcm, 2, 1, self.source_rate, self.target_rate, self.state,
        )
        if self.target_channels == 2:
            pcm = audioop.tostereo(pcm, 2, 1, 1)
        return pcm


class PCMPlayback(discord.AudioSource):
    """discord.py pulls exactly 20ms (48kHz stereo PCM16) from its audio thread."""
    FRAME = 3840

    def __init__(self, max_frames=1000):
        self.max_frames = max_frames  # At most 20 seconds of generated audio.
        self.frames = deque()
        self.pending = b""
        self.lock = Lock()
        self.closed = False
        self.played_frames = 0

    def push(self, pcm):
        with self.lock:
            if self.closed:
                return
            data = self.pending + pcm
            count = len(data) // self.FRAME
            if len(self.frames) + count > self.max_frames:
                raise BufferError("Playback buffer full")
            self.frames.extend(data[i*self.FRAME:(i+1)*self.FRAME] for i in range(count))
            self.pending = data[count*self.FRAME:]

    def finish_turn(self):
        with self.lock:
            if self.pending:
                if len(self.frames) >= self.max_frames:
                    raise BufferError("Playback buffer full")
                self.frames.append(self.pending.ljust(self.FRAME, b"\0"))
                self.pending = b""

    def read(self):
        with self.lock:
            if self.closed:
                return b""
            if self.frames:
                self.played_frames += 1
                return self.frames.popleft()
        return bytes(self.FRAME)

    def clear(self):
        with self.lock:
            self.frames.clear()
            self.pending = b""

    def cleanup(self):
        with self.lock:
            self.closed = True
            self.frames.clear()
            self.pending = b""
