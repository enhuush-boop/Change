from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.app.services import url_scan


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.payload


class FakeClient:
    def __init__(self, post_response: dict, get_responses: list) -> None:
        self.post_response = post_response
        self.get_responses = iter(get_responses)
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.posts.append((url, kwargs))
        return FakeResponse(self.post_response)

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.gets.append(url)
        response = next(self.get_responses)
        if isinstance(response, tuple):
            status_code, payload = response
            return FakeResponse(payload, status_code=status_code)
        return FakeResponse(response)


class VirusTotalTests(unittest.IsolatedAsyncioTestCase):
    async def test_submits_url_and_returns_completed_analysis(self) -> None:
        client = FakeClient(
            {"data": {"id": "analysis-123"}},
            [
                (404, {}),
                {
                    "data": {
                        "attributes": {
                            "status": "completed",
                            "stats": {"malicious": 2, "suspicious": 1, "harmless": 70},
                            "results": {
                                "Bad Engine": {
                                    "engine_name": "Bad Engine",
                                    "category": "malicious",
                                    "result": "phishing",
                                },
                                "Good Engine": {
                                    "engine_name": "Good Engine",
                                    "category": "harmless",
                                    "result": "clean",
                                },
                            },
                        }
                    }
                }
            ],
        )

        with patch.object(url_scan.settings, "virustotal_api_key", "test-key"):
            result = await url_scan._virustotal("https://example.com/path", client)

        self.assertTrue(result["submitted"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stats"]["malicious"], 2)
        self.assertEqual(result["classification"], "malicious")
        self.assertEqual(result["report_state"], "scanned")
        self.assertEqual(result["detections"][0]["result"], "phishing")
        self.assertEqual(client.posts[0][1]["data"], {"url": "https://example.com/path"})
        self.assertTrue(client.gets[0].startswith("https://www.virustotal.com/api/v3/urls/"))
        self.assertEqual(client.gets[1], "https://www.virustotal.com/api/v3/analyses/analysis-123")

    async def test_queued_analysis_is_polled_twice(self) -> None:
        queued = {"data": {"attributes": {"status": "queued"}}}
        client = FakeClient({"data": {"id": "analysis-queued"}}, [(404, {}), queued, queued])

        with (
            patch.object(url_scan.settings, "virustotal_api_key", "test-key"),
            patch.object(url_scan.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = await url_scan._virustotal("https://example.com", client)

        self.assertEqual(result["status"], "queued")
        self.assertIn("still queued", result["note"])
        self.assertEqual(len(client.gets), 3)
        self.assertEqual(sleep.await_count, 1)

    async def test_request_key_overrides_environment_key(self) -> None:
        client = FakeClient(
            {"data": {"id": "analysis-override"}},
            [(404, {}), {"data": {"attributes": {"status": "completed", "stats": {}}}}],
        )
        with patch.object(url_scan.settings, "virustotal_api_key", "environment-key"):
            await url_scan._virustotal("https://example.com", client, "extension-key")

        self.assertEqual(client.posts[0][1]["headers"]["x-apikey"], "extension-key")

    async def test_invalid_key_format_is_rejected_locally(self) -> None:
        result = await url_scan.validate_virustotal_key("short key")
        self.assertFalse(result["valid"])

    async def test_missing_key_does_not_submit(self) -> None:
        client = FakeClient({}, [])
        with patch.object(url_scan.settings, "virustotal_api_key", ""):
            result = await url_scan._virustotal("https://example.com", client)

        self.assertFalse(result["enabled"])
        self.assertEqual(client.posts, [])

    async def test_zero_detections_is_scanned_but_not_proof_of_safety(self) -> None:
        client = FakeClient(
            {"data": {"id": "analysis-clean"}},
            [(404, {}), {"data": {"attributes": {
                "status": "completed",
                "stats": {"malicious": 0, "suspicious": 0, "harmless": 0, "undetected": 72},
                "results": {},
            }}}],
        )
        result = await url_scan._virustotal("https://example.com", client, "test-key")
        self.assertEqual(result["classification"], "undetected")
        self.assertEqual(result["report_state"], "scanned")

    async def test_rate_limit_has_clear_not_scanned_state(self) -> None:
        client = FakeClient({}, [])

        async def rate_limited_post(url: str, **kwargs) -> FakeResponse:
            return FakeResponse({}, status_code=429)

        client.post = rate_limited_post
        async def not_found_get(url: str, **kwargs) -> FakeResponse:
            return FakeResponse({}, status_code=404)
        client.get = not_found_get
        result = await url_scan._virustotal("https://example.com", client, "test-key")
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["report_state"], "not_scanned")

    async def test_existing_report_is_returned_without_resubmission(self) -> None:
        client = FakeClient({}, [{"data": {"attributes": {
            "last_analysis_date": 1_700_000_000,
            "last_analysis_stats": {"malicious": 0, "harmless": 68},
            "last_analysis_results": {},
        }}}])
        result = await url_scan._virustotal("https://example.com", client, "test-key")
        self.assertEqual(result["report_source"], "existing")
        self.assertEqual(result["stats"]["harmless"], 68)
        self.assertEqual(client.posts, [])


if __name__ == "__main__":
    unittest.main()
