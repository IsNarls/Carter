"""Receive Discord Opus through voice-recv and decrypt DAVE before decoding.

Pinned voice-recv 0.5.2a179 predates DAVE receive. Requesting Opus bypasses
its PCM path; discord.py still owns the DAVE handshake and encryption keys.
"""

import asyncio
import threading
from collections.abc import Callable, Collection

import discord
from discord.ext import voice_recv
from discord.ext.voice_recv.reader import AudioReader, UDPKeepAlive
from discord.utils import MISSING
from davey import MediaType


class PCMReceiveSink(voice_recv.AudioSink):
    """Calls callback(user_id, 48kHz stereo s16 PCM) on the receive thread.

    Callbacks must be quick and thread safe. Only the activating user's ID
    should be supplied, so other participants never reach the audio provider.
    """

    def __init__(
        self,
        callback: Callable[[int, bytes], None],
        allowed_user_ids: Collection[int],
        *,
        on_error: Callable[[str], None] | None = None,
    ):
        super().__init__()
        self.callback = callback
        self.allowed_user_ids = frozenset(allowed_user_ids)
        self.on_error = on_error
        self.last_error: str | None = None
        self._failures = 0
        self._reported = False
        self._decoders = {}
        self._closed = False

    def wants_opus(self):
        return True

    def _failure(self, code):
        self.last_error = code
        self._failures += 1
        # Key transitions and damaged/lost packets can fail briefly. Persistent
        # failure must surface, without logging audio or exception messages.
        if self._failures >= 50 and not self._reported:
            self._reported = True
            if self.on_error:
                self.on_error(code)

    def write(self, user, data):
        if self._closed or user is None or user.bot or user.id not in self.allowed_user_ids:
            return
        packet = data.packet
        # Fake loss packets and transport silence have no speech to transmit.
        if not packet or packet.is_silence() or not data.opus:
            return
        # Discord's audio RTP payload type is 120. Camera/screen streams must
        # never be passed to an Opus decoder, even when sharing a user's SSRC.
        if getattr(packet, "payload", 120) != 120:
            return
        connection = self.voice_client._connection
        opus = bytes(data.opus)
        if connection.dave_protocol_version > 0:
            session = connection.dave_session
            if session is None or not session.ready:
                self._failure("dave_not_ready")
                return
            try:
                opus = session.decrypt(user.id, MediaType.audio, opus)
            except Exception:
                self._failure("dave_decryption_failed")
                return
        try:
            key = (user.id, packet.ssrc)
            decoder = self._decoders.get(key)
            if decoder is None:
                # Replace any old SSRC for this user after reconnects.
                self._decoders = {k: v for k, v in self._decoders.items() if k[0] != user.id}
                decoder = self._decoders[key] = discord.opus.Decoder()
            pcm = decoder.decode(opus, fec=False)
        except (discord.opus.OpusError, discord.opus.OpusNotLoaded):
            self._failure("opus_decode_failed")
            return
        self._failures = 0
        self._reported = False
        self.last_error = None
        if pcm and not self._closed:
            self.callback(user.id, pcm)

    def cleanup(self):
        self._closed = True
        self._decoders.clear()


class _KeepAlive(UDPKeepAlive):
    def run(self):
        # Upstream sleeps 5000 seconds and cannot wake during stop(). Keep a
        # short interruptible wait so deactivate/rejoin retires its threads.
        while not self._end_thread.is_set():
            if self.voice_client.is_connected():
                connection = self.voice_client._connection
                try:
                    connection.socket.sendto(
                        (self.counter % (1 << 64)).to_bytes(8, "big"),
                        (connection.endpoint_ip, connection.voice_port),
                    )
                except OSError:
                    pass
                self.counter += 1
            self._end_thread.wait(5)

    def stop(self):
        self._end_thread.set()


class _ReceiveReader(AudioReader):
    def __init__(self, sink, voice_client, *, after=None):
        super().__init__(sink, voice_client, after=after)
        self.keepalive = _KeepAlive(voice_client)
        self.stopped = threading.Event()

    def _stop(self):
        try:
            super()._stop()
        finally:
            for worker in (self.packet_router, self.event_router, self.speaking_timer, self.keepalive):
                if worker.ident is not None and worker is not threading.current_thread():
                    # This cleanup runs in its own thread. A stuck old router
                    # must keep a new session blocked; the async caller has a
                    # timeout while waiting for the stopped event.
                    worker.join()
            self.stopped.set()


class CarterVoiceClient(voice_recv.VoiceRecvClient):
    def __init__(self, client, channel):
        super().__init__(client, channel)
        self._receive_lock = threading.RLock()
        self._stopping_readers = []

    def listen(self, sink, *, after=None):
        with self._receive_lock:
            if not self.is_connected():
                raise discord.ClientException("Not connected to voice.")
            if not isinstance(sink, voice_recv.AudioSink):
                raise TypeError("sink must be an AudioSink")
            if self.is_listening():
                raise discord.ClientException("Already receiving audio.")
            if any(not reader.stopped.is_set() for reader in self._stopping_readers):
                raise discord.ClientException("Previous voice receiver is still stopping.")
            self._stopping_readers.clear()
            self._reader = _ReceiveReader(sink, self, after=after)
            self._reader.start()

    def stop_listening(self):
        with self._receive_lock:
            reader = self._reader
            if reader:
                self._reader = MISSING
                self._stopping_readers.append(reader)
                reader.stop()

    async def wait_receive_cleanup(self, timeout=3):
        readers = tuple(self._stopping_readers)
        if not readers:
            return
        async with asyncio.timeout(timeout):
            for reader in readers:
                while not reader.stopped.is_set():
                    await asyncio.sleep(0.01)
        with self._receive_lock:
            self._stopping_readers = [r for r in self._stopping_readers if not r.stopped.is_set()]

    def _remove_ssrc(self, *, user_id):
        # Upstream dereferences a missing reader when a member leaves while
        # Carter is connected but AI is inactive.
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if ssrc is not None:
            with self._receive_lock:
                if self._reader:
                    self._reader.speaking_timer.drop_ssrc(ssrc)
                self._ssrc_to_id.pop(ssrc, None)
