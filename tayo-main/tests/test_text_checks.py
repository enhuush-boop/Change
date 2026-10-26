from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.app.services.text_analysis import _chunks, analyze_text


class Model:
    model_id = "test-model"

    def predict(self, text: str) -> dict:
        return {
            "ai_probability": 0.95,
            "latency_ms": 1,
            "tokens": 10,
            "truncated": False,
        }


class TextQualityTests(unittest.TestCase):
    def test_short_and_repeated_inputs_skip_model(self) -> None:
        for text in ["aaaaaaaaaaaaaaaaaaaaaaaaa", "2026 !!! 12345", "hello " * 100]:
            with self.subTest(text=text), patch(
                "backend.app.services.text_analysis.get_text_model"
            ) as loader:
                result = analyze_text(text)
                loader.assert_not_called()
                self.assertEqual(result["label"], "Uncertain")
                self.assertIsNone(result["ai_probability"])
                self.assertEqual(result["quality"]["status"], "insufficient_input")

    def test_short_meaningful_text_gets_model_scores(self) -> None:
        with patch(
            "backend.app.services.text_analysis.get_text_model",
            return_value=(Model(), None),
        ):
            result = analyze_text("Энэ бол богино монгол текст юм.")
        self.assertEqual(result["ai_probability"], 0.95)
        self.assertEqual(result["label"], "Uncertain")
        self.assertTrue(result["quality"]["warnings"])

    def test_mongolian_never_gets_authorship_verdict(self) -> None:
        text = (
            "Монгол орны байгаль цаг уурын өөрчлөлтөд хэрхэн дасан зохицож "
            "байгааг судлаачид олон жилийн турш ажиглаж байна. Хөдөөгийн иргэд "
            "өөрсдийн туршлага дээр үндэслэн ус хөрс ургамлыг хамгаалах шинэ "
            "аргуудыг хамтран туршжээ. "
        ) * 3
        with patch(
            "backend.app.services.text_analysis.get_text_model",
            return_value=(Model(), None),
        ):
            result = analyze_text(text)
        self.assertGreater(result["text_stats"]["words"], 50)
        self.assertEqual(result["ai_probability"], 0.95)
        self.assertEqual(result["label"], "Uncertain")
        self.assertEqual(result["quality"]["status"], "unvalidated_language")
        self.assertIsNone(result["confidence"])

    def test_model_failure_does_not_promote_heuristic_to_verdict(self) -> None:
        unique = " ".join(
            f"uniqueword{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}"
            for i in range(80)
        )
        text = unique + " varied writing sample " * 25
        with patch(
            "backend.app.services.text_analysis.get_text_model",
            return_value=(None, "offline"),
        ):
            result = analyze_text(text)
        self.assertEqual(result["label"], "Uncertain")
        self.assertFalse(result["quality"]["classification_available"])
        self.assertEqual(result["quality"]["status"], "model_unavailable")


class TextChecksTests(unittest.TestCase):
    def test_checks_cover_unavailable_and_empty(self) -> None:
        for text in ["", "12345", "aaaaaaaaaaa"]:
            with self.subTest(text=text), patch(
                "backend.app.services.text_analysis.get_text_model"
            ) as loader:
                result = analyze_text(text)
                self.assertEqual(len(result["checks"]), 18)
                self.assertEqual(
                    next(x for x in result["checks"] if x["id"] == "plagiarism")[
                        "status"
                    ],
                    "unavailable",
                )
                loader.assert_not_called()

    def test_sentence_duplicates_and_invisible(self) -> None:
        with patch(
            "backend.app.services.text_analysis.get_text_model",
            return_value=(None, "offline"),
        ):
            result = analyze_text("Монгол хэл. Монгол хэл. \u200b")
        checks = {c["id"]: c for c in result["checks"]}
        self.assertEqual(checks["sentences"]["value"], 1)
        self.assertEqual(checks["characters"]["value"], 1)

    def test_long_paragraph_windows_are_bounded(self) -> None:
        chunks = _chunks("word " * 2300)
        self.assertEqual(len(chunks), 8)
        self.assertTrue(all(len(c.split()) == 250 for c in chunks))


if __name__ == "__main__":
    unittest.main()
