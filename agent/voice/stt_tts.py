"""Голосовой движок (опциональный модуль).

STT: faster-whisper + sounddevice (локально, приватно). Если не установлены —
интерфейс сообщает об этом, и голос доступен через браузер (Web Speech API
в Web UI).

TTS (озвучка локально, для глобального голосового управления):
  1. Windows → PowerShell + System.Speech (встроенный голос Windows);
  2. macOS  → `say`;
  3. Linux  → espeak-ng / espeak.

Запись с микрофона — с детекцией тишины (VAD по энергии): запись начинается
со звуком и заканчивается после паузы, поэтому не нужно угадывать длину фразы.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import numpy as _np  # noqa: F401
    _HAS_NP = True
except Exception:
    _HAS_NP = False


class Voice:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._whisper = None
        self._model = None

    # ---------------- доступность ----------------
    def stt_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            import sounddevice  # noqa: F401
            if not _HAS_NP:
                return False
            return True
        except ImportError:
            return False

    def stt_file_available(self) -> bool:
        """Распознавание файла (без микрофона): достаточно faster-whisper."""
        try:
            import faster_whisper  # noqa: F401
            return _HAS_NP
        except ImportError:
            return False

    def stt_reason(self) -> str:
        missing = []
        for mod in ("faster_whisper", "sounddevice"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        if not _HAS_NP:
            missing.append("numpy")
        if not missing:
            return "доступен"
        return "нужно: pip install " + " ".join(missing)

    def _ensure(self):
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self.cfg.voice.get("stt_model", "small"), device="cpu",
            compute_type="int8")

    def transcribe_file(self, path: str, language: str | None = None) -> str:
        """Распознаёт аудиофайл (wav/mp3/ogg/webm — что декодирует PyAV).

        `language` — код языка («ru», «en»); None → из настроек,
        пустая строка → автоопределение.
        """
        if not self.stt_file_available():
            raise RuntimeError("faster-whisper не установлен "
                               "(pip install faster-whisper numpy)")
        self._ensure()
        lang = self.cfg.voice.get("language", "ru") if language is None else (language or None)
        segments, _ = self._model.transcribe(path, language=lang)
        return " ".join(s.text.strip() for s in segments)

    # ---------------- запись с VAD ----------------
    def _rms(self, block) -> float:
        try:
            import numpy as np
            data = np.frombuffer(block, dtype=np.int16)
            if data.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
        except Exception:
            return 100.0   # numpy нет — считаем, что звук есть (старый режим)

    def record_and_transcribe(self, seconds: float = 10.0,
                              abort=None) -> str:
        """Запись с микрофона и распознавание.

        По умолчанию — умная запись: стартует со звуком и заканчивается
        после паузы (`vad_silence` сек), не длиннее `vad_max_seconds`.
        Число в аргументе трактуется как максимум секунд записи.
        `abort` — опциональный threading.Event: если выставлен во время
        записи (например, хоткей нажат второй раз), запись прекращается.
        """
        if not self.stt_available():
            raise RuntimeError("аудио-модули не установлены: "
                               "pip install faster-whisper sounddevice numpy")
        max_sec = float(seconds or self.cfg.voice.get("vad_max_seconds", 20))
        wav_path = self._record_phrase(max_seconds=max_sec, abort=abort)
        try:
            return self.transcribe_file(wav_path)
        finally:
            Path(wav_path).unlink(missing_ok=True)

    def _record_phrase(self, max_seconds: float = 20.0,
                       silence_after: float | None = None,
                       sample_rate: int = 16000,
                       abort=None) -> str:
        """Пишет фразу в WAV-файл (temp), возвращает путь.

        VAD: ждём начала речи (порог по энергии), копим звук, завершаем по
        тишине `silence_after` секунд или по лимиту `max_seconds`.
        """
        import sounddevice as sd
        import wave
        silence_after = float(silence_after if silence_after is not None
                              else self.cfg.voice.get("vad_silence", 1.2))
        # абсолютная тишина при отсутствии numpy не определяется — пишем всё
        block_s = 0.1
        n_blocks = max(1, int(max_seconds / block_s))
        silent_blocks_for_stop = max(1, int(silence_after / block_s))

        frames: list[bytes] = []
        started = False
        silent_run = 0
        speech_blocks = 0
        start_t = time.time()

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            with sd.RawInputStream(samplerate=sample_rate, channels=1,
                                   dtype="int16", blocksize=int(sample_rate * block_s)) as stream:
                for _ in range(n_blocks + 30):   # +30 блоков на старт речи
                    if abort is not None and abort.is_set():
                        break
                    data, _ = stream.read(int(sample_rate * block_s))
                    rms = self._rms(data)
                    if not started:
                        if rms > 80:
                            started = True
                            silent_run = 0
                    if started:
                        frames.append(bytes(data))
                        speech_blocks += 1
                        silent_run = silent_run + 1 if rms < 60 else 0
                        if silent_run >= silent_blocks_for_stop and speech_blocks > 5:
                            break
                    if time.time() - start_t > max_seconds + 3:
                        break
            if not frames:  # ничего не сказали — короткая пустая запись
                frames.append(b"\x00\x00" * (sample_rate // 2))
            with wave.open(tmp_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(b"".join(frames))
            return tmp_path
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    # ---------------- TTS ----------------
    def tts_available(self) -> bool:
        return self.tts_provider() is not None

    def tts_provider(self) -> str | None:
        if sys.platform == "win32":
            return "windows-sapi"
        if sys.platform == "darwin":
            return "say" if shutil.which("say") else None
        if shutil.which("espeak-ng"):
            return "espeak-ng"
        if shutil.which("espeak"):
            return "espeak"
        if shutil.which("pwsh") or shutil.which("powershell"):
            return "windows-sapi"   # PowerShell Core на Linux/mac
        return None

    def speak(self, text: str) -> bool:
        """Произносит текст вслух (в системные колонки). Блокирующий вызов."""
        text = clean_speech_text(text)[:900]
        if not text:
            return False
        provider = self.tts_provider()
        try:
            if provider == "windows-sapi":
                return self._sapi_speak(text)
            if provider == "say":
                r = subprocess.run(["say", text], capture_output=True, timeout=120)
                return r.returncode == 0
            if provider in ("espeak-ng", "espeak"):
                cmd = [provider, "-v", self._espeak_voice(), "-s", "165", text]
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False
        return False

    def _espeak_voice(self) -> str:
        lang = self.cfg.voice.get("language", "ru")
        return {"ru": "ru+m3", "en": "en+m3"}.get(lang, lang)

    def _sapi_speak(self, text: str) -> bool:
        """Windows PowerShell + System.Speech (голос Windows, без файлов)."""
        import base64
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        ps = (
            "$t=[System.Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{b64}'));"
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "try{$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSpecified,"
            "[System.Speech.Synthesis.VoiceAge]::NotSpecified,0,"
            "[System.Globalization.CultureInfo]::GetCultureInfo('ru-RU'))}catch{};"
            "$s.Speak($t);$s.Dispose()"
        )
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
        exe = "powershell" if shutil.which("powershell") else "pwsh"
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive",
                            "-EncodedCommand", encoded],
                           capture_output=True, timeout=180)
        return r.returncode == 0


def clean_speech_text(text: str) -> str:
    """Убирает markdown/символы, чтобы TTS читал естественно."""
    if not text:
        return ""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_~>#|]", " ", t)
    t = re.sub(r"^\s*[-+]\s+", "", t, flags=re.M)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"https?://\S+", "ссылка", t)
    return t.strip()
