"""Combine URL reputation and static page inspection into user-facing guidance."""

from __future__ import annotations

import base64
from collections import Counter
from urllib.parse import quote


def _host_key(value: str) -> str:
    from urllib.parse import urlparse

    try:
        host = (urlparse(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _virustotal_gui_url(value: str) -> str:
    identifier = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return "https://www.virustotal.com/gui/url/" + identifier.rstrip("=")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def overall_safety(
    security: dict | None,
    website: dict | None = None,
    destination_security: dict | None = None,
) -> dict:
    """Return a plain-language open / sandbox / avoid recommendation.

    This is deliberately a triage summary, not a guarantee. It combines the
    URL reputation score with static page findings and the redirect target.
    """
    security = security or {}
    destination_security = destination_security or {}
    inspection = (website or {}).get("inspection") or {}
    findings = inspection.get("findings") or []
    security_checks = security.get("checks") or {}
    threat_intel = security_checks.get("threat_intel") or {}
    vt = threat_intel.get("virustotal") or {}
    destination_vt = (
        (destination_security.get("checks") or {}).get("threat_intel") or {}
    ).get("virustotal") or {}
    risk_score = max(
        int(security.get("risk_score") or 0),
        int(destination_security.get("risk_score") or 0),
    )
    statuses = {
        security.get("security_status"),
        destination_security.get("security_status"),
    }
    severity = Counter(item.get("severity") for item in findings)
    critical = [item for item in findings if item.get("severity") == "critical"]
    high = [item for item in findings if item.get("severity") == "high"]
    caution = [item for item in findings if item.get("severity") == "caution"]

    if "malicious" in statuses or critical or risk_score >= 60:
        level = "do_not_open"
    elif (
        "suspicious" in statuses
        or high
        or destination_vt.get("submitted")
        or risk_score >= 30
    ):
        level = "sandbox_only"
    elif caution or risk_score >= 10:
        level = "caution"
    elif website is None:
        level = "unknown"
    else:
        level = "no_strong_signal"

    reasons: list[str] = []
    reasons.extend(security.get("reasons") or [])
    reasons.extend(destination_security.get("reasons") or [])
    for item in critical + high + caution:
        title = item.get("title") or "Page warning"
        detail = item.get("detail") or ""
        reasons.append(f"{title}: {detail}" if detail else title)
    if destination_security.get("url") and _host_key(destination_security["url"]) != _host_key(
        security.get("url") or ""
    ):
        reasons.append(
            "The link redirects to a different domain: "
            f"{_host_key(destination_security['url'])}."
        )
    reasons = _unique(reasons)[:10]

    zero_click_note = (
        "Static inspection cannot prove a page is safe. It does not execute "
        "JavaScript, PDFs, media, or browser exploit code, so it cannot detect "
        "zero-click malware or a brand-new exploit. Keep the browser and OS "
        "patched, and use a disposable sandbox or VM for an untrusted link."
    )

    if level == "do_not_open":
        label = "Do not open this link"
        summary = (
            "Strong phishing, malware, or dangerous-file indicators were found. "
            "Do not open the page, enter credentials, or download anything."
        )
        actions = [
            "Do not open the link in your normal browser.",
            "Do not enter passwords, one-time codes, payment details, or wallet secrets.",
            "Do not download or run any file from this page.",
            "Verify the sender through a separate, known channel.",
            "If investigation is required, use a disposable VM or a separated sandbox service.",
        ]
    elif level == "sandbox_only":
        label = "Open only in an isolated sandbox"
        summary = (
            "The URL or page has risk indicators. It may still be legitimate, "
            "but do not open it with your normal logged-in browser profile."
        )
        actions = [
            "Use a disposable browser profile, VM, or isolated sandbox service.",
            "Do not sign in to any account or reuse an existing session.",
            "Do not allow downloads, pop-ups, clipboard access, or camera/microphone permissions.",
            "Confirm the organization through its official website or a known phone number.",
        ]
    elif level == "caution":
        label = "Proceed with caution"
        summary = (
            "No direct malware match was found, but the page has patterns that "
            "deserve checking before you trust it."
        )
        actions = [
            "Check the address bar and certificate details before entering anything.",
            "Do not submit personal data unless you reached the site from an official source.",
            "Avoid unexpected downloads and pop-ups.",
            "Use a sandbox if you need to inspect the page visually.",
        ]
    elif level == "unknown":
        label = "Could not fully inspect"
        summary = (
            "The URL checks ran, but the page content could not be inspected. "
            "This is not proof that the link is safe."
        )
        actions = [
            "Treat the link as unverified.",
            "Do not enter credentials or download files.",
            "Try a sandbox service or contact the sender through a known channel.",
        ]
    else:
        label = "No strong static threat signal"
        summary = (
            "No high-signal phishing or malware indicator was found by the static "
            "checks. This is not a guarantee that the site is safe."
        )
        actions = [
            "Confirm that the domain is the official one before entering credentials.",
            "Do not download unexpected files or install browser extensions.",
            "Keep the browser and operating system patched.",
            "Use a sandbox for any link you do not fully trust.",
        ]

    return {
        "level": level,
        "label": label,
        "summary": summary,
        "reasons": reasons,
        "actions": actions,
        "risk_score": risk_score,
        "finding_counts": {
            "critical": severity.get("critical", 0),
            "high": severity.get("high", 0),
            "caution": severity.get("caution", 0),
            "info": severity.get("info", 0),
        },
        "threat_intel": {
            "virustotal": vt.get("classification") or "not available",
            "virustotal_report_state": vt.get("report_state"),
            "safe_browsing": (
                "match"
                if (threat_intel.get("safe_browsing") or {}).get("matched")
                else "no match"
                if (threat_intel.get("safe_browsing") or {}).get("enabled")
                else "not configured"
            ),
            "urlhaus": (threat_intel.get("urlhaus") or {}).get("query_status")
            or "not available",
        },
        "zero_click_note": zero_click_note,
        "external_tools": {
            "virustotal_report": _virustotal_gui_url(security.get("url") or ""),
            "urlscan": (
                "https://urlscan.io/livescan/?url="
                + quote(
                    (website or {}).get("url")
                    or destination_security.get("url")
                    or security.get("url")
                    or "",
                    safe="",
                )
            ),
        },
        "note": (
            "This recommendation is a triage summary, not a forensic verdict. "
            "A low score can still be a targeted phishing site."
        ),
    }


def factcheck_overview(factchecks: list[dict] | None) -> dict:
    checks = factchecks or []
    counts = Counter(
        (item.get("verdict") or "Not Sure") for item in checks
    )
    checked = len(checks)
    if not checked:
        return {
            "checked": 0,
            "true": 0,
            "false": 0,
            "not_sure": 0,
            "status": "none",
            "headline": "No page claims were fact-checked.",
            "note": (
                "The page may still contain false or misleading information. "
                "Extraction only covers a few concise claims."
            ),
        }

    true_count = counts.get("True", 0)
    false_count = counts.get("False", 0)
    unsure_count = counts.get("Not Sure", 0)
    if false_count:
        status = "contradicted"
        headline = (
            f"{checked} claim(s) checked: {false_count} contradicted by existing "
            f"fact-checks, {true_count} supported, {unsure_count} unresolved."
        )
    elif true_count:
        status = "partly_supported"
        headline = (
            f"{checked} claim(s) checked: {true_count} supported by existing "
            f"fact-checks, {unsure_count} unresolved. This does not validate the "
            "whole page."
        )
    else:
        status = "unresolved"
        headline = (
            f"{checked} claim(s) checked, but no existing fact-check matched. "
            "Unresolved is not the same as true or false."
        )
    return {
        "checked": checked,
        "true": true_count,
        "false": false_count,
        "not_sure": unsure_count,
        "status": status,
        "headline": headline,
        "note": (
            "Each claim is checked independently. The page as a whole is not "
            "labeled true or false."
        ),
    }
