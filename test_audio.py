"""Offline PCM checks; no audio devices, provider calls, or environment files."""
import math
import struct
import unittest

from audio import PCMConverter, PCMPlayback


def mono_samples(count, rate=24000):
    return struct.pack("<" + "h" * count, *(
        int(5000 * math.sin(2 * math.pi * 440 * index / rate))
        for index in range(count)
    ))


class PCMConverterTests(unittest.TestCase):
    def test_discord_stereo_becomes_16khz_mono(self):
        pcm = struct.pack("<hh", 1000, 3000) * 960
        result = PCMConverter(48000, 16000, source_channels=2).convert(pcm)
        self.assertEqual(len(result), 320 * 2)
        self.assertEqual(struct.unpack("<320h", result), (2000,) * 320)

    def test_provider_mono_becomes_48khz_stereo(self):
        pcm = mono_samples(480)
        result = PCMConverter(24000, 48000, target_channels=2).convert(pcm)
        # Stateful interpolation starts at the first sample and holds its tail.
        self.assertLessEqual(abs(len(result) - 960 * 4), 4)
        samples = struct.unpack("<" + "h" * (len(result) // 2), result)
        self.assertEqual(samples[::2], samples[1::2])
        self.assertGreater(max(samples), 4000)

    def test_chunk_boundaries_preserve_resampling_state(self):
        pcm = mono_samples(1440)
        whole = PCMConverter(24000, 48000).convert(pcm)
        converter = PCMConverter(24000, 48000)
        split = b"".join(converter.convert(pcm[start:start + 960])
                         for start in range(0, len(pcm), 960))
        self.assertEqual(split, whole)
        self.assertIsNotNone(converter.state)

    def test_unaligned_samples_are_rejected(self):
        with self.assertRaises(ValueError):
            PCMConverter(48000, 16000, source_channels=2).convert(b"\0\0")
        with self.assertRaises(ValueError):
            PCMConverter(24000, 48000).convert(b"\0")


class PCMPlaybackTests(unittest.TestCase):
    def test_partial_frames_are_combined_and_final_tail_is_padded(self):
        source = PCMPlayback()
        first = b"\x01\x02" * 100
        rest = b"\x03\x04" * ((source.FRAME - len(first)) // 2)
        source.push(first)
        self.assertEqual(source.read(), bytes(source.FRAME))
        source.push(rest + b"\x05\x06")
        self.assertEqual(source.read(), first + rest)
        source.finish_turn()
        self.assertEqual(source.read(), b"\x05\x06" + bytes(source.FRAME - 2))
        self.assertEqual(source.played_frames, 2)

    def test_clear_discards_queued_and_partial_audio(self):
        source = PCMPlayback()
        source.push(b"\x01" * (source.FRAME + 2))
        source.clear()
        source.finish_turn()
        self.assertEqual(source.read(), bytes(source.FRAME))
        self.assertFalse(source.frames)
        self.assertEqual(source.pending, b"")

    def test_closed_source_ends_playback_and_ignores_push(self):
        source = PCMPlayback()
        source.push(b"\x01" * source.FRAME)
        source.cleanup()
        source.push(b"\x02" * source.FRAME)
        source.finish_turn()
        self.assertEqual(source.read(), b"")
        self.assertFalse(source.frames)
        self.assertEqual(source.pending, b"")

    def test_overflow_is_bounded_and_does_not_replace_existing_audio(self):
        source = PCMPlayback(max_frames=1)
        source.push(b"\x01" * source.FRAME + b"\x02\x02")
        with self.assertRaises(BufferError):
            source.push(b"\x03" * source.FRAME)
        with self.assertRaises(BufferError):
            source.finish_turn()
        self.assertEqual(len(source.frames), 1)
        self.assertEqual(source.pending, b"\x02\x02")
        self.assertEqual(source.read(), b"\x01" * source.FRAME)
        source.finish_turn()
        self.assertEqual(source.read(), b"\x02\x02" + bytes(source.FRAME - 2))


if __name__ == "__main__":
    unittest.main()
