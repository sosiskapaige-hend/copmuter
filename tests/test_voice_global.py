"""Тесты голосового глобального управления: wake-word «Джарвис» и чистка
текста для озвучки (без железа/библиотек — только чистые функции)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.voice.global_voice import normalize_wake_aliases, strip_wake_word
from agent.voice.stt_tts import clean_speech_text


class TestWakeWord(unittest.TestCase):
    def test_normalize_aliases(self):
        cfg = {"wake_word": "Джарвис",
               "wake_aliases": ["жарвис", "jarvis"]}
        aliases = normalize_wake_aliases(cfg)
        self.assertEqual(aliases[0], "джарвис")
        self.assertIn("жарвис", aliases)
        self.assertIn("jarvis", aliases)

    def test_default_alias_when_unset(self):
        self.assertEqual(normalize_wake_aliases({})[0], "джарвис")

    def test_strip_wake_word_prefix(self):
        aliases = ["джарвис", "жарвис", "jarvis"]
        cmd = strip_wake_word("Джарвис создай папку на рабочем столе", aliases)
        self.assertEqual(cmd, "создай папку на рабочем столе")
        cmd = strip_wake_word("жарвис, который час?", aliases)
        self.assertEqual(cmd, "который час?")
        cmd = strip_wake_word("Jarvis open notepad", aliases)
        self.assertEqual(cmd, "open notepad")

    def test_no_wake_word_returns_none(self):
        aliases = ["джарвис"]
        self.assertIsNone(strip_wake_word("привет, как дела?", aliases))
        self.assertIsNone(strip_wake_word("", aliases))
        # слово не в начале фразы — не активация
        self.assertIsNone(strip_wake_word("скажи джарвис привет", aliases))

    def test_punctuation_before_wake_word(self):
        aliases = ["джарвис"]
        cmd = strip_wake_word("— Джарвис, выключи музыку", aliases)
        self.assertEqual(cmd, "выключи музыку")


class TestCleanSpeech(unittest.TestCase):
    def test_cleans_markdown(self):
        text = clean_speech_text("**Готово!** Смотри `код` и [ссылку](https://x.ru)")
        self.assertNotIn("*", text)
        self.assertNotIn("[", text)
        self.assertIn("Готово", text)
        self.assertIn("код", text)

    def test_urls_become_links(self):
        text = clean_speech_text("Документация: https://example.com/page?q=1")
        self.assertIn("ссылка", text)
        self.assertNotIn("https://", text)

    def test_empty(self):
        self.assertEqual(clean_speech_text(""), "")
        self.assertEqual(clean_speech_text("   \n  "), "")


if __name__ == "__main__":
    unittest.main()
