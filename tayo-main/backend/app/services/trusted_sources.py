"""The registry of sources this build will quote, and what is known about each.

Two jobs. First, say whether a link a reader is looking at belongs to a source
worth starting from — a Facebook post that cites nso.mn is in a different
position from one that cites nothing. Second, supply the feeds that
evidence_index pulls from.

Nothing here decides whether a claim is true. A source's tier says what kind of
authority it has, not that a particular article of its is correct, and the
registry deliberately records a source's known slant (Montsame's state
ownership, for instance) rather than presenting every entry as neutral.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus, urlparse

from ..config import settings

# A tier is a claim about what the source *is*, used to order evidence, never
# to settle a question on its own.
TIER_RANK = {
    "fact_check": 0,
    "official": 1,
    "international": 2,
    "news": 3,
}

CATEGORY_LABELS = {
    "fact_check": ("Баримт шалгагч", "Fact-checker"),
    "health_authority": ("Эрүүл мэндийн байгууллага", "Health authority"),
    "medical_research": ("Анагаахын судалгаа", "Medical research"),
    "medical_reference": ("Эрүүл мэндийн лавлах", "Medical reference"),
    "university": ("Их сургууль, судалгаа", "University & research"),
    "public_safety": ("Онцгой байдал, аюулгүй байдал", "Public safety"),
    "environment": ("Байгаль орчин", "Environment"),
    "government": ("Засгийн газар", "Government"),
    "statistics": ("Статистик", "Statistics"),
    "law": ("Хууль эрх зүй", "Law"),
    "media": ("Мэдээлэл", "News media"),
    "international": ("Олон улсын эх сурвалж", "International source"),
}

_MEDICAL_CATEGORIES = {
    "health_authority",
    "medical_research",
    "medical_reference",
}
_MEDICAL_SCOPES = {
    "medical", "health", "public_health", "vaccines", "radiation",
    "nutrition", "maternal_child", "infectious_disease", "chronic_disease",
    "medication", "mental_health", "reproductive_health", "first_aid",
}

# Scopes that say what a source is, not what it covers. They keep a source
# eligible but do not demonstrate that it matches the claim's topic.
_GENERIC_SOURCE_SCOPES = {
    "medical", "health", "public_health", "fact_check", "research", "news",
    "policy", "science", "government", "education", "peer_reviewed",
    "systematic_review", "clinical_trials", "patient_education", "standards",
    "statistics", "demography", "development",
}

# Concept keys that need a topic alias to line up with the registry's scopes.
_TOPIC_SCOPE_ALIASES = {
    "blood": {"first_aid"},
    "suction": {"first_aid"},
    "honey": {"nutrition"},
    "food": {"nutrition"},
    "food_safety": {"food_safety", "nutrition"},
    "vitamin": {"nutrition"},
    "nuclear_power": {"radiation", "nuclear_safety"},
    "nuclear_waste": {"radiation", "nuclear_safety"},
    "meltdown": {"radiation", "nuclear_safety"},
    "air_pollution": {"public_health"},
    "pregnancy": {"maternal_child"},
    "child": {"maternal_child"},
}


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    path = settings.data_dir / "trusted_sources.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        return {
            "sources": [],
            "error": f"Trusted-source registry could not be loaded: {exc}",
        }
    if not isinstance(data.get("sources"), list):
        return {"sources": [], "error": "Registry has no 'sources' list."}
    return data


def _infer_category(source: dict[str, Any]) -> str:
    category = (source.get("category") or "").strip()
    if category:
        return category
    tier = source.get("tier")
    if tier == "fact_check":
        return "fact_check"
    if tier == "international":
        return "international"
    if tier == "official":
        return "government"
    return "media"


def normalize_source(source: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, public shape used by both lookups and the UI."""
    category = _infer_category(source)
    scopes = [str(scope) for scope in (source.get("scopes") or []) if scope]
    medical = source.get("medical")
    if medical is None:
        medical = category in _MEDICAL_CATEGORIES or bool(
            set(scopes) & _MEDICAL_SCOPES
        )
    domain = (source.get("domain") or "").strip().lower()
    homepage = source.get("homepage") or (f"https://{domain}" if domain else "")
    label_mn, label_en = CATEGORY_LABELS.get(
        category, (category.replace("_", " "), category.replace("_", " ").title())
    )
    return {
        "id": source.get("id"),
        "name": source.get("name"),
        "domain": domain,
        "tier": source.get("tier"),
        "language": source.get("language", "en"),
        "category": category,
        "category_label_mn": label_mn,
        "category_label_en": label_en,
        "medical": bool(medical),
        "scopes": scopes,
        "homepage": homepage,
        "search_url": source.get("search_url"),
        "feeds": source.get("feeds") or [],
        "feed_verified": bool(source.get("feed_verified")),
        "note": source.get("note"),
        "country": source.get("country"),
        "rank": source.get("rank", 50),
    }


def all_sources() -> list[dict]:
    return load_registry().get("sources", [])


def normalized_sources() -> list[dict]:
    return [normalize_source(source) for source in all_sources()]


def list_sources(
    *,
    scope: str | None = None,
    category: str | None = None,
    language: str | None = None,
    medical: bool | None = None,
    query: str | None = None,
) -> list[dict]:
    """Filter the registry without hiding why a source is included."""
    scope = (scope or "all").strip().lower()
    category = (category or "").strip().lower()
    language = (language or "").strip().lower()
    needle = (query or "").strip().lower()
    out: list[dict] = []
    for source in normalized_sources():
        if scope == "medical" and not source["medical"]:
            continue
        if scope == "fact_check" and source["category"] != "fact_check":
            continue
        if category and source["category"] != category:
            continue
        if language and source["language"] != language:
            continue
        if medical is not None and source["medical"] != medical:
            continue
        if needle:
            haystack = " ".join(
                str(source.get(key) or "")
                for key in ("name", "domain", "note", "category", "scopes")
            ).lower()
            if needle not in haystack:
                continue
        out.append(source)
    out.sort(
        key=lambda source: (
            source["category"] not in {"fact_check", "health_authority"},
            source["language"] != "mn",
            TIER_RANK.get(source.get("tier", ""), 50),
            source.get("name") or "",
        )
    )
    return out


def registry_summary(sources: list[dict] | None = None) -> dict[str, Any]:
    chosen = sources if sources is not None else normalized_sources()
    categories: dict[str, int] = {}
    languages: dict[str, int] = {}
    tiers: dict[str, int] = {}
    medical = 0
    for source in chosen:
        categories[source["category"]] = categories.get(source["category"], 0) + 1
        languages[source["language"]] = languages.get(source["language"], 0) + 1
        tiers[source["tier"]] = tiers.get(source["tier"], 0) + 1
        if source["medical"]:
            medical += 1
    return {
        "notice": load_registry().get("notice"),
        "counts": {
            "total": len(chosen),
            "medical": medical,
            "mongolian": languages.get("mn", 0),
            "categories": categories,
            "languages": languages,
            "tiers": tiers,
        },
        "categories": [
            {
                "id": key,
                "label_mn": CATEGORY_LABELS.get(key, (key, key))[0],
                "label_en": CATEGORY_LABELS.get(key, (key, key))[1],
                "count": count,
            }
            for key, count in sorted(
                categories.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
    }


def _expand_topics(topics) -> set[str]:
    wanted: set[str] = set()
    for topic in topics:
        key = str(topic or "").strip().lower().replace(" ", "_")
        if not key:
            continue
        wanted.add(key)
        wanted.update(_TOPIC_SCOPE_ALIASES.get(key, ()))
    return wanted


def _source_search_score(
    source: dict[str, Any],
    *,
    specific_overlap: int,
    broad_overlap: bool,
    mismatch: bool,
) -> float:
    category = source.get("category")
    language = source.get("language")
    score = 0.0
    if category == "fact_check":
        # A Mongolian fact-checker is the best first link for any claim; an
        # international one is still a better first stop than a general
        # reference work when the claim is about a circulating falsehood.
        score += 100.0 if language == "mn" else 30.0
    if specific_overlap:
        score += 50.0 + 5.0 * max(0, specific_overlap - 1)
    if language == "mn":
        score += 20.0
    if category in {"medical_research", "medical_reference"}:
        score += 25.0
    elif category == "health_authority":
        score += 15.0
    elif category in {"public_safety", "government", "university"}:
        score += 5.0
    if broad_overlap:
        score += 5.0
    if mismatch:
        # The source has a specialty, but it is not the claim's specialty.
        score -= 30.0
    if source.get("search_url"):
        score += 2.0
    score -= min(float(source.get("rank") or 50), 100.0) * 0.05
    return score


def _source_search_reason(
    source: dict[str, Any],
    overlap: set[str],
    mismatch: bool,
) -> tuple[str, str]:
    if overlap:
        labels = ", ".join(sorted(item.replace("_", " ") for item in overlap))
        return (
            f"Matches the detected topic ({labels}).",
            f"Илэрсэн сэдэвт тохирч байна ({labels}).",
        )
    category = source.get("category")
    if category == "fact_check":
        return (
            "General fact-check search; compare the exact wording.",
            "Ерөнхий баримт шалгалтын хайлт; үг хэллэгийг тулгана уу.",
        )
    if category in {"medical_research", "medical_reference"}:
        return (
            "General medical literature or reference search.",
            "Ерөнхий эрүүл мэндийн судалгаа эсвэл лавлагааны хайлт.",
        )
    if mismatch:
        return (
            "Specialist source outside the detected topic; may not be relevant.",
            "Илэрсэн сэдвээс өөр мэргэжлийн эх сурвалж; хамааралгүй байж болно.",
        )
    return (
        "Trusted source search.",
        "Итгэмжлэгдсэн эх сурвалжийн хайлт.",
    )


def source_search_links(
    query: str,
    *,
    scope: str = "all",
    limit: int = 10,
    topics: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build inspectable search links, ranked by topic overlap.

    ``topics`` should contain the detected concept keys and categories (for
    example ``honey`` and ``nutrition``). A national mental-health centre is a
    trusted source, but it is not a useful first place to search for a claim
    about honey and cancer, so specialist sources whose scopes do not overlap
    the claim are ranked below general medical and fact-checking sources.
    """
    needle = (query or "").strip()
    candidates = list_sources(scope=scope if scope != "auto" else "all")
    wanted = _expand_topics(topics or ())
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for source in candidates:
        source_scopes = {str(item) for item in (source.get("scopes") or [])}
        specific_scopes = source_scopes - _GENERIC_SOURCE_SCOPES
        overlap = specific_scopes & wanted
        mismatch = bool(specific_scopes) and not overlap
        score = _source_search_score(
            source,
            specific_overlap=len(overlap),
            broad_overlap=bool((source_scopes - specific_scopes) & wanted),
            mismatch=mismatch,
        )
        scored.append((score, len(overlap), source))
    scored.sort(
        key=lambda pair: (
            -pair[0],
            -pair[1],
            pair[2]["language"] != "mn",
            TIER_RANK.get(pair[2].get("tier", ""), 50),
            pair[2].get("rank", 50),
            pair[2].get("name") or "",
        )
    )

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, _, source in scored:
        if len(out) >= limit:
            break
        template = source.get("search_url")
        if template and needle:
            url = template.replace("{query}", quote_plus(needle))
        elif source.get("domain") and needle:
            url = (
                "https://www.google.com/search?q="
                + quote_plus(f"site:{source['domain']} {needle}")
            )
        else:
            url = source.get("homepage") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        name = source.get("name") or source.get("domain") or "Source"
        source_scopes = {str(item) for item in (source.get("scopes") or [])}
        specific_scopes = source_scopes - _GENERIC_SOURCE_SCOPES
        overlap = specific_scopes & wanted
        mismatch = bool(specific_scopes) and not overlap
        reason_en, reason_mn = _source_search_reason(source, overlap, mismatch)
        out.append(
            {
                "title": f"{name}: {needle}" if needle else name,
                "source": name,
                "source_id": source.get("id"),
                "url": url,
                "homepage": source.get("homepage"),
                "snippet": (
                    f"{reason_en} {source.get('note') or ''}"
                )[:400],
                "snippet_mn": (
                    f"{reason_mn} {source.get('note') or ''}"
                )[:400],
                "relation": "search",
                "tier": source.get("tier"),
                "provider": "source_search",
                "category": source.get("category"),
                "language": source.get("language"),
                "is_medical": source.get("medical", False),
                "matched_topics": sorted(overlap),
                "relevance": round(min(0.95, max(0.10, score / 120.0)), 3),
            }
        )
    return out


def _host_matches(host: str, domain: str) -> bool:
    """True for the domain itself and for any subdomain of it."""
    host = host.lower().strip(".")
    domain = domain.lower().strip(".")
    return host == domain or host.endswith("." + domain)


def source_for_url(url: str) -> dict | None:
    """Return the registry entry a URL belongs to, or None if unlisted."""
    try:
        host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    # Longest domain first, so factcheck.afp.com wins over a bare afp.com entry.
    for source in sorted(all_sources(), key=lambda s: -len(s.get("domain", ""))):
        if source.get("domain") and _host_matches(host, source["domain"]):
            return source
    return None


def describe_url(url: str) -> dict:
    """Say what is known about a link, for a reader deciding whether to trust it."""
    source = source_for_url(url)
    if not source:
        return {
            "listed": False,
            "url": url,
            "note": (
                "This domain is not in the registry. That is not a verdict on it — "
                "the list is short and deliberately conservative."
            ),
        }
    return {
        "listed": True,
        "url": url,
        "id": source["id"],
        "name": source["name"],
        "tier": source.get("tier"),
        "language": source.get("language"),
        "category": source.get("category"),
        "medical": source.get("medical"),
        "note": source.get("note"),
    }


def feeds() -> list[tuple[dict, str]]:
    """Every (source, feed_url) pair the index can ingest."""
    out: list[tuple[dict, str]] = []
    for source in all_sources():
        for url in source.get("feeds") or []:
            out.append((source, url))
    return out


def verdict_from_title(source: dict, title: str) -> str | None:
    """Read a fact-checker's own verdict out of its headline.

    Mongolian fact-check headlines put the verdict in capitals at the end
    ("… ТӨӨРӨГДҮҮЛСЭН"). The marker table per source is observed, not
    exhaustive: a headline with no known marker returns None rather than a
    guess, and the article still gets surfaced as related reading.
    """
    markers = source.get("verdict_markers") or {}
    upper = title.upper()
    for marker, verdict in markers.items():
        if marker.upper() in upper:
            return verdict
    return None


def tier_rank(source: dict | None) -> int:
    if not source:
        return 99
    return TIER_RANK.get(source.get("tier", ""), 50)
