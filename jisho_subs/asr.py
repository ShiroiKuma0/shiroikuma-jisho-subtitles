"""Transcription with faster-whisper, cached per audio file.

The transcript is scaffolding, not output: it exists so the aligner can find
where in the audio each of the book's sentences is spoken.  Its wording never
reaches the SRT.  That makes the accuracy bar unusually low and the cache
unusually valuable — once a file is transcribed, every later run of the tool is
free, which is what keeps iterating on alignment cheap.

Measured on an RTX 5090 with ``large-v3`` float16, 10 minutes of German audio:
sequential 23.2× realtime, batched(32) **86.5×**.  On 24 CPU cores with int8:
3.9×.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from typing import List, Optional

from .audio import AudioFile, check_decode, decode

CACHE_VERSION = 2   # ffmpeg decoding; PyAV truncated damaged files


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    path: str
    words: List[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "jisho-subs")


def _content_key(path: str, chunk: int = 1 << 20) -> str:
    """Identify a file by size plus its head and tail.

    Hashing whole audiobooks would cost minutes per run for no benefit; size
    plus 2 MB of content is not going to collide across a music library.
    """
    size = os.path.getsize(path)
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(chunk))
        if size > chunk * 2:
            fh.seek(-chunk, os.SEEK_END)
            h.update(fh.read(chunk))
    return h.hexdigest()


class Transcriber:
    """Lazily-loaded Whisper model with an on-disk transcript cache."""

    def __init__(self, model_name: str = "large-v3", device: str = "auto",
                 compute_type: Optional[str] = None, batch_size: int = 32,
                 beam_size: int = 1, cache_dir: Optional[str] = None,
                 initial_prompt: Optional[str] = None, log=None):
        self.model_name = model_name
        self.batch_size = batch_size
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt or None
        self.cache_dir = cache_dir or default_cache_dir()
        self.log = log or (lambda *a, **k: None)
        self._model = None
        self._pipeline = None
        self.device, self.compute_type = self._resolve_device(device, compute_type)

    @staticmethod
    def _resolve_device(device: str, compute_type: Optional[str]):
        """Pick the device by asking CTranslate2, which is what actually runs.

        Deliberately not via torch: nothing else here needs it, and a 2.5 GB
        dependency for one boolean is not worth carrying.
        """
        if device == "auto":
            device = "cpu"
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
            except Exception:
                pass
        if compute_type is None:
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    # -- model -----------------------------------------------------------

    def _ensure_model(self):
        if self._pipeline is not None:
            return
        from faster_whisper import WhisperModel, BatchedInferencePipeline
        self.log(f"loading {self.model_name} on {self.device} ({self.compute_type})")
        kwargs = {}
        if self.device == "cpu":
            kwargs["cpu_threads"] = min(24, os.cpu_count() or 8)
        self._model = WhisperModel(self.model_name, device=self.device,
                                   compute_type=self.compute_type, **kwargs)
        self._pipeline = BatchedInferencePipeline(model=self._model)

    # -- cache -----------------------------------------------------------

    def _cache_path(self, path: str, language: str) -> str:
        payload = "|".join([
            str(CACHE_VERSION), self.model_name, language, str(self.beam_size),
            self.compute_type, self.initial_prompt or "",
        ])
        key = hashlib.sha1(payload.encode()).hexdigest()[:12]
        return os.path.join(self.cache_dir, f"{_content_key(path)}.{key}.json")

    def _load_cached(self, cache_path: str) -> Optional[Transcript]:
        try:
            with open(cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return Transcript(data["path"], [Word(**w) for w in data["words"]])

    def _store(self, cache_path: str, transcript: Transcript) -> None:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"path": transcript.path,
                       "words": [asdict(w) for w in transcript.words]}, fh)
        os.replace(tmp, cache_path)

    # -- transcription ---------------------------------------------------

    def transcribe(self, audio: AudioFile, language: str,
                   force: bool = False) -> Transcript:
        cache_path = self._cache_path(audio.path, language)
        if not force:
            cached = self._load_cached(cache_path)
            if cached is not None:
                self.log(f"  cached   {audio.name}")
                return cached

        self._ensure_model()
        # Decode ourselves rather than letting faster-whisper reach for PyAV,
        # which silently truncates files with malformed cover art.
        samples = decode(audio.path)
        check_decode(audio.path, samples, audio.duration, log=self.log)
        segments, _info = self._pipeline.transcribe(
            samples,
            batch_size=self.batch_size,
            language=language,
            beam_size=self.beam_size,
            word_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=self.initial_prompt,
        )
        words: List[Word] = []
        for seg in segments:
            for w in (seg.words or []):
                text = w.word.strip()
                if text:
                    words.append(Word(text, float(w.start), float(w.end)))
        transcript = Transcript(audio.path, words)
        self._store(cache_path, transcript)
        return transcript
