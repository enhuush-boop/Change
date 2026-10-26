from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from backend.app import main
from backend.app.schemas import UrlCheckRequest


class CombinedUrlCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_malicious_url_is_never_fetched(self) -> None:
        security = {
            "url": "https://bad.example/",
            "security_status": "malicious",
        }
        extractor = Mock()
        with (
            patch.object(main, "scan_url", new=AsyncMock(return_value=security)),
            patch.object(main, "extract_web_content", new=extractor),
        ):
            result = await main.api_check_url(UrlCheckRequest(url="https://bad.example"))

        self.assertEqual(result["workflow_status"], "blocked_malicious")
        extractor.assert_not_called()
        self.assertIsNone(result["factcheck"])
        self.assertEqual(result["safety"]["level"], "do_not_open")

    async def test_extracted_content_uses_existing_factcheck_contract(self) -> None:
        security = {
            "url": "https://example.com/story",
            "security_status": "safe-looking",
        }
        website = {
            "title": "Story",
            "url": "https://example.com/story",
            "content": "This is the extracted article claim with enough readable text.",
        }
        factcheck = {"verdict": "Not Sure", "evidence": [], "summary": "No match"}
        check = AsyncMock(return_value=factcheck)
        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(main, "scan_url", new=AsyncMock(return_value=security)),
            patch.object(main, "extract_web_content", return_value=website),
            patch.object(main, "factcheck_claim", new=check),
            patch.object(main, "run_in_threadpool", side_effect=run_inline),
        ):
            result = await main.api_check_url(UrlCheckRequest(url="https://example.com/story"))

        self.assertEqual(result["workflow_status"], "complete")
        self.assertEqual(result["factcheck"], factcheck)
        check.assert_awaited_once_with(website["content"], "auto", "auto")

    async def test_connected_virustotal_key_is_forwarded_to_scanner(self) -> None:
        scanner = AsyncMock(return_value={
            "url": "https://example.com/",
            "security_status": "malicious",
        })
        with patch.object(main, "scan_url", new=scanner):
            await main.api_check_url(UrlCheckRequest(
                url="https://example.com",
                virustotal_api_key="extension-key",
            ))

        scanner.assert_awaited_once_with("https://example.com", "extension-key")

    async def test_page_claims_are_factchecked_separately(self) -> None:
        security = {"url": "https://example.com/", "security_status": "safe-looking"}
        website = {
            "title": "Page",
            "url": "https://example.com/",
            "content": "Fallback content",
            "claims": ["First independently checkable claim.", "Second independently checkable claim."],
        }
        check = AsyncMock(side_effect=[
            {"claim": website["claims"][0], "verdict": "Not Sure"},
            {"claim": website["claims"][1], "verdict": "False"},
        ])

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(main, "scan_url", new=AsyncMock(return_value=security)),
            patch.object(main, "extract_web_content", return_value=website),
            patch.object(main, "factcheck_claim", new=check),
            patch.object(main, "run_in_threadpool", side_effect=run_inline),
        ):
            result = await main.api_check_url(UrlCheckRequest(url="https://example.com"))

        self.assertEqual(len(result["factchecks"]), 2)
        self.assertEqual(result["factchecks"][1]["verdict"], "False")
        self.assertEqual(result["factcheck_overview"]["false"], 1)
        self.assertEqual(result["factcheck_overview"]["status"], "contradicted")

    async def test_cross_host_redirect_destination_is_rescanned_without_vt_submission(self) -> None:
        security = {
            "url": "https://short.example/go",
            "security_status": "safe-looking",
            "risk_score": 5,
            "reasons": [],
            "checks": {},
        }
        destination = {
            "url": "https://final.example/login",
            "security_status": "malicious",
            "risk_score": 75,
            "reasons": ["Threat intelligence flagged the destination."],
            "checks": {},
        }
        website = {
            "title": "Final login page",
            "url": "https://final.example/login",
            "content": "A final page claim that should be checked independently.",
            "claims": ["A final page claim that should be checked independently."],
            "inspection": {
                "page_type": "Login / account page",
                "findings": [],
                "risk_level": "low",
            },
            "fetch": {
                "redirects": [{
                    "status": 302,
                    "from": "https://short.example/go",
                    "to": "https://final.example/login",
                    "cross_host": True,
                }],
            },
        }
        scanner = AsyncMock(side_effect=[security, destination])
        check = AsyncMock(return_value={"verdict": "Not Sure"})

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(main, "scan_url", new=scanner),
            patch.object(main, "extract_web_content", return_value=website),
            patch.object(main, "factcheck_claim", new=check),
            patch.object(main, "run_in_threadpool", side_effect=run_inline),
        ):
            result = await main.api_check_url(UrlCheckRequest(url="https://short.example/go"))

        scanner.assert_has_awaits([
            call("https://short.example/go", None),
            call("https://final.example/login", None, submit_virustotal=False),
        ])
        self.assertEqual(result["destination_security"], destination)
        self.assertEqual(result["workflow_status"], "destination_flagged")
        self.assertEqual(result["safety"]["level"], "do_not_open")
        self.assertEqual(result["overview"]["final_url"], "https://final.example/login")
        self.assertTrue(any(
            "different domain" in reason for reason in result["safety"]["reasons"]
        ))


if __name__ == "__main__":
    unittest.main()
