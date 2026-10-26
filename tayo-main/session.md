# TAYO Session Handoff

Last updated: 2026-10-27T00:15:54+08:00

Working directory:

```text
/home/kalinka/repos/Initialization/git-hub-clones/tayo-main
```

## Status at a glance

- The URL checker is now implemented as a static **link dossier**, not just a
  scraped article preview.
- The backend is running on `127.0.0.1:8000` with the new code.
- All 59 unit tests pass.
- The fast smoke test passes 22/22 checks.
- The extension has **not** been visually verified in a browser because Node and
  Chromium are not installed in this environment.
- Changes are **uncommitted**. `tayo-main` is not its own Git root; the enclosing
  Git root is `/home/kalinka/repos/Initialization`.
- Reload the extension at `chrome://extensions` before testing the new Link UI.

## Goal of the work

Improve the Link tab so a user can understand:

- what a website contains,
- whether its main claims are supported or contradicted,
- whether the link looks like phishing, malware, or a dangerous download,
- what safer next step to take, especially with respect to zero-click threats.

TAYO must keep its existing extension/backend architecture, preserve the
connected VirusTotal-key workflow, and never execute page JavaScript or download
and run files during inspection.

## Implemented behavior

### `/api/check/url` workflow

1. Run URL reputation checks:
   - offline URL heuristics
   - DNS/DNSSEC over HTTPS
   - TLS certificate inspection
   - RDAP domain age
   - optional Safe Browsing, VirusTotal, URLhaus
2. If reputation is explicitly `malicious`, stop and never fetch the page.
3. Otherwise, fetch **static HTML only** with SSRF protection:
   - public-IP resolution and pinning
   - up to 5 redirects
   - response-size and timeout caps
   - bounded gzip/deflate decoding
4. Build a page dossier:
   - final URL and redirect chain
   - title, metadata, language, page type
   - extractive overview, key points, topics, word count
   - forms and password/sensitive fields
   - download links and executable extensions
   - frames/iframes, hidden frames
   - inline/external scripts and heuristic script flags
   - meta refresh, `<base href>`, mixed content
   - security headers
5. If the final host differs from the original host, rescan the destination
   with `submit_virustotal=False`.
6. Fact-check up to 3 concise page claims independently.
7. Return:
   - `overview`
   - `safety`
   - `destination_security`
   - `factchecks`
   - `factcheck_overview`
   - `website`
   - `security`

### Safety levels

`overall_safety()` returns one of:

- `do_not_open`
- `sandbox_only`
- `caution`
- `unknown`
- `no_strong_signal`

Each level includes a plain-language summary, reasons, concrete actions,
threat-intel status, external-tool links, and a zero-click limitation note.

### User-browser behavior

The user's browser does **not** open the checked site during analysis.

- The local backend makes the outbound request.
- No page JavaScript is executed.
- No PDFs or media are opened.
- No files are downloaded and run.
- The site sees the backend machine's IP and the TAYO user agent, not the user's
  browser cookies or session.

Exceptions:

- Clicking the displayed source URL opens the site in the browser.
- Clicking VirusTotal or urlscan.io leaves TAYO.
- urlscan.io may visit the target publicly.

## Files changed

### Backend

| File | Change |
|---|---|
| `backend/app/services/web_content.py` | Expanded static fetch, redirects, response headers, gzip/deflate decoding, metadata, summary, page type, forms, downloads, frames, scripts, mixed content, meta refresh, base href, security headers, findings. |
| `backend/app/services/link_dossier.py` | New. Safety recommendation levels/actions, threat-intel recap, external tools, zero-click note, fact-check overview. |
| `backend/app/services/url_scan.py` | Added optional VirusTotal submission. `scan_url(..., submit_virustotal=False)` supports destination rescans without auto-submission. Existing original-URL VT flow is preserved. |
| `backend/app/main.py` | `/api/check/url` now returns overview/safety/destination/fact-check overview; rescans cross-host destinations; preserves `blocked_malicious`; added `destination_flagged`. |

### Extension

| File | Change |
|---|---|
| `extension/panel.js` | Replaced the old combined URL renderer with safety, URL-security, overview, static-inspection, and fact-check cards. Added redirects, forms, downloads, scripts, findings, security headers, external tools. |
| `extension/panel.css` | Added styles for safety levels, findings, overview, redirect chains, inspection lists, header checks, topic chips, external tools, and the static-analysis note. |
| `extension/panel.html` | Updated Link-tab copy and added a visible static-analysis/zero-click warning. |

### Tests

| File | Change |
|---|---|
| `tests/test_web_content.py` | Added gzip/decompression tests, compressed fetch integration, dossier fixture, redirects, forms/password fields, downloads, hidden frames, scripts, security headers, tracker-frame false-positive regression. |
| `tests/test_url_check.py` | Added destination rescan without VT submission, `destination_flagged`, safety assertions, fact-check overview assertions. |
| `tests/test_link_dossier.py` | New. Safety levels, cross-host destination handling, zero-click disclosure, fact-check overview. |

### Scripts and docs

| File | Change |
|---|---|
| `scripts/smoke_test.py` | Replaced outdated “no page body fetched” check; added `/api/check/url` safety and inspection checks. |
| `README.md` | Updated Link feature description; added link-dossier and zero-click limits; removed outdated passive-scan claims. |
| `docs/ARCHITECTURE.md` | Rewrote URL pipeline; documented redirects, static-only inspection, safety levels, zero-click limitations, and new modules. |
| `docs/HOW_TO_RUN.md` | Added “What the Link tab inspects”; updated demo instructions. |

## False-positive tuning already applied

- Removed `.com` from executable extensions so TikTok profile paths are not
  treated as executable files.
- Known analytics/tag-manager hidden frames are informational.
- External frames are informational.
- Common JavaScript APIs are informational unless decoding/eval patterns exist.
- `javascript:void(0)` links are ignored.
- Download findings are aggregated into one finding per page.
- `python.org` works now because gzip-compressed HTML is decoded correctly.

## Verification performed

### Unit tests

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Result:

```text
Ran 59 tests in 0.543s
OK
```

### Compile check

```bash
.venv/bin/python -m py_compile \
  backend/app/main.py \
  backend/app/services/web_content.py \
  backend/app/services/link_dossier.py \
  backend/app/services/url_scan.py \
  scripts/smoke_test.py
```

Result: passed.

### Smoke test

```bash
.venv/bin/python scripts/smoke_test.py --skip-models
```

Result: `22 passed, 0 failed`.

### Live URL checks

Tested against the restarted backend on port 8000:

- `https://example.com` → `complete`, `no_strong_signal`
- `https://www.python.org` → `complete`, `no_strong_signal`
- `https://www.w3schools.com/html/html_forms.asp` → `complete`,
  `no_strong_signal` after false-positive tuning

## Running services

Backend:

```text
PID: 10239
URL: http://127.0.0.1:8000
Command: .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
Log: /tmp/tayo-backend-8000.log
```

Start/restart with:

```bash
./scripts/run_backend.sh
```

If port 8000 is occupied, check:

```bash
ss -ltnp | grep ':8000'
```

## Resume plan

1. Reload the extension at `chrome://extensions`.
2. Open the side panel and test the Link tab with:
   - `https://example.com`
   - `https://www.python.org`
   - a real news article
   - a shortener or redirect URL if available
3. Confirm the five cards render correctly:
   - Open or avoid
   - URL & domain security
   - Page overview
   - Static page inspection
   - Fact-check results
4. Check dark mode and the Mongolian/English language toggle.
5. Optionally improve UI numbering when a card is skipped because page
   extraction fails (the current numbering can have gaps).
6. Optionally update `docs/screenshots/url-scan.png`; the existing screenshot
   predates the dossier UI.

## Known limitations / caveats

- No browser-based visual verification was performed.
- `node --check extension/panel.js` could not run because Node is not installed.
  The JavaScript was reviewed manually.
- Static inspection cannot detect zero-click malware, PDF/browser exploit chains,
  or a site that becomes malicious only after login.
- Fact-checking is limited to a few concise claims. The bundled fact-check corpus
  is small, so many results are expected to be `Not Sure`.
- Cross-host redirect destinations are rescanned for reputation but not
  automatically submitted to VirusTotal.
- urlscan.io inspection is public; do not submit private or secret URLs.
- Backend logs in `/tmp` may disappear after a reboot.
- Changes are uncommitted.

## Earlier completed work in this session

The text-checker work was already merged into TAYO's existing Text tab before the
URL work. It includes:

- 18 descriptive checks across 14 groups
- quality gating
- 250-word chunking and coverage reporting
- experimental Mongolian scoring with no authorship verdict
- updated Text-tab UI/docs/tests

Relevant file: `backend/app/services/text_analysis.py`.

## Key commands

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Run smoke test:

```bash
.venv/bin/python scripts/smoke_test.py --skip-models
```

Check live URL dossier:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/check/url \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

Check backend health:

```bash
curl -sS http://127.0.0.1:8000/api/health
```
