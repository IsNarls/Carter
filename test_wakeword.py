"""Wake-word checks use local fake recognizers and never read credentials."""
import json
import struct
import tempfile
import unittest
from collections import deque
from unittest.mock import Mock, patch

from wakeword import WakeWordDetector


VOICE = struct.pack("<hh", 2000, 2000) * 960
SILENCE = bytes(3840)


class Recognizer:
    def __init__(self):
        self.results = deque()
        self.partial = ""
        self.final = ""
        self.audio = []
        self.resets = 0

    def AcceptWaveform(self, pcm):
        self.audio.append(pcm)
        if self.results:
            final, text = self.results.popleft()
            if final:
                self.final = text
            else:
                self.partial = text
            return final
        return False

    def PartialResult(self):
        return json.dumps({"partial": self.partial})

    def Result(self):
        return json.dumps({"text": self.final})

    def Reset(self):
        self.resets += 1
        self.partial = self.final = ""


class WakeWordTests(unittest.TestCase):
    def setUp(self):
        self.recognizers = []

        def factory():
            recognizer = Recognizer()
            self.recognizers.append(recognizer)
            return recognizer

        self.detector = WakeWordDetector("", recognizer_factory=factory)
        self.addCleanup(self.detector.close)

    def recognizer(self, user_id=1):
        self.detector.process(user_id, SILENCE)
        return self.detector._speakers[user_id][0]

    def test_whole_name_in_partial_natural_utterance_triggers_once(self):
        recognizer = self.recognizer()
        recognizer.results.append((False, "hey Carter tell me the time"))
        self.assertTrue(self.detector.process(1, VOICE))
        self.assertEqual(recognizer.resets, 1)
        self.assertFalse(self.detector.process(1, VOICE))

    def test_partial_substrings_do_not_trigger(self):
        recognizer = self.recognizer()
        for text in ("cart", "descartes", "carters", "carterton", "car tart", ""):
            recognizer.results.append((False, text))
            self.assertFalse(self.detector.process(1, VOICE), text)

    def test_silence_does_not_reuse_stale_partial(self):
        recognizer = self.recognizer()
        recognizer.partial = "carter"
        self.assertFalse(self.detector.process(1, SILENCE))
        self.assertFalse(self.detector.process(1, SILENCE))
        self.assertEqual(recognizer.resets, 0)

    def test_final_result_can_trigger_after_speech_ends(self):
        recognizer = self.recognizer()
        self.detector.process(1, VOICE)
        recognizer.results.append((True, "hello carter"))
        self.assertTrue(self.detector.process(1, SILENCE))
        self.assertEqual(recognizer.resets, 1)
        self.assertFalse(self.detector.process(1, SILENCE))

    def test_silence_only_final_hallucination_does_not_trigger(self):
        recognizer = self.recognizer()
        recognizer.results.append((True, "carter"))
        self.assertFalse(self.detector.process(1, SILENCE))

    def test_speaker_audio_and_names_are_kept_separate(self):
        first = self.recognizer(1)
        second = self.recognizer(2)
        first.results.append((False, "carter"))
        self.assertFalse(self.detector.process(2, VOICE))
        self.assertTrue(self.detector.process(1, VOICE))
        self.assertEqual(len(second.audio[-1]), 640)
        self.assertEqual(struct.unpack("<320h", second.audio[-1]), (2000,) * 320)

    def test_resamplers_preserve_audio_across_frame_boundaries(self):
        recognizer = self.recognizer()
        first_converter = self.detector._speakers[1][1]
        self.detector.process(1, VOICE)
        self.assertIs(self.detector._speakers[1][1], first_converter)
        recognizer.results.append((False, "carter"))
        self.detector.process(1, VOICE)
        self.assertIsNot(self.detector._speakers[1][1], first_converter)

    def test_lru_evicts_oldest_speaker_at_eight(self):
        for user_id in range(1, 9):
            self.detector.process(user_id, SILENCE)
        oldest = self.recognizers[0]
        self.detector.process(1, SILENCE)
        self.detector.process(9, SILENCE)
        self.assertEqual(len(self.detector._speakers), 8)
        self.assertIn(1, self.detector._speakers)
        self.assertNotIn(2, self.detector._speakers)
        self.assertEqual(oldest.resets, 0)
        self.assertEqual(self.recognizers[1].resets, 1)

    def test_reset_one_or_all_discards_recognizers_and_audio(self):
        first = self.recognizer(1)
        second = self.recognizer(2)
        self.detector.reset(1)
        self.assertNotIn(1, self.detector._speakers)
        self.assertIn(2, self.detector._speakers)
        self.assertEqual(first.resets, 1)
        self.detector.reset()
        self.assertFalse(self.detector._speakers)
        self.assertEqual(second.resets, 1)
        self.detector.reset(99)

    def test_empty_invalid_and_closed_audio(self):
        self.assertFalse(self.detector.process(1, b""))
        self.assertFalse(self.recognizers)
        with self.assertRaises(ValueError):
            self.detector.process(1, b"\0\0")
        self.detector.close()
        self.detector.close()
        self.assertFalse(self.detector.process(1, VOICE))
        self.assertFalse(self.detector._speakers)

    def test_long_unbroken_speech_resets_to_bound_decoder_state(self):
        recognizer = self.recognizer()
        self.detector._speakers[1][3] = 20 * 16000 - 320
        self.assertFalse(self.detector.process(1, VOICE))
        self.assertEqual(recognizer.resets, 1)
        self.assertEqual(self.detector._speakers[1][3], 0)

    def test_multiword_phrase_matches_contiguous_whole_tokens(self):
        recognizer = Recognizer()
        detector = WakeWordDetector("", "hey carter", lambda: recognizer)
        self.addCleanup(detector.close)
        recognizer.results.extend([(False, "hey there carter"), (False, "okay hey carter")])
        self.assertFalse(detector.process(1, VOICE))
        self.assertTrue(detector.process(1, VOICE))

    def test_invalid_phrase_and_missing_model_have_clear_errors(self):
        with self.assertRaises(ValueError):
            WakeWordDetector("", "123", lambda: Recognizer())
        with self.assertRaisesRegex(RuntimeError, "model missing"):
            WakeWordDetector("/nonexistent-carter-wake-model")

    def test_local_model_is_loaded_once_and_released_on_close(self):
        vosk = Mock()
        vosk.KaldiRecognizer.side_effect = lambda *_args: Recognizer()
        with tempfile.TemporaryDirectory() as model_path, patch.dict("sys.modules", {"vosk": vosk}):
            detector = WakeWordDetector(model_path)
            detector.process(1, SILENCE)
            detector.process(2, SILENCE)
            vosk.Model.assert_called_once_with(model_path=model_path)
            self.assertEqual(vosk.KaldiRecognizer.call_count, 2)
            self.assertEqual(len(vosk.KaldiRecognizer.call_args.args), 2)
            vosk.SetLogLevel.assert_called_once_with(-1)
            detector.close()
            self.assertIsNone(detector._model)
            self.assertIsNone(detector._recognizer_factory)


if __name__ == "__main__":
    unittest.main()
