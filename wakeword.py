"""Local wake-name recognition. No network, audio files, or provider sessions.

Vosk is general speech recognition, so a name may be missed or falsely heard in
noise. Full vocabulary leaves competing words available instead of forcing all
speech into a one-name grammar. The controller supplies a cooldown after a hit.
"""
import audioop
import json
import re
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from audio import PCMConverter


class WakeWordDetector:
    """Feed Discord PCM16/48kHz/stereo from one background worker.

    ``recognizer_factory()`` injects a Vosk-compatible recognizer for offline
    tests. Production loads a single local model, shared by up to eight speaker
    recognizers; importing this module does not import Vosk or load its model.
    """
    MAX_SPEAKERS = 8
    SAMPLE_RATE = 16000
    MIN_RMS = 180
    MAX_UTTERANCE_SECONDS = 20

    def __init__(self, model_path: str, phrase="carter", recognizer_factory=None):
        self.phrase = tuple(re.findall(r"[a-z]+", phrase.casefold()))
        if not self.phrase:
            raise ValueError("Wake phrase must contain English words")
        self._lock = Lock()
        self._speakers = OrderedDict()
        self._closed = False
        self._model = None
        if recognizer_factory is None:
            # Explicit model_path avoids Vosk's automatic model downloads.
            if not model_path or not Path(model_path).is_dir():
                raise RuntimeError("Wake-word model missing; run setup_wakeword.py")
            try:
                from vosk import KaldiRecognizer, Model, SetLogLevel
            except ImportError as exc:
                raise RuntimeError("Install Carter requirements for local wake words") from exc
            SetLogLevel(-1)
            try:
                self._model = Model(model_path=str(model_path))
            except Exception as exc:
                raise RuntimeError("Cannot load wake-word model; run setup_wakeword.py") from exc
            model = self._model
            recognizer_factory = lambda: KaldiRecognizer(model, self.SAMPLE_RATE)
        self._recognizer_factory = recognizer_factory

    def process(self, user_id, pcm) -> bool:
        """Return True only when partial/final text begins with the wake phrase."""
        with self._lock:
            if self._closed or not pcm:
                return False
            if len(pcm) % 4:
                raise ValueError("Unaligned Discord PCM")
            speaker = self._speakers.pop(user_id, None)
            if speaker is None:
                recognizer = self._recognizer_factory()
                speaker = [recognizer, self._converter(), False, 0]
            self._speakers[user_id] = speaker
            if len(self._speakers) > self.MAX_SPEAKERS:
                _, evicted = self._speakers.popitem(last=False)
                evicted[0].Reset()

            recognizer, converter, heard_voice, samples = speaker
            mono = converter.convert(pcm)
            voiced = audioop.rms(mono, 2) >= self.MIN_RMS
            speaker[2] = heard_voice or voiced
            speaker[3] = samples + len(mono) // 2
            final = bool(recognizer.AcceptWaveform(mono))
            # PartialResult can retain old text while silence is fed. Only a
            # voiced frame can trigger a partial; final results consume an
            # utterance and may arrive during the following silence.
            raw = recognizer.Result() if final else (
                recognizer.PartialResult() if voiced else "{}"
            )
            data = json.loads(raw)
            text = data.get("text" if final else "partial", "")
            tokens = tuple(re.findall(r"[a-z]+", str(text).casefold()))
            size = len(self.phrase)
            hit = speaker[2] and tokens[:size] == self.phrase
            if hit or final or speaker[3] >= self.SAMPLE_RATE * self.MAX_UTTERANCE_SECONDS:
                recognizer.Reset()
                speaker[1:] = [self._converter(), False, 0]
            return bool(hit)

    def reset(self, user_id=None):
        """Discard previous speech at activation, cooldown, or speaker departure."""
        with self._lock:
            if user_id is None:
                speakers = list(self._speakers.values())
                self._speakers.clear()
            else:
                speaker = self._speakers.pop(user_id, None)
                speakers = [speaker] if speaker is not None else []
            for speaker in speakers:
                speaker[0].Reset()

    def close(self):
        with self._lock:
            self._closed = True
            self._speakers.clear()
            self._recognizer_factory = None
            self._model = None

    @classmethod
    def _converter(cls):
        return PCMConverter(48000, cls.SAMPLE_RATE, source_channels=2)
