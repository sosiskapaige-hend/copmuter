"""Голосовой интерфейс (опциональный модуль).

STT: faster-whisper + sounddevice (локально, приватно). Если не установлены —
интерфейс сообщает об этом и голос доступен через браузер (Web Speech API
в Web UI).
TTS: espeak-ng / say / edge-tts (если есть).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class Voice:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._whisper = None
        self._model = None

    def stt_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            import sounddevice  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure(self):
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        self._model = WhisperModel(self.cfg.voice.get("stt_model", "small"), device="cpu")

    def transcribe_file(self, path: str) -> str:
        if not self.stt_available():
            raise RuntimeError("faster-whisper/sounddevice не установлены "
                               "(pip install faster-whisper sounddevice)")
        self._ensure()
        segments, _ = self._model.transcribe(
            path, language=self.cfg.voice.get("language", "ru"))
        return " ".join(s.text.strip() for s in segments)

    def record_and_transcribe(self, seconds: float = 10.0) -> str:
        if not self.stt_available():
            raise RuntimeError("аудио-модули не установлены")
        import sounddevice as sd
        import numpy as np
        samplerate = 16000
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        frames = sd.rec(int(seconds * samplerate), samplerate=samplerate, channels=1)
        sd.wait()
        import wave
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(samplerate)
            w.writeframes(frames.astype("int16").tobytes())
        text = self.transcribe_file(tmp)
        Path(tmp).unlink(missing_ok=True)
        return text

    def tts_available(self) -> bool:
        return any(shutil.which(c) for c in ("espeak-ng", "espeak", "say"))

    def say(self, text: str, out_path: str = "") -> str | None:
        text = text[:500]
        if shutil.which("espeak-ng"):
            out = out_path or (tempfile.gettempdir() + "/agent_tts.ogg")
            r = subprocess.run(["espeak-ng", "-v", "ru", "-w", out, text],
                               capture_output=True, timeout=30)
            return out if r.returncode == 0 else None
        if shutil.which("espeak"):
            out = out_path or (tempfile.gettempdir() + "/agent_tts.wav")
            r = subprocess.run(["espeak", "-v", "ru", "-w", out, text],
                               capture_output=True, timeout=30)
            return out if r.returncode == 0 else None
        if shutil.which("say"):
            r = subprocess.run(["say", text], capture_output=True, timeout=30)
            return "spoken" if r.returncode == 0 else None
        return None
