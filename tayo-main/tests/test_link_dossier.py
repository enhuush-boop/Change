from __future__ import annotations

import unittest

from backend.app.services.link_dossier import factcheck_overview, overall_safety


def _security(**overrides) -> dict:
    value = {
        "url": "https://example.com/",
        "security_status": "safe-looking",
        "risk_score": 0,
        "reasons": [],
        "checks": {},
    }
    value.update(overrides)
    return value


def _website(**inspection) -> dict:
    return {
        "url": "https://example.com/",
        "inspection": {
            "findings": [],
            "risk_level": "low",
            **inspection,
        },
    }


class OverallSafetyTests(unittest.TestCase):
    def test_malicious_threat_intel_requires_do_not_open(self) -> None:
        result = overall_safety(_security(security_status="malicious", risk_score=80))

        self.assertEqual(result["level"], "do_not_open")
        self.assertIn("Do not open", result["label"])
        self.assertTrue(result["actions"])

    def test_suspicious_score_recommends_a_sandbox(self) -> None:
        result = overall_safety(_security(security_status="suspicious", risk_score=40))

        self.assertEqual(result["level"], "sandbox_only")
        self.assertTrue(any("sandbox" in action.lower() for action in result["actions"]))

    def test_caution_finding_keeps_the_page_out_of_the_safe_bucket(self) -> None:
        result = overall_safety(
            _security(),
            _website(findings=[{
                "id": "external_form",
                "severity": "caution",
                "title": "Form submits to a different domain",
                "detail": "Confirm the destination before entering personal information.",
            }]),
        )

        self.assertEqual(result["level"], "caution")
        self.assertIn("Form submits to a different domain", result["reasons"][0])

    def test_uninspected_page_is_unknown_not_safe(self) -> None:
        result = overall_safety(_security(), None)

        self.assertEqual(result["level"], "unknown")
        self.assertIn("not proof", result["summary"])

    def test_clean_static_inspection_has_no_strong_signal(self) -> None:
        result = overall_safety(_security(), _website())

        self.assertEqual(result["level"], "no_strong_signal")
        self.assertIn("not a guarantee", result["summary"])
        self.assertIn("zero-click", result["zero_click_note"].lower())

    def test_cross_host_destination_is_considered_and_explained(self) -> None:
        destination = _security(
            url="https://final.example/login",
            security_status="malicious",
            risk_score=75,
        )
        result = overall_safety(_security(url="https://short.example/go"), None, destination)

        self.assertEqual(result["level"], "do_not_open")
        self.assertTrue(any("different domain" in reason for reason in result["reasons"]))
        self.assertIn("final.example", result["external_tools"]["urlscan"])


class FactcheckOverviewTests(unittest.TestCase):
    def test_no_factchecks_is_explicit(self) -> None:
        result = factcheck_overview([])

        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["status"], "none")
        self.assertIn("No page claims", result["headline"])

    def test_contradicted_claims_are_summarized(self) -> None:
        result = factcheck_overview([
            {"verdict": "False"},
            {"verdict": "True"},
            {"verdict": "Not Sure"},
        ])

        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["false"], 1)
        self.assertEqual(result["true"], 1)
        self.assertEqual(result["not_sure"], 1)
        self.assertEqual(result["status"], "contradicted")
        self.assertIn("1 contradicted", result["headline"])

    def test_supported_and_unresolved_are_not_overstated(self) -> None:
        supported = factcheck_overview([{"verdict": "True"}])
        unresolved = factcheck_overview([{"verdict": "Not Sure"}])

        self.assertEqual(supported["status"], "partly_supported")
        self.assertIn("does not validate the whole page", supported["headline"])
        self.assertEqual(unresolved["status"], "unresolved")
        self.assertIn("not the same as true or false", unresolved["headline"])


if __name__ == "__main__":
    unittest.main()
