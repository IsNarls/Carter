import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import discord
from discord.utils import MISSING

from discord_receive import CarterVoiceClient, PCMReceiveSink, _KeepAlive, _ReceiveReader


class ReceiveSinkTests(unittest.TestCase):
    def setUp(self):
        self.callback = Mock()
        self.errors = Mock()
        self.sink = PCMReceiveSink(self.callback, {7}, on_error=self.errors)
        self.session = Mock(ready=True)
        self.session.decrypt.return_value = b"plain opus"
        self.connection = SimpleNamespace(dave_protocol_version=1, dave_session=self.session)
        self.sink._voice_client = SimpleNamespace(_connection=self.connection)
        self.user = SimpleNamespace(id=7, bot=False)
        self.data = SimpleNamespace(
            opus=b"encrypted opus",
            packet=SimpleNamespace(ssrc=11, payload=120, is_silence=lambda: False),
        )
        self.decoder = Mock()
        self.decoder.decode.return_value = b"pcm"
        self.decoder_patch = patch("discord_receive.discord.opus.Decoder", return_value=self.decoder)
        self.decoder_patch.start()
        self.addCleanup(self.decoder_patch.stop)
        self.addCleanup(self.sink.cleanup)

    def test_dave_decrypts_before_opus_decode(self):
        self.sink.write(self.user, self.data)
        self.session.decrypt.assert_called_once()
        self.decoder.decode.assert_called_once_with(b"plain opus", fec=False)
        self.callback.assert_called_once_with(7, b"pcm")

    def test_inactive_other_users_bots_unknowns_and_video_are_dropped(self):
        self.sink.write(None, self.data)
        self.sink.write(SimpleNamespace(id=8, bot=False), self.data)
        self.sink.write(SimpleNamespace(id=7, bot=True), self.data)
        self.data.packet.payload = 101
        self.sink.write(self.user, self.data)
        self.session.decrypt.assert_not_called()
        self.decoder.decode.assert_not_called()
        self.callback.assert_not_called()

    def test_unready_dave_drops_without_attempting_ciphertext_decode(self):
        self.session.ready = False
        self.sink.write(self.user, self.data)
        self.session.decrypt.assert_not_called()
        self.decoder.decode.assert_not_called()
        self.assertEqual(self.sink.last_error, "dave_not_ready")
        self.errors.assert_not_called()

    def test_persistent_decryption_failure_surfaces_sanitized_error_once(self):
        self.session.decrypt.side_effect = ValueError("private provider detail")
        for _ in range(80):
            self.sink.write(self.user, self.data)
        self.errors.assert_called_once_with("dave_decryption_failed")
        self.decoder.decode.assert_not_called()
        self.callback.assert_not_called()
        self.session.decrypt.side_effect = None
        self.sink.write(self.user, self.data)
        self.assertIsNone(self.sink.last_error)

    def test_non_dave_audio_can_decode_without_session(self):
        self.connection.dave_protocol_version = 0
        self.connection.dave_session = None
        self.sink.write(self.user, self.data)
        self.decoder.decode.assert_called_once_with(b"encrypted opus", fec=False)

    def test_decode_fault_does_not_kill_receive_router(self):
        self.decoder_patch.stop()
        discord.opus.Decoder()  # OpusError's constructor needs libopus loaded.
        self.decoder_patch.start()
        self.decoder.decode.side_effect = discord.opus.OpusError(-4)
        for _ in range(50):
            self.sink.write(self.user, self.data)
        self.errors.assert_called_once_with("opus_decode_failed")
        self.callback.assert_not_called()

    def test_transport_silence_and_cleanup_do_not_forward_audio(self):
        self.data.packet.is_silence = lambda: True
        self.sink.write(self.user, self.data)
        self.data.packet.is_silence = lambda: False
        self.sink.cleanup()
        self.sink.write(self.user, self.data)
        self.session.decrypt.assert_not_called()
        self.callback.assert_not_called()

    def test_real_opus_encoding_decoding_outputs_stereo_pcm(self):
        # A generated tone-free PCM frame exercises the installed libopus.
        self.decoder_patch.stop()
        self.connection.dave_protocol_version = 0
        self.data.opus = discord.opus.Encoder().encode(b"\0" * 3840, 960)
        self.sink.write(self.user, self.data)
        self.assertEqual(len(self.callback.call_args.args[1]), 3840)


class ReceiveLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_cleanup_uses_bounded_worker_joins(self):
        reader = object.__new__(_ReceiveReader)
        reader.stopped = threading.Event()
        workers = []
        for name in ("packet_router", "event_router", "speaking_timer", "keepalive"):
            worker = SimpleNamespace(ident=1, join=Mock())
            setattr(reader, name, worker)
            workers.append(worker)
        with patch("discord_receive.AudioReader._stop"):
            reader._stop()
        self.assertTrue(reader.stopped.is_set())
        for worker in workers:
            worker.join.assert_called_once_with(timeout=reader.WORKER_JOIN_TIMEOUT)

    async def test_stop_wait_and_repeated_cleanup(self):
        voice = object.__new__(CarterVoiceClient)
        voice._receive_lock = threading.RLock()
        voice._stopping_readers = []
        stopped = threading.Event()
        reader = SimpleNamespace(stopped=stopped, stop=stopped.set)
        voice._reader = reader
        voice.stop_listening()
        voice.stop_listening()
        await voice.wait_receive_cleanup(timeout=0.1)
        self.assertIs(voice._reader, MISSING)
        self.assertEqual(voice._stopping_readers, [])

    async def test_unfinished_cleanup_times_out(self):
        voice = object.__new__(CarterVoiceClient)
        voice._receive_lock = threading.RLock()
        voice._stopping_readers = [SimpleNamespace(stopped=threading.Event())]
        with self.assertRaises(TimeoutError):
            await voice.wait_receive_cleanup(timeout=0.02)

    async def test_member_leaving_while_inactive_does_not_access_reader(self):
        voice = object.__new__(CarterVoiceClient)
        voice._receive_lock = threading.RLock()
        voice._reader = MISSING
        voice._id_to_ssrc = {7: 11}
        voice._ssrc_to_id = {11: 7}
        voice._remove_ssrc(user_id=7)
        self.assertEqual(voice._id_to_ssrc, {})
        self.assertEqual(voice._ssrc_to_id, {})

    async def test_keepalive_stops_promptly(self):
        socket = Mock()
        voice = SimpleNamespace(
            is_connected=lambda: True,
            _connection=SimpleNamespace(socket=socket, endpoint_ip="127.0.0.1", voice_port=1234),
        )
        keepalive = _KeepAlive(voice)
        keepalive.start()
        keepalive.stop()
        keepalive.join(timeout=0.2)
        self.assertFalse(keepalive.is_alive())
        socket.sendto.assert_called_once()


if __name__ == "__main__":
    unittest.main()
