"""Speech-to-text backends.

The default backend is faster-whisper (an open-source, self-hosted Whisper
implementation). Whisper is multilingual, so a sentence that mixes Hindi and
English can be transcribed in one pass.

Language "modes" exposed to the UI:
  auto      let Whisper detect the language
  hinglish  Hindi + English mixed: Hindi in Devanagari, English words in Latin.
            Done by pinning language=hi and priming the decoder with a mixed prompt.
  en / hi   force a single language
"""
import logging
import threading
from functools import lru_cache

import numpy as np

from . import config

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

MIXED_PROMPT = "नमस्ते, how are you? मैं ठीक हूँ, let's start the meeting. आज का agenda क्या है?"

# mode -> (language code or None, initial prompt or None)
MODES: dict[str, tuple[str | None, str | None]] = {
    "auto": (None, None),
    "hinglish": ("hi", MIXED_PROMPT),
    "en": ("en", None),
    "hi": ("hi", None),
}


class MockTranscriber:
    """Deterministic fake used by the test-suite (STT_BACKEND=mock)."""

    def warmup(self) -> None:
        pass

    def transcribe(self, audio: np.ndarray, mode: str = "auto") -> tuple[str, str | None]:
        return f"mock transcript {len(audio) / SAMPLE_RATE:.1f}s", "en"


class WhisperTranscriber:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()  # CTranslate2 model: one decode at a time

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model '%s' (%s)", config.WHISPER_MODEL, config.WHISPER_COMPUTE_TYPE)
            self._model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
            )
        return self._model

    def warmup(self) -> None:
        with self._lock:
            self._load()

    def transcribe(self, audio: np.ndarray, mode: str = "auto") -> tuple[str, str | None]:
        language, prompt = MODES.get(mode, MODES["auto"])
        with self._lock:
            model = self._load()
            segments, info = model.transcribe(
                audio,
                language=language,
                initial_prompt=prompt,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language


@lru_cache(maxsize=1)
def get_transcriber():
    if config.STT_BACKEND == "mock":
        return MockTranscriber()
    return WhisperTranscriber()


def load_audio(path: str) -> np.ndarray:
    """Decode any audio/video file to 16 kHz mono float32."""
    if config.STT_BACKEND == "mock":
        return np.zeros(SAMPLE_RATE, dtype=np.float32)
    from faster_whisper.audio import decode_audio

    return decode_audio(path, sampling_rate=SAMPLE_RATE)
