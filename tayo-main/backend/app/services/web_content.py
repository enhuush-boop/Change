"""SSRF-protected extraction of readable text, page safety signals, and previews."""

from __future__ import annotations

import base64
import html
import http.client
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

from ..config import settings
from .mongolian import detect_language
from .net_utils import UnsafeUrlError, parse_http_url, resolve_public_host

REDIRECTS = {301, 302, 303, 307, 308}
RASTER_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
BLOCK_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header", "aside",
    "form", "button", "svg", "canvas", "iframe", "dialog", "template",
}
TEXT_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "article", "main", "section", "div",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
SENSITIVE_FIELD_NAMES = {
    "password", "passwd", "pass", "pin", "otp", "token", "secret", "email",
    "username", "user", "login", "card", "cvv", "cvc", "iban", "account",
    "ssn", "tax", "wallet", "seed", "mnemonic",
}
EXECUTABLE_EXTENSIONS = {
    ".exe", ".scr", ".msi", ".bat", ".cmd", ".pif", ".vbs",
    ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".cpl", ".lnk",
    ".iso", ".img", ".vhd", ".vhdx", ".jar", ".apk", ".dmg", ".pkg",
    ".deb", ".rpm", ".ps1", ".psm1", ".reg", ".msix", ".appx",
}
TRACKER_HOSTS = {
    "www.googletagmanager.com", "googletagmanager.com",
    "www.google-analytics.com", "google-analytics.com",
    "connect.facebook.net", "www.facebook.com",
    "static.hotjar.com", "script.hotjar.com",
    "cdn.segment.com", "js.hs-scripts.com",
}
RISKY_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".cab", ".tar", ".gz", ".bz2", ".xz",
    ".docm", ".xlsm", ".pptm", ".dotm", ".xlam", ".pdf",
}
SECURITY_HEADERS = {
    "content-security-policy": "Content-Security-Policy",
    "strict-transport-security": "Strict-Transport-Security",
    "x-frame-options": "X-Frame-Options",
    "x-content-type-options": "X-Content-Type-Options",
    "referrer-policy": "Referrer-Policy",
    "permissions-policy": "Permissions-Policy",
    "cross-origin-opener-policy": "Cross-Origin-Opener-Policy",
}
SUMMARY_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "are", "was", "were", "will", "would", "can", "could", "not", "you",
    "your", "our", "their", "they", "them", "but", "about", "into", "than",
    "then", "also", "more", "most", "other", "some", "such", "only", "over",
    "after", "before", "when", "where", "which", "who", "what", "how", "its",
    "it's", "all", "any", "may", "might", "must", "should", "been", "being",
    "use", "using", "used", "one", "two", "new", "now", "see", "here",
    "бол", "ба", "нь", "энэ", "тэр", "болон", "гэж", "гэсэн", "байна",
    "байгаа", "байсан", "хийх", "хийсэн", "нь", "юм", "бас", "дээр",
}
COOKIE_PATTERNS = (
    "cookie", "privacy policy", "terms of service", "all rights reserved",
    "subscribe", "newsletter", "sign in", "log in", "javascript",
)


@dataclass
class FetchResult:
    body: bytes
    final_url: str
    content_type: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    redirects: list[dict] = field(default_factory=list)

    def __iter__(self):
        # Kept for compatibility with callers/tests that unpack three values.
        yield self.body
        yield self.final_url
        yield self.content_type


class ContentFetchError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        *,
        final_url: str | None = None,
        content_type: str | None = None,
        redirects: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.final_url = final_url
        self.content_type = content_type
        self.redirects = redirects or []


def _connection(host: str, port: int, scheme: str) -> http.client.HTTPConnection:
    """Resolve once, reject unsafe answers, and connect only to validated IPs."""
    ips = resolve_public_host(host, port)
    if scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=settings.request_timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=settings.request_timeout)

    def connect_pinned(_address, timeout=None, source_address=None):
        last_error: OSError | None = None
        for ip in ips:
            try:
                return socket.create_connection(
                    (ip, port), timeout=timeout, source_address=source_address
                )
            except OSError as exc:
                last_error = exc
        raise last_error or OSError("No validated public IP could be reached")

    conn._create_connection = connect_pinned
    return conn


def _target(parsed) -> str:
    target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        target += "?" + quote(parsed.query, safe="/%:@!$&'()*+,;=-._~?&=")
    return target


def _host_key(value: str) -> str:
    try:
        host = (urlparse(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _selected_headers(response: http.client.HTTPResponse) -> dict[str, str]:
    names = (
        "content-type", "content-disposition", "content-length",
        "content-encoding", "server", "x-powered-by", *SECURITY_HEADERS,
    )
    headers: dict[str, str] = {}
    for name in names:
        value = response.getheader(name)
        if value:
            headers[name.lower()] = value.strip()
    return headers


def _decompress_body(body: bytes, content_encoding: str, max_bytes: int) -> bytes:
    """Decode a bounded response body without allowing a compression bomb."""
    encodings = [
        part.strip().lower()
        for part in (content_encoding or "").split(",")
        if part.strip()
    ]
    for encoding in reversed(encodings):
        if encoding == "identity":
            continue
        if encoding in {"gzip", "x-gzip"}:
            wbits = 16 + zlib.MAX_WBITS
        elif encoding == "deflate":
            wbits = zlib.MAX_WBITS
        else:
            raise ContentFetchError(
                f"Website uses an unsupported compression format ({encoding}).",
                415,
            )
        try:
            decoder = zlib.decompressobj(wbits)
            output = decoder.decompress(body, max_bytes + 1)
            if len(output) > max_bytes or decoder.unconsumed_tail:
                raise ContentFetchError(
                    "Website response is larger than the allowed limit.",
                    413,
                )
            output += decoder.flush()
            if len(output) > max_bytes:
                raise ContentFetchError(
                    "Website response is larger than the allowed limit.",
                    413,
                )
            if not decoder.eof:
                raise zlib.error("incomplete compressed stream")
        except zlib.error as exc:
            raise ContentFetchError(
                "Website sent invalid compressed data."
            ) from exc
        body = output
    return body


def _fetch(url: str, *, accept: str, max_bytes: int) -> FetchResult:
    current = url
    deadline = time.monotonic() + max(10, settings.request_timeout * 3)
    redirects: list[dict] = []
    for _ in range(5):
        parsed = parse_http_url(current)
        if parsed.username or parsed.password:
            raise UnsafeUrlError("URLs with embedded credentials are blocked.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        conn = _connection(parsed.hostname or "", port, parsed.scheme)
        try:
            conn.request(
                "GET", _target(parsed),
                headers={
                    "User-Agent": "TAYO/0.2 (+local verification tool)",
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                },
            )
            response = conn.getresponse()
            headers = _selected_headers(response)
            if response.status in REDIRECTS:
                location = response.getheader("Location")
                if not location:
                    raise ContentFetchError(
                        "Website redirect has no destination.",
                        final_url=current,
                        redirects=redirects,
                    )
                target = urljoin(current, location)
                redirects.append(
                    {
                        "status": response.status,
                        "from": current,
                        "to": target,
                        "cross_host": _host_key(target) != _host_key(current),
                    }
                )
                current = target
                continue
            if response.status != 200:
                raise ContentFetchError(
                    f"Website returned HTTP {response.status}.",
                    final_url=current,
                    redirects=redirects,
                )
            content_type = (
                response.getheader("Content-Type") or ""
            ).split(";", 1)[0].lower()
            length = response.getheader("Content-Length")
            if length and length.isdecimal() and int(length) > max_bytes:
                raise ContentFetchError(
                    "Website response is larger than the allowed limit.",
                    413,
                    final_url=current,
                    content_type=content_type,
                    redirects=redirects,
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise ContentFetchError(
                        "Website download timed out.",
                        504,
                        final_url=current,
                        content_type=content_type,
                        redirects=redirects,
                    )
                chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ContentFetchError(
                        "Website response is larger than the allowed limit.",
                        413,
                        final_url=current,
                        content_type=content_type,
                        redirects=redirects,
                    )
                chunks.append(chunk)
            try:
                body = _decompress_body(
                    b"".join(chunks),
                    headers.get("content-encoding", ""),
                    max_bytes,
                )
            except ContentFetchError as exc:
                exc.final_url = current
                exc.content_type = content_type
                exc.redirects = redirects
                raise
            return FetchResult(
                body=body,
                final_url=current,
                content_type=content_type,
                status=response.status,
                headers=headers,
                redirects=redirects,
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise ContentFetchError(f"Could not access website: {exc}") from exc
        finally:
            conn.close()
    raise ContentFetchError(
        "Website redirected too many times.",
        400,
        final_url=current,
        redirects=redirects,
    )


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.blocks: list[tuple[int, str]] = []
        self.headings: list[str] = []
        self.links: list[dict] = []
        self.assets: list[dict] = []
        self.forms: list[dict] = []
        self.frames: list[dict] = []
        self.script_sources: list[str] = []
        self.inline_scripts: list[str] = []
        self.meta_refresh: str | None = None
        self.base_href: str | None = None
        self.canonical: str | None = None
        self.lang: str | None = None
        self._stack: list[str] = []
        self._blocked = 0
        self._text: list[str] = []
        self._weight = 0
        self._script_depth = 0
        self._script_parts: list[str] = []
        self._current_form: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {str(k).lower(): (v or "") for k, v in attrs}
        if tag not in VOID_TAGS:
            self._stack.append(tag)
        if tag in BLOCK_TAGS:
            self._blocked += 1
        if tag in {"article", "main"}:
            self._weight += 4
        elif tag in {"p", "h1", "h2", "h3", "section"}:
            self._weight += 1

        if tag == "html":
            self.lang = attrs_map.get("lang") or self.lang
        elif tag == "base":
            href = attrs_map.get("href", "").strip()
            if href:
                self.base_href = href
        elif tag == "meta":
            key = (attrs_map.get("property") or attrs_map.get("name") or "").lower()
            value = attrs_map.get("content", "").strip()
            if key and value:
                self.meta[key] = value
            if attrs_map.get("http-equiv", "").lower() == "refresh":
                self.meta_refresh = value or None
            if key == "content-security-policy" and value:
                self.meta["meta_content_security_policy"] = value
        elif tag == "link":
            rel = attrs_map.get("rel", "").lower()
            href = attrs_map.get("href", "").strip()
            if href and "icon" in rel and "favicon" not in self.meta:
                self.meta["favicon"] = href
            if href and "canonical" in rel:
                self.canonical = href
        elif tag == "form":
            form = {
                "action": attrs_map.get("action", "").strip(),
                "method": (attrs_map.get("method") or "get").lower(),
                "password_field": False,
                "sensitive_fields": [],
                "hidden_fields": 0,
                "autocomplete": attrs_map.get("autocomplete", ""),
            }
            self.forms.append(form)
            self._current_form = form
        elif tag == "input":
            input_type = (attrs_map.get("type") or "text").lower()
            name = (attrs_map.get("name") or attrs_map.get("id") or "").lower()
            if self._current_form is not None:
                if input_type == "password":
                    self._current_form["password_field"] = True
                if input_type == "hidden":
                    self._current_form["hidden_fields"] += 1
                if any(term in name for term in SENSITIVE_FIELD_NAMES):
                    if name and name not in self._current_form["sensitive_fields"]:
                        self._current_form["sensitive_fields"].append(name[:80])
            if attrs_map.get("src"):
                self.assets.append(
                    {"kind": "input", "src": attrs_map["src"], "hidden": False}
                )
        elif tag == "a":
            href = attrs_map.get("href", "").strip()
            if href:
                self.links.append(
                    {
                        "href": href,
                        "download": "download" in attrs_map,
                        "text": "",
                    }
                )
        elif tag in {"iframe", "frame"}:
            src = attrs_map.get("src", "").strip()
            style = attrs_map.get("style", "").lower()
            width = (attrs_map.get("width") or "").strip()
            height = (attrs_map.get("height") or "").strip()
            hidden = (
                "display:none" in style.replace(" ", "")
                or "visibility:hidden" in style.replace(" ", "")
                or width in {"0", "1"}
                or height in {"0", "1"}
                or "hidden" in attrs_map
            )
            self.frames.append(
                {"kind": tag, "src": src, "hidden": hidden, "style": style}
            )
        elif tag == "object":
            data = attrs_map.get("data", "").strip()
            if data:
                self.frames.append(
                    {"kind": "object", "src": data, "hidden": False, "style": ""}
                )
        elif tag == "embed":
            src = attrs_map.get("src", "").strip()
            if src:
                self.frames.append(
                    {"kind": "embed", "src": src, "hidden": False, "style": ""}
                )
        elif tag in {"img", "source", "video", "audio", "track"}:
            src = (
                attrs_map.get("src")
                or attrs_map.get("data-src")
                or attrs_map.get("poster")
                or ""
            ).strip()
            if src:
                self.assets.append({"kind": tag, "src": src, "hidden": False})
        elif tag == "script":
            src = attrs_map.get("src", "").strip()
            if src:
                self.script_sources.append(src)
            if src or attrs_map.get("type", "").lower() == "application/ld+json":
                # JSON-LD is data, not executable, but it is useful metadata.
                if attrs_map.get("type", "").lower() == "application/ld+json":
                    self._script_depth += 1
                else:
                    self._script_depth += 1
            else:
                self._script_depth += 1
            if src:
                self._script_depth = max(0, self._script_depth - 1)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            text = "".join(self._script_parts).strip()
            if text and len(text) <= 100_000:
                self.inline_scripts.append(text)
            self._script_parts.clear()
            self._script_depth = max(0, self._script_depth - 1)
        if tag == "form":
            self._current_form = None
        if tag in TEXT_TAGS:
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and text:
                self.headings.append(text[:300])
            if len(text) >= 20:
                self.blocks.append((self._weight, text))
            self._text.clear()
        if tag in BLOCK_TAGS and self._blocked:
            self._blocked -= 1
        if tag in {"article", "main"}:
            self._weight = max(0, self._weight - 4)
        elif tag in {"p", "h1", "h2", "h3", "section"}:
            self._weight = max(0, self._weight - 1)
        if tag in self._stack:
            # Recover sensibly from imperfect real-world HTML while keeping
            # membership tests (title, article, blocked areas) accurate.
            index = len(self._stack) - 1 - self._stack[::-1].index(tag)
            del self._stack[index:]

    def handle_data(self, data: str) -> None:
        if self._script_depth:
            if sum(len(part) for part in self._script_parts) < 100_000:
                self._script_parts.append(data)
            return
        if self._blocked or not data.strip():
            return
        if "title" in self._stack:
            self.title_parts.append(data.strip())
        if any(tag in self._stack for tag in TEXT_TAGS):
            self._text.append(data.strip())


def _candidate_claims(blocks: list[tuple[int, str]], title: str) -> list[str]:
    """Pick concise assertions that can be checked independently."""
    candidates: list[tuple[int, int, str]] = []
    for order, (weight, block) in enumerate(blocks):
        for sentence in re.split(r"(?<=[.!?。！？])\s+", block):
            sentence = re.sub(r"\s+", " ", sentence).strip(" -•\t")
            words = sentence.split()
            if 6 <= len(words) <= 80 and 35 <= len(sentence) <= 600:
                candidates.append((weight, order, sentence))
    if title and len(title.split()) >= 5:
        candidates.append((6, -1, title))
    seen: set[str] = set()
    chosen: list[str] = []
    for _, _, claim in sorted(candidates, key=lambda item: (-item[0], item[1])):
        key = re.sub(r"\W+", " ", claim.casefold()).strip()
        if key and key not in seen:
            seen.add(key)
            chosen.append(claim)
        if len(chosen) == 5:
            break
    return chosen


def _decode(payload: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _preview_data(image_url: str) -> str | None:
    try:
        fetched = _fetch(
            image_url, accept="image/avif,image/webp,image/png,image/jpeg,image/gif",
            max_bytes=settings.max_preview_image_mb * 1024 * 1024,
        )
        raw, _, content_type = fetched
        if content_type not in RASTER_TYPES or not raw:
            return None
        return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return None


def _file_signature(body: bytes) -> str | None:
    if body.startswith(b"MZ"):
        return "Windows executable"
    if body.startswith(b"%PDF-"):
        return "PDF document"
    if body.startswith(b"PK\x03\x04"):
        return "ZIP or Office document"
    if body.startswith(b"\x7fELF"):
        return "Linux executable"
    if body.startswith(b"\xd0\xcf\x11\xe0"):
        return "Legacy Office document"
    return None


def _extension(value: str) -> str:
    try:
        path = urlparse(value).path.lower()
    except ValueError:
        return ""
    match = re.search(r"(\.[a-z0-9]{2,8})$", path)
    return match.group(1) if match else ""


def _is_http(value: str) -> bool:
    try:
        return urlparse(value).scheme == "http"
    except ValueError:
        return False


def _clean_sentence(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" -•\t")
    return value[:320]


def _summary_sentences(blocks: list[tuple[int, str]]) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    for order, (weight, block) in enumerate(blocks):
        for sentence in re.split(r"(?<=[.!?。！？])\s+", block):
            sentence = _clean_sentence(sentence)
            lowered = sentence.casefold()
            if len(sentence) < 45 or len(sentence.split()) < 6:
                continue
            if any(pattern in lowered for pattern in COOKIE_PATTERNS):
                continue
            candidates.append((weight, order, sentence))
    seen: set[str] = set()
    chosen: list[str] = []
    for _, _, sentence in sorted(candidates, key=lambda item: (-item[0], item[1])):
        key = re.sub(r"\W+", " ", sentence.casefold()).strip()
        if key and key not in seen:
            seen.add(key)
            chosen.append(sentence)
        if len(chosen) == 6:
            break
    return chosen


def _topics(title: str, headings: list[str], content: str) -> list[str]:
    words = re.findall(r"[^\W\d_]{3,}", f"{title} {' '.join(headings)} {content[:4000]}".lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in SUMMARY_STOPWORDS or len(word) < 4:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, count in ranked[:8] if count >= 2]


def _page_type(
    parser: _ArticleParser,
    content: str,
    forms: list[dict],
    downloads: list[dict],
) -> str:
    og_type = (parser.meta.get("og:type") or "").lower()
    path = urlparse(parser.canonical or "").path.lower()
    title = (parser.meta.get("og:title") or " ".join(parser.title_parts) or "").lower()
    if any(form.get("password_field") for form in forms):
        return "Login / account page"
    if "article" in og_type or parser.meta.get("article:published_time"):
        return "Article / news page"
    if "product" in og_type:
        return "Product / service page"
    if downloads:
        return "Download page"
    if any(term in title for term in ("404", "not found", "access denied", "forbidden")):
        return "Error / access page"
    if any(term in path for term in ("/login", "/signin", "/sign-in", "/account")):
        return "Login / account page"
    if "forum" in og_type or "discussion" in og_type:
        return "Forum / community page"
    words = len(content.split())
    if words < 120 and len(parser.links) >= 15:
        return "Landing / navigation page"
    return "Web page"


def _build_summary(
    parser: _ArticleParser,
    content: str,
    description: str,
    title: str,
) -> dict:
    sentences = _summary_sentences(parser.blocks)
    meta_description = parser.meta.get("description") or parser.meta.get("og:description") or ""
    meta_description = re.sub(r"\s+", " ", meta_description).strip()
    brief = meta_description if 45 <= len(meta_description) <= 600 else ""
    if not brief and sentences:
        brief = " ".join(sentences[:2])
    if not brief:
        brief = description or content[:320]
    key_points = [sentence for sentence in sentences if sentence not in brief][:4]
    if not key_points:
        key_points = [heading for heading in parser.headings[:4] if heading]
    words = len(re.findall(r"\S+", content))
    return {
        "brief": brief[:700],
        "key_points": key_points,
        "topics": _topics(title, parser.headings, content),
        "word_count": words,
        "reading_time_minutes": max(1, round(words / 220)) if words else 0,
        "source": "page metadata + extractive text",
        "note": (
            "Extractive overview from visible page text. It is not a live "
            "screenshot, a full article reproduction, or an endorsement."
        ),
    }


def _finding(
    findings: list[dict],
    identifier: str,
    severity: str,
    title: str,
    detail: str,
    evidence: str | list[str] | None = None,
) -> None:
    findings.append(
        {
            "id": identifier,
            "severity": severity,
            "title": title,
            "detail": detail,
            "evidence": evidence,
        }
    )


def _inspect_page(
    parser: _ArticleParser,
    *,
    content: str,
    final_url: str,
    headers: dict[str, str],
    body: bytes,
    content_type: str,
) -> dict:
    final_host = _host_key(final_url)
    findings: list[dict] = []

    forms: list[dict] = []
    for form in parser.forms:
        action_raw = form.get("action") or final_url
        action = urljoin(final_url, action_raw)
        action_host = _host_key(action)
        external = bool(action_host and final_host and action_host != final_host)
        entry = {
            "action": action,
            "method": form.get("method", "get"),
            "password_field": bool(form.get("password_field")),
            "sensitive_fields": form.get("sensitive_fields", []),
            "hidden_fields": form.get("hidden_fields", 0),
            "external_action": external,
        }
        forms.append(entry)
        if action.lower().startswith("javascript:"):
            _finding(
                findings, "javascript_form", "high",
                "Form submits through a javascript: URL",
                "A form action using javascript: can hide where credentials are sent.",
                action[:300],
            )
        elif entry["password_field"] and external:
            _finding(
                findings, "external_password_form", "critical",
                "Password form sends data to another domain",
                "The page asks for a password but submits it to a different host. "
                "This is a strong phishing pattern. Do not type a password.",
                action[:300],
            )
        elif entry["password_field"]:
            _finding(
                findings, "password_form", "caution",
                "This page contains a password field",
                "Login pages are normal, but verify the domain and certificate before "
                "typing credentials. TAYO cannot verify who receives the password.",
                action[:300],
            )
        elif external:
            _finding(
                findings, "external_form", "caution",
                "Form submits to a different domain",
                "Check that the destination belongs to the same organization before "
                "entering personal information.",
                action[:300],
            )
        if entry["sensitive_fields"]:
            _finding(
                findings, "sensitive_form", "caution",
                "Form asks for sensitive information",
                "Fields resemble credentials or payment data. Confirm the site is the "
                "official domain before entering anything.",
                ", ".join(entry["sensitive_fields"][:8]),
            )

    downloads: list[dict] = []
    seen_downloads: set[str] = set()
    for link in parser.links:
        href = (link.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        resolved = urljoin(final_url, href)
        extension = _extension(resolved)
        is_download = bool(link.get("download")) or extension in (
            EXECUTABLE_EXTENSIONS | RISKY_EXTENSIONS
        )
        if not is_download or resolved in seen_downloads:
            continue
        seen_downloads.add(resolved)
        severity = "high" if extension in EXECUTABLE_EXTENSIONS else "caution"
        downloads.append(
            {
                "url": resolved,
                "extension": extension or None,
                "explicit_download": bool(link.get("download")),
                "severity": severity,
            }
        )

    if downloads:
        executable = any(
            item["extension"] in EXECUTABLE_EXTENSIONS for item in downloads
        )
        _finding(
            findings,
            "download_link",
            "high" if executable else "caution",
            "Page links to downloadable files",
            "Opening or running an unexpected download can infect a device. "
            "Download only from a trusted source.",
            [item["url"][:300] for item in downloads[:8]],
        )

    meta_refresh = None
    if parser.meta_refresh:
        match = re.search(r"url\s*=\s*(.+)", parser.meta_refresh, re.I)
        target = match.group(1).strip().strip("'\"") if match else ""
        resolved = urljoin(final_url, target) if target else final_url
        meta_refresh = {"raw": parser.meta_refresh[:300], "target": resolved}
        extension = _extension(resolved)
        if _host_key(resolved) != final_host or extension:
            severity = "high" if extension in EXECUTABLE_EXTENSIONS else "caution"
            _finding(
                findings, "meta_refresh", severity,
                "The page redirects automatically",
                "An automatic redirect can hide the final destination or start a file "
                "download before you can inspect the page.",
                resolved[:300],
            )

    external_scripts: list[str] = []
    for src in parser.script_sources:
        resolved = urljoin(final_url, src)
        host = _host_key(resolved)
        if host and host != final_host and resolved not in external_scripts:
            external_scripts.append(resolved)

    inline = "\n".join(parser.inline_scripts)
    js_flags: list[str] = []
    js_patterns = {
        "eval": r"\beval\s*\(",
        "Function constructor": r"\bFunction\s*\(",
        "base64 decode": r"\batob\s*\(",
        "char-code decode": r"String\.fromCharCode\s*\(",
        "unescape": r"\bunescape\s*\(",
        "document.write": r"document\.write\s*\(",
        "location assignment": r"(?:window\.)?location(?:\.href)?\s*=",
        "window.open": r"window\.open\s*\(",
    }
    for label, pattern in js_patterns.items():
        if re.search(pattern, inline, re.I):
            js_flags.append(label)
    decode_flags = {"base64 decode", "char-code decode", "unescape", "eval"}
    common_flags = {
        "Function constructor", "document.write", "location assignment",
        "window.open",
    }
    if len(decode_flags & set(js_flags)) >= 2:
        _finding(
            findings, "obfuscated_javascript", "high",
            "Obfuscated JavaScript patterns found",
            "Combinations of eval, base64, character-code decoding, or unescape can "
            "hide malicious behavior. This is a heuristic, not proof.",
            ", ".join(js_flags),
        )
    elif decode_flags & set(js_flags):
        _finding(
            findings, "javascript_patterns", "caution",
            "JavaScript decoding pattern found",
            "A script decodes or evaluates data at runtime. This is common in "
            "applications, but also in obfuscated malicious pages.",
            ", ".join(js_flags),
        )
    elif common_flags & set(js_flags):
        _finding(
            findings, "javascript_common", "info",
            "Common JavaScript patterns present",
            "The page uses common browser APIs. These are not malicious by "
            "themselves; they are listed for transparency.",
            ", ".join(js_flags),
        )

    frames: list[dict] = []
    for frame in parser.frames:
        source = frame.get("src") or ""
        resolved = urljoin(final_url, source) if source else ""
        external = bool(resolved and _host_key(resolved) != final_host)
        frames.append(
            {
                "kind": frame.get("kind"),
                "src": resolved,
                "hidden": bool(frame.get("hidden")),
                "external": external,
            }
        )
        if frame.get("hidden") and _host_key(resolved) in TRACKER_HOSTS:
            _finding(
                findings, "tracking_frame", "info",
                "Known analytics or tag-manager frame",
                "The page loads a hidden frame from a well-known analytics or "
                "tag-manager host. This is common and not a phishing signal by itself.",
                resolved[:300],
            )
        elif frame.get("hidden"):
            _finding(
                findings, "hidden_frame", "caution",
                "Hidden or zero-size frame",
                "Hidden frames can load content without showing it. Some analytics "
                "and embeds do this legitimately, but it deserves a closer look.",
                resolved[:300],
            )
        elif external:
            _finding(
                findings, "external_frame", "info",
                "Page embeds content from another domain",
                "Embedded frames commonly show videos, maps, ads, or widgets. "
                "Check the source domain before interacting.",
                resolved[:300],
            )

    mixed_content: list[str] = []
    if final_url.startswith("https://"):
        for link in parser.links:
            resolved = urljoin(final_url, link.get("href") or "")
            if _is_http(resolved) and resolved not in mixed_content:
                mixed_content.append(resolved)
        for asset in parser.assets:
            resolved = urljoin(final_url, asset.get("src") or "")
            if _is_http(resolved) and resolved not in mixed_content:
                mixed_content.append(resolved)
        for frame in frames:
            if _is_http(frame.get("src") or "") and frame["src"] not in mixed_content:
                mixed_content.append(frame["src"])
    if mixed_content:
        _finding(
            findings, "mixed_content", "caution",
            "An HTTPS page loads HTTP resources",
            "Mixed content can be intercepted or modified in transit.",
            mixed_content[:5],
        )

    if parser.base_href:
        base = urljoin(final_url, parser.base_href)
        if _host_key(base) != final_host:
            _finding(
                findings, "external_base", "caution",
                "Page base URL points to another domain",
                "A base tag changes where relative links and forms are sent. "
                "This is uncommon and can be used to redirect interaction.",
                base[:300],
            )

    data_links = []
    for link in parser.links:
        href = (link.get("href") or "").strip()
        lowered = re.sub(r"\s+", "", href.lower())
        if not lowered.startswith(("data:", "javascript:")):
            continue
        if lowered in {"javascript:void(0)", "javascript:void(0);", "javascript:;", "javascript:#"}:
            continue
        resolved = href if lowered.startswith("data:") else urljoin(final_url, href)
        if resolved not in data_links:
            data_links.append(resolved)
    if data_links:
        _finding(
            findings, "embedded_link", "caution",
            "Page contains data: or javascript: links",
            "These links can hide content or behavior. Do not open them manually.",
            [item[:300] for item in data_links[:5]],
        )

    content_disposition = headers.get("content-disposition", "")
    if content_disposition.lower().startswith("attachment"):
        extension = _extension(final_url)
        severity = "high" if extension in EXECUTABLE_EXTENSIONS else "caution"
        _finding(
            findings, "attachment_response", severity,
            "The server sends this as a file download",
            "A link that immediately downloads a file is riskier than a normal page. "
            "Do not open unexpected files.",
            content_disposition[:300],
        )

    signature = _file_signature(body)
    if signature and content_type in {"text/html", "application/xhtml+xml"}:
        _finding(
            findings, "disguised_file", "critical",
            "An executable or document is disguised as a web page",
            "The response claims to be HTML but its file signature looks like a "
            "binary, archive, or document. Do not open it.",
            signature,
        )

    security_headers = {}
    for key, label in SECURITY_HEADERS.items():
        security_headers[key] = {
            "label": label,
            "present": bool(headers.get(key)),
            "value": headers.get(key),
        }
    if parser.meta.get("meta_content_security_policy"):
        security_headers["content-security-policy"]["present"] = True
        security_headers["content-security-policy"]["value"] = parser.meta[
            "meta_content_security_policy"
        ]

    page_type = _page_type(parser, content, forms, downloads)
    risk_points = sum(
        {"critical": 5, "high": 3, "caution": 1, "info": 0}.get(item["severity"], 0)
        for item in findings
    )
    risk_level = (
        "high" if risk_points >= 3
        else "medium" if risk_points >= 1
        else "low"
    )
    return {
        "page_type": page_type,
        "forms": forms,
        "downloads": downloads,
        "scripts": {
            "external": external_scripts[:20],
            "inline_count": len(parser.inline_scripts),
            "inline_characters": len(inline),
            "flags": js_flags,
        },
        "frames": frames,
        "mixed_content": mixed_content[:20],
        "meta_refresh": meta_refresh,
        "base_href": parser.base_href,
        "redirects": [],
        "security_headers": security_headers,
        "findings": findings,
        "risk_level": risk_level,
        "risk_points": risk_points,
        "note": (
            "Static HTML inspection only. TAYO does not execute JavaScript, media, "
            "or browser exploits, so it cannot prove that a page is safe from "
            "zero-click malware."
        ),
    }


def extract_web_content(url: str) -> dict:
    fetched = _fetch(
        url, accept="text/html,application/xhtml+xml;q=0.9",
        max_bytes=settings.max_webpage_mb * 1024 * 1024,
    )
    raw, final_url, content_type = fetched
    headers = getattr(fetched, "headers", {}) or {}
    redirects = getattr(fetched, "redirects", []) or []
    status = getattr(fetched, "status", 200)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ContentFetchError(
            f"URL did not return a web page (content-type: {content_type or 'unknown'}).",
            415,
            final_url=final_url,
            content_type=content_type,
            redirects=redirects,
        )
    parser = _ArticleParser()
    parser.feed(_decode(raw, content_type))
    ranked = sorted(enumerate(parser.blocks), key=lambda item: (-item[1][0], item[0]))
    chosen = [text for _, (_, text) in ranked[:80]]
    content = re.sub(r"\s+", " ", "\n\n".join(chosen)).strip()[:100_000]
    if len(content) < 40:
        raise ContentFetchError(
            "The page did not contain enough readable visible text.",
            422,
            final_url=final_url,
            content_type=content_type,
            redirects=redirects,
        )
    title = html.unescape(
        parser.meta.get("og:title") or parser.meta.get("twitter:title")
        or " ".join(parser.title_parts)
    ).strip()[:300]
    meta_description = html.unescape(
        parser.meta.get("description") or parser.meta.get("og:description") or ""
    ).strip()[:500]
    description = meta_description or content[:280]
    preview_ref = (
        parser.meta.get("og:image")
        or parser.meta.get("twitter:image")
        or parser.meta.get("preview_image")
    )
    preview_url = urljoin(final_url, preview_ref) if preview_ref else None
    favicon_ref = parser.meta.get("favicon")
    inspection = _inspect_page(
        parser,
        content=content,
        final_url=final_url,
        headers=headers,
        body=raw,
        content_type=content_type,
    )
    inspection["redirects"] = redirects
    summary = _build_summary(parser, content, description, title)
    language = parser.lang or detect_language(content)
    return {
        "title": title or (parse_http_url(final_url).hostname or final_url),
        "url": final_url,
        "description": description,
        "content": content,
        "content_characters": len(content),
        "preview_data_url": _preview_data(preview_url) if preview_url else None,
        "preview_source_url": preview_url,
        "appearance": {
            "kind": "safe_reconstruction",
            "headings": parser.headings[:6],
            "theme_color": parser.meta.get("theme-color"),
            "favicon_url": urljoin(final_url, favicon_ref) if favicon_ref else None,
            "note": "Reconstructed from static page metadata and visible text; not a live screenshot.",
        },
        "summary": summary,
        "inspection": inspection,
        "fetch": {
            "requested_url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "redirects": redirects,
            "headers": headers,
        },
        "claims": _candidate_claims(parser.blocks, title),
        "metadata": {
            "site_name": parser.meta.get("og:site_name"),
            "author": parser.meta.get("author") or parser.meta.get("article:author"),
            "published_time": parser.meta.get("article:published_time"),
            "og_type": parser.meta.get("og:type"),
            "language": language,
            "canonical": urljoin(final_url, parser.canonical) if parser.canonical else None,
        },
    }
