"""Multi-source retrieval for the Claim tab.

The Claim tab used to ask one search box about the raw sentence. This module
runs the queries produced by :mod:`claim_understanding` against several
independent sources and keeps the result of each query visible:

* published fact-checks (Google Fact Check Tools, FACTCHECK.mn, local index),
* medical literature (Europe PMC / PubMed records),
* trusted-source search links chosen from the registry.

It does not decide truth by itself. A published verdict is only adopted when
the retrieved review is close enough to the analysed claim, and every verdict
stays attributed to the publisher. Medical papers are shown as context unless
several of them explicitly contradict the claim. Everything else is
``Not Sure``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from ..config import settings
from .claim_understanding import claim_similarity, concept_keys
from .evidence_index import load_index, search as evidence_search
from .mongolian import has_negation, match_key
from .trusted_sources import source_for_url, source_search_links, tier_rank

logger = logging.getLogger(__name__)

GOOGLE_FACTCHECK_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
FACTCHECK_MN_URL = "https://factcheck.mn/wp-json/wp/v2/posts"
EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

MAX_QUERIES_PER_PROVIDER = 4
MAX_EVIDENCE = 30
MAX_GROUP = 12

FALSE_TERMS = (
    "false", "fake", "incorrect", "wrong", "misleading", "not true",
    "hoax", "scam", "fabricated", "altered", "miscaptioned", "manipulated",
    "unsubstantiated", "no evidence", "худал", "худлаа", "хуурамч",
    "ташаа", "үнэн биш", "hudal", "hudlaa", "төөрөгдүүл",
)
TRUE_TERMS = (
    "true", "correct", "accurate", "supported", "confirmed", "үнэн", "зөв",
    "unen", "баталгааж",
)
MIXED_TERMS = ("half", "partly", "mixed", "хэсэгчлэн", "хагас үнэн", "зарим нь")

# A publisher sometimes records the reviewed claim as "The claim 'X' is
# evidence-based". The rating then applies to that wrapper, not to X, so it
# must not be transferred to the user's sentence.
_META_CLAIM_MARKERS = (
    "the claim ",
    "the claim that",
    "the assertion",
    "the allegation",
    "claims that",
    "claim that",
    "the idea that",
    "is not an evidence-based claim",
    "is an evidence-based claim",
    "the rumor",
    "the rumour",
    "viral post",
    "social media post",
    "facebook post",
    "гэсэн мэдэгдэл",
    "гэх мэдэгдэл",
    "гэж мэдээлсэн",
    "гэсэн мэдээлэл",
)

CONTRADICTION_CUES = (
    "no evidence", "not effective", "ineffective", "lack of evidence",
    "no significant", "not associated", "no association", "failed to",
    "unsupported", "no benefit", "not recommended", "does not", "do not",
    "cannot", "no causal", "myth", "debunked", "нотолгоо байхгүй",
    "үр дүнгүй", "холбоогүй", "хамааралгүй",
)
# Medical abstracts routinely contain "not recommended" or "failed to" about a
# technique that has nothing to do with the user's claim. Only a title-level
# refutation is specific enough to count as a contradiction; everything else
# stays visible as context.
TITLE_CONTRADICTION_CUES = (
    "no evidence", "lack of evidence", "not effective", "ineffective",
    "no significant", "no association", "not associated", "unsupported",
    "no benefit", "myth", "debunk", "not recommended", "should not",
    "does not", "do not", "cannot", "not a cure", "no cure", "harmful",
    "dangerous", "нотолгоо байхгүй", "үр дүнгүй", "холбоогүй", "хамааралгүй",
)
SUPPORT_CUES = (
    "effective", "efficacy", "benefit", "reduces risk", "protective",
    "improves", "associated with", "increases risk", "causes",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    value = html.unescape(str(text))
    value = _TAG_RE.sub(" ", value)
    value = value.replace("\u00a0", " ")
    return _WS_RE.sub(" ", value).strip()


def rating_to_verdict(rating: str | None) -> str:
    """Map a publisher's own wording to the three labels this app uses."""
    value = (rating or "").lower()
    if any(term in value for term in MIXED_TERMS):
        return "Not Sure"
    if any(term in value for term in FALSE_TERMS):
        return "False"
    if any(term in value for term in TRUE_TERMS):
        return "True"
    return "Not Sure"


def _claim_concepts(analysis: dict[str, Any]) -> set[str]:
    return {item["key"] for item in analysis.get("concepts") or []}


def _polarity_mismatch(claim: str, candidate: str) -> bool:
    """True when one text negates and the other does not.

    A review of "vaccines cause autism" is not a review of "vaccines do not
    cause autism". The rating may even point in the opposite direction, so the
    conservative choice is to keep the review as context and not adopt it.
    """
    return has_negation(claim) != has_negation(candidate)


def _is_meta_claim(text: str) -> bool:
    lower = f" {_clean(text).lower()} "
    return any(marker in lower for marker in _META_CLAIM_MARKERS)


def _source_topics(analysis: dict[str, Any]) -> list[str]:
    """Concept keys/categories used only to rank source search links."""
    topics: list[str] = []
    for concept in analysis.get("concepts") or []:
        for item in (concept.get("key"), concept.get("category")):
            value = str(item or "").strip().lower()
            if value and value not in topics:
                topics.append(value)
    return topics


# Topic labels that are too common to establish that two sentences are about
# the same claim. "COVID" + "vaccine" matches thousands of unrelated reviews;
# "vaccine" + "autism" or "honey" + "cancer" does not. A retrieved fact-check
# needs at least one specific concept before its verdict can be adopted.
BROAD_CONCEPTS = {
    "vaccine",
    "covid",
    "flu",
    "infection",
    "virus",
    "bacteria",
    "immunity",
    "medication",
    "side_effect",
    "child",
    "pregnancy",
    "blood",
    "food",
    "vitamin",
}


def _relevance(
    analysis: dict[str, Any],
    text: str,
) -> tuple[float, int, int]:
    """Return (relevance, concept hits, specific hits).

    Cross-language pairs cannot rely on wording. Concept overlap is what keeps
    an English review of the same vaccine claim visible for a Mongolian post.
    Broad concepts are useful evidence of topic, but they cannot make a
    loosely related review look identical to the user's claim.
    """
    wanted = _claim_concepts(analysis)
    hits = concept_keys(text) & wanted
    specific = hits - BROAD_CONCEPTS
    similarity = claim_similarity(
        analysis.get("claim", ""),
        text,
        analysis.get("keywords") or [],
        polarity_aware=False,
    )
    if not wanted:
        return similarity, 0, 0
    if len(hits) >= 2 and specific:
        return max(similarity, 0.75), len(hits), len(specific)
    if len(hits) >= 2:
        # Broad-only overlap stays below the 0.60 verdict threshold. It can
        # still be shown as context, and a high wording score can lift it.
        return max(similarity, 0.55), len(hits), 0
    if len(hits) == 1 and specific:
        return max(similarity, 0.48), 1, 1
    if len(hits) == 1:
        return max(similarity, 0.35), 1, 0
    return similarity, 0, 0


def _medical_stance(
    title: str,
    abstract: str,
    hits: int,
    specific_hits: int | None = None,
) -> str:
    title_lower = title.lower()
    # Existing callers and tests used the total concept count; when a caller
    # has not supplied the specific count, keep the old behaviour.
    specific = hits if specific_hits is None else specific_hits
    if (
        hits >= 2
        and specific >= 1
        and any(cue in title_lower for cue in TITLE_CONTRADICTION_CUES)
    ):
        return "contradicts"
    abstract_lower = abstract.lower()
    if hits >= 1 and any(cue in abstract_lower for cue in SUPPORT_CUES):
        return "supports_context"
    return "context"


def _google_item(
    review: dict[str, Any],
    claim_text: str,
    query: str,
    analysis: dict[str, Any],
) -> dict[str, Any] | None:
    url = (review.get("url") or "").strip()
    if not url:
        return None
    rating = (review.get("textualRating") or "").strip()
    verdict = rating_to_verdict(rating)
    title = _clean(review.get("title")) or _clean(claim_text[:220]) or "Fact-check"
    publisher = _clean((review.get("publisher") or {}).get("name")) or "Fact-checker"
    checked_text = f"{claim_text} {title}"
    relevance, hits, specific_hits = _relevance(analysis, checked_text)
    polarity_text = claim_text.strip() or title
    polarity_mismatch = _polarity_mismatch(
        analysis.get("claim", ""), polarity_text
    )
    meta_claim = _is_meta_claim(polarity_text)
    if polarity_mismatch or meta_claim:
        relation = "fact_check" if verdict != "Not Sure" else "related"
    else:
        relation = (
            "contradicts"
            if verdict == "False"
            else "supports"
            if verdict == "True"
            else "related"
        )
    return {
        "title": title,
        "source": publisher,
        "url": url,
        "snippet": (
            f"Published rating: {rating or 'not stated'}. "
            f"Claim reviewed: {_clean(claim_text)[:320]}"
        ),
        "snippet_mn": (
            f"Нийтэлсэн үнэлгээ: {rating or 'тодорхойгүй'}. "
            f"Шалгасан мэдэгдэл: {_clean(claim_text)[:320]}"
        ),
        "relation": relation,
        "tier": "fact_check",
        "provider": "google_factcheck",
        "published": review.get("reviewDate"),
        "published_verdict": rating,
        "rating": rating,
        "query": query,
        "relevance": round(relevance, 3),
        "concept_hits": hits,
        "specific_hits": specific_hits,
        "polarity_mismatch": polarity_mismatch,
        "meta_claim": meta_claim,
        "not_used_for_verdict": bool(polarity_mismatch or meta_claim),
    }


async def _google_query(
    client: httpx.AsyncClient,
    query: str,
    analysis: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    key = settings.google_factcheck_api_key
    if not key:
        return [], "Google Fact Check API key is not configured."
    try:
        response = await client.get(
            GOOGLE_FACTCHECK_URL,
            params={"query": query, "key": key, "pageSize": 10},
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.info("Google Fact Check query failed: %s", exc)
        return [], str(exc)[:180]
    out: list[dict[str, Any]] = []
    for claim in data.get("claims") or []:
        claim_text = claim.get("text") or ""
        for review in claim.get("claimReview") or []:
            item = _google_item(review, claim_text, query, analysis)
            if item:
                out.append(item)
    return out, None


async def _factcheck_mn_query(
    client: httpx.AsyncClient,
    query: str,
    analysis: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = await client.get(
            FACTCHECK_MN_URL,
            params={
                "search": query,
                "per_page": 6,
                "_fields": "id,date,link,title,excerpt",
            },
        )
        response.raise_for_status()
        posts = response.json()
    except Exception as exc:
        logger.info("FACTCHECK.mn search failed: %s", exc)
        return [], str(exc)[:180]
    if not isinstance(posts, list):
        return [], "FACTCHECK.mn returned an unexpected response."
    out: list[dict[str, Any]] = []
    for post in posts:
        link = (post.get("link") or "").strip()
        title = _clean((post.get("title") or {}).get("rendered"))
        if not link or not title:
            continue
        excerpt = _clean((post.get("excerpt") or {}).get("rendered"))
        source = source_for_url(link) or {}
        full = f"{title} {excerpt}"
        relevance, hits, specific_hits = _relevance(analysis, full)
        polarity_mismatch = _polarity_mismatch(analysis.get("claim", ""), full)
        verdict = None
        markers = source.get("verdict_markers") or {}
        upper = title.upper()
        for marker, label in markers.items():
            if marker.upper() in upper:
                verdict = label
                break
        if polarity_mismatch:
            relation = "fact_check" if verdict else "related"
        else:
            relation = (
                "contradicts"
                if verdict and rating_to_verdict(verdict) == "False"
                else "supports"
                if verdict and rating_to_verdict(verdict) == "True"
                else "fact_check"
                if verdict
                else "related"
            )
        out.append(
            {
                "title": title,
                "source": source.get("name", "FACTCHECK.mn"),
                "url": link,
                "snippet": excerpt[:400],
                "snippet_mn": excerpt[:400],
                "relation": relation,
                "tier": "fact_check",
                "provider": "factcheck_mn",
                "published": post.get("date"),
                "published_verdict": verdict,
                "rating": verdict,
                "query": query,
                "relevance": round(relevance, 3),
                "concept_hits": hits,
                "specific_hits": specific_hits,
                "polarity_mismatch": polarity_mismatch,
                "not_used_for_verdict": bool(polarity_mismatch),
            }
        )
    return out, None


def _europe_pmc_item(
    record: dict[str, Any],
    query: str,
    analysis: dict[str, Any],
) -> dict[str, Any] | None:
    title = _clean(record.get("title"))
    if not title:
        return None
    abstract = _clean(record.get("abstractText"))
    journal = (
        _clean(record.get("journalTitle"))
        or _clean(((record.get("journalInfo") or {}).get("journal") or {}).get("title"))
    )
    authors = _clean(record.get("authorString"))
    source_id = record.get("source") or "MED"
    record_id = record.get("id") or record.get("pmid")
    doi = record.get("doi")
    if record_id:
        url = f"https://europepmc.org/article/{source_id}/{record_id}"
    elif doi:
        url = f"https://doi.org/{doi}"
    else:
        return None
    relevance, hits, specific_hits = _relevance(
        analysis, f"{title} {abstract[:1000]}"
    )
    stance = _medical_stance(title, abstract, hits, specific_hits)
    details = " · ".join(
        part for part in (journal, str(record.get("pubYear") or "")[:4], authors[:120]) if part
    )
    snippet = (abstract or details or "Medical literature record.")[:460]
    return {
        "title": title,
        "source": journal or "Europe PMC / PubMed",
        "url": url,
        "snippet": snippet,
        "snippet_mn": snippet,
        "relation": "contradicts" if stance == "contradicts" else "context",
        "stance": stance,
        "tier": "international",
        "provider": "europe_pmc",
        "published": str(record.get("pubYear") or ""),
        "published_verdict": None,
        "rating": None,
        "query": query,
        "relevance": round(relevance, 3),
        "concept_hits": hits,
        "specific_hits": specific_hits,
        "is_medical": True,
        "authors": authors,
        "journal": journal,
    }


async def _europe_pmc_query(
    client: httpx.AsyncClient,
    query: str,
    analysis: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = await client.get(
            EUROPEPMC_URL,
            params={
                "query": query,
                "format": "json",
                "pageSize": 6,
                "resultType": "core",
            },
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.info("Europe PMC query failed: %s", exc)
        return [], str(exc)[:180]
    out: list[dict[str, Any]] = []
    for record in ((data.get("resultList") or {}).get("result") or []):
        item = _europe_pmc_item(record, query, analysis)
        if item:
            out.append(item)
    return out, None


def _local_index_query(
    query: str,
    analysis: dict[str, Any],
    indexed: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        report = evidence_search(query, limit=5) if indexed is None else _search_loaded_index(indexed, query)
    except Exception as exc:
        logger.info("Local index search failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for item in report.get("results") or []:
        title = item.get("title") or ""
        snippet = item.get("snippet") or item.get("summary") or ""
        relevance, hits, specific_hits = _relevance(analysis, f"{title} {snippet}")
        relevance = max(relevance, float(item.get("match_score") or 0))
        verdict = item.get("published_verdict") or item.get("verdict")
        polarity_mismatch = _polarity_mismatch(
            analysis.get("claim", ""), f"{title} {snippet}"
        )
        if polarity_mismatch:
            relation = "fact_check" if verdict else "related"
        else:
            relation = (
                "contradicts"
                if verdict and rating_to_verdict(verdict) == "False"
                else "supports"
                if verdict and rating_to_verdict(verdict) == "True"
                else "fact_check"
                if verdict
                else "related"
            )
        out.append(
            {
                "title": title,
                "source": (
                    item.get("source")
                    or item.get("source_name")
                    or "Registered source"
                ),
                "url": item.get("url") or "",
                "snippet": snippet,
                "snippet_mn": snippet,
                "relation": relation,
                "tier": item.get("tier") or "news",
                "provider": "local_index",
                "published": item.get("published"),
                "published_verdict": verdict,
                "rating": verdict,
                "query": query,
                "relevance": round(relevance, 3),
                "concept_hits": hits,
                "specific_hits": specific_hits,
                "polarity_mismatch": polarity_mismatch,
                "not_used_for_verdict": bool(polarity_mismatch),
            }
        )
    return out


def _search_loaded_index(index: dict[str, Any], query: str) -> dict[str, Any]:
    """Search an already-loaded index without another file read.

    Kept small and local to avoid changing the public evidence_index API.
    """
    from rapidfuzz import fuzz

    items = index.get("items") or []
    if not items:
        return {"available": False, "results": []}
    claim_key = match_key(query)
    claim_tokens = {
        token for token in claim_key.split() if len(token) > 2
    }
    scored = []
    for item in items:
        item_tokens = {
            token
            for token in match_key(f"{item.get('title', '')} {item.get('summary', '')}").split()
            if len(token) > 2
        }
        if not claim_tokens or not item_tokens:
            continue
        overlap = len(claim_tokens & item_tokens) / len(claim_tokens)
        fuzzy = fuzz.token_set_ratio(claim_key, match_key(item.get("title") or "")) / 100.0
        score = round(0.6 * overlap + 0.4 * fuzzy, 4)
        if score >= 0.42:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], tier_rank({"tier": pair[1].get("tier")})))
    results = []
    for score, item in scored[:5]:
        results.append(
            {
                "title": item.get("title") or "",
                "source": item.get("source_name") or "Registered source",
                "url": item.get("url") or "",
                "snippet": (item.get("summary") or "")[:300],
                "published": item.get("published"),
                "tier": item.get("tier"),
                "published_verdict": item.get("verdict"),
                "match_score": score,
                "relation": "fact_check" if item.get("verdict") else "related",
            }
        )
    return {"available": True, "results": results}


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        url = item.get("url") or ""
        key = url or f"{item.get('source')}|{item.get('title')}"
        current = best.get(key)
        if current is None:
            best[key] = item
            continue
        priority = {"contradicts": 0, "supports": 1, "fact_check": 2, "context": 3, "related": 4}
        new_rank = (
            priority.get(item.get("relation", "related"), 5),
            -float(item.get("relevance") or 0),
            tier_rank({"tier": item.get("tier")}),
        )
        old_rank = (
            priority.get(current.get("relation", "related"), 5),
            -float(current.get("relevance") or 0),
            tier_rank({"tier": current.get("tier")}),
        )
        if new_rank < old_rank:
            best[key] = item
    return list(best.values())


def _sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {"contradicts": 0, "supports": 1, "fact_check": 2, "context": 3, "related": 4, "search": 5}
    return sorted(
        items,
        key=lambda item: (
            priority.get(item.get("relation", "related"), 5),
            -float(item.get("relevance") or 0),
            tier_rank({"tier": item.get("tier")}),
        ),
    )


def _aggregate_verdict(
    analysis: dict[str, Any],
    fact_checks: list[dict[str, Any]],
    medical: list[dict[str, Any]],
) -> tuple[str, float, str, dict[str, Any] | None]:
    strong = [
        item
        for item in fact_checks
        if item.get("published_verdict")
        and not item.get("polarity_mismatch")
        and not item.get("meta_claim")
        and float(item.get("relevance") or 0) >= 0.60
    ]
    false = [item for item in strong if rating_to_verdict(item["published_verdict"]) == "False"]
    true = [item for item in strong if rating_to_verdict(item["published_verdict"]) == "True"]
    claim_negated = has_negation(analysis.get("claim", ""))
    contradictions = [
        item
        for item in medical
        # A negated claim ("X does not cause Y") can be supported by the same
        # title-level "no evidence" wording that contradicts the positive form.
        # Do not let that ambiguity lower the verdict.
        if not claim_negated
        and item.get("relation") == "contradicts"
        and int(item.get("concept_hits") or 0) >= 2
        and int(item.get("specific_hits", item.get("concept_hits")) or 0) >= 1
    ]

    if false and not true:
        confidence = 0.82 if len(false) >= 2 else 0.72
        return "False", confidence, "published_fact_check", false[0]
    if true and not false:
        confidence = 0.80 if len(true) >= 2 else 0.68
        return "True", confidence, "published_fact_check", true[0]
    if false and true:
        return "Not Sure", 0.35, "conflicting_fact_checks", false[0]
    if len(contradictions) >= 2:
        return "False", 0.60, "medical_literature", None
    return "Not Sure", 0.28 if (fact_checks or medical) else 0.18, "no_verdict", None


def _summary(
    analysis: dict[str, Any],
    verdict: str,
    basis: str,
    reported: dict[str, Any] | None,
    fact_checks: list[dict[str, Any]],
    medical: list[dict[str, Any]],
    related: list[dict[str, Any]],
    source_links: list[dict[str, Any]],
) -> str:
    mongolian = str(analysis.get("language", "")).startswith("mn")
    queries = [q["query"] for q in (analysis.get("queries") or [])[:5]]
    query_note = "; ".join(queries) if queries else analysis.get("claim", "")

    if basis == "published_fact_check" and reported:
        if mongolian:
            return (
                f"{reported.get('source', 'Баримт шалгагч')} энэ мэдэгдэлтэй төстэй "
                f"агуулгыг «{reported.get('published_verdict') or reported.get('rating')}» "
                f"гэж үнэлсэн байна. TAYO тэр дүгнэлтийг эх сурвалжийнх нь хэвээр "
                f"харуулж байна: нийтлэлийг нээж, үг хэллэг болон хамрах хүрээг "
                f"тулгана уу. Хайлтад ашигласан: {query_note}."
            )
        return (
            f"{reported.get('source', 'A fact-checker')} published "
            f"«{reported.get('published_verdict') or reported.get('rating')}» on a "
            f"closely related claim. TAYO repeats that publisher's verdict rather "
            f"than claiming an independent judgment; open the review and compare "
            f"the wording and scope. Queries used: {query_note}."
        )
    if basis == "conflicting_fact_checks":
        return (
            "Олдсон баримт шалгалтууд зөрүүтэй дүгнэлт өгч байна. Тиймээс эцсийн "
            "дүгнэлтийг «Not Sure» гэж үлдээлээ. Холбоос бүрийг нээж, аль нь таны "
            "мэдэгдэлтэй яг таарч байгааг харна уу."
            if mongolian
            else "The retrieved fact-checks do not agree. The combined answer stays "
            "Not Sure; open the reviews and check which one addresses this exact claim."
        )
    if basis == "medical_literature":
        return (
            "Хэд хэдэн эрдэм шинжилгээний эх сурвалж мэдэгдлийг эсэргүүцэж байна. "
            "Энэ нь баримт шалгагчийн нийтэлсэн дүгнэлт биш, харин судалгааны "
            "эх сурвалжид үндэслэсэн хязгаарлагдмал дүгнэлт юм. Хураангуйг уншина уу."
            if mongolian
            else "Several medical literature records contradict the claim. This is a "
            "limited literature-based answer, not a published fact-check; read the abstracts."
        )

    if mongolian:
        if fact_checks or medical or related:
            return (
                f"Энэ мэдэгдэлтэй яг таарах нийтлэгдсэн дүгнэлт олдсонгүй. "
                f"{len(fact_checks)} баримт шалгалт, {len(medical)} эрдэм шинжилгээний "
                f"бүртгэл, {len(related)} холбоотой нийтлэл олдлоо. Эдгээр нь "
                f"мэдэгдлийг батлах эсвэл няцаах эцсийн үндэслэл биш. "
                f"Хайлтад ашигласан: {query_note}."
            )
        return (
            "Энэ мэдэгдэлтэй таарах баримт шалгалт эсвэл эрдэм шинжилгээний "
            "бүртгэл олдсонгүй. Энэ нь мэдэгдэл үнэн эсвэл худал гэсэн дүгнэлт биш. "
            f"Хайлтад ашигласан: {query_note}. Доорх эх сурвалжийн холбоосоор "
            "үргэлжлүүлэн шалгана уу."
        )
    if fact_checks or medical or related:
        return (
            f"No published verdict closely matching this claim was found. "
            f"Retrieved {len(fact_checks)} fact-check item(s), {len(medical)} medical "
            f"record(s), and {len(related)} related article(s). They are context, not "
            f"a verdict. Queries used: {query_note}."
        )
    return (
        "No matching fact-check or medical record was found. This is not a judgment "
        f"that the claim is true or false. Queries used: {query_note}. "
        "Use the trusted-source links below to continue checking."
    )


def _limitations(analysis: dict[str, Any], statuses: list[dict[str, Any]]) -> list[str]:
    limitations = [
        "A published verdict belongs to the publisher, not to TAYO. Similar wording is not proof that the two claims are identical.",
        "Medical literature is shown as context. Only two or more explicit contradicting records can affect the verdict, and full texts were not reviewed.",
        "The local index covers recent feed items only; an older fact-check on the same claim can be missed.",
        "Queries are generated from detected concepts, not from the whole pasted post. The exact queries are shown with the result.",
    ]
    if analysis.get("language") not in {"en"}:
        limitations.append(
            "Cross-language retrieval can miss Mongolian-language fact-checks or translate a concept differently."
        )
    for status in statuses:
        if status.get("status") == "skipped":
            limitations.append(f"{status.get('provider')}: {status.get('note')}")
        elif status.get("status") == "error":
            limitations.append(f"{status.get('provider')} failed: {status.get('note')}")
    return limitations


def _query_status(
    queries: list[dict[str, Any]],
    results_by_query: dict[str, int],
    errors_by_query: dict[str, str],
) -> list[dict[str, Any]]:
    out = []
    for query in queries:
        key = query["query"]
        error = errors_by_query.get(key)
        out.append(
            {
                "query": key,
                "purpose": query.get("purpose"),
                "language": query.get("language"),
                "providers": query.get("providers") or [],
                "result_count": results_by_query.get(key, 0),
                "status": "error" if error else "ok",
                "error": error,
            }
        )
    return out


async def retrieve_evidence(analysis: dict[str, Any]) -> dict[str, Any]:
    """Run the planned queries and return grouped, inspectable evidence."""
    queries = analysis.get("queries") or []
    fact_check_queries = [
        query for query in queries if query.get("purpose") == "fact_check"
    ][:MAX_QUERIES_PER_PROVIDER]
    medical_queries = [
        query for query in queries if query.get("purpose") == "medical"
    ][:MAX_QUERIES_PER_PROVIDER]

    google_queries = [
        query
        for query in fact_check_queries
        if "google_factcheck" in (query.get("providers") or [])
    ]
    mn_queries = [
        query
        for query in fact_check_queries
        if "factcheck_mn" in (query.get("providers") or [])
    ]

    statuses: list[dict[str, Any]] = []
    results_by_query: dict[str, int] = {}
    errors_by_query: dict[str, str] = {}
    collected: list[dict[str, Any]] = []

    index = load_index()
    index_available = bool(index.get("items"))

    async def run_provider(
        provider: str,
        query: str,
        coroutine_factory,
    ) -> None:
        items, error = await coroutine_factory()
        if error:
            errors_by_query[query] = error
        if items:
            collected.extend(items)
            results_by_query[query] = results_by_query.get(query, 0) + len(items)
        statuses.append(
            {
                "provider": provider,
                "query": query,
                "status": "error" if error else "ok",
                "results": len(items),
                "note": error,
            }
        )

    timeout = max(8.0, float(settings.request_timeout))
    semaphore = asyncio.Semaphore(8)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "TAYO/0.1 (claim retrieval; contact: local demo)"},
    ) as client:
        tasks = []

        if not settings.google_factcheck_api_key:
            statuses.append(
                {
                    "provider": "google_factcheck",
                    "status": "skipped",
                    "results": 0,
                    "note": "Google Fact Check API key is not configured.",
                }
            )
        else:
            for query in google_queries:
                async def _google(q=query["query"]):
                    async with semaphore:
                        return await _google_query(client, q, analysis)
                tasks.append(run_provider("google_factcheck", query["query"], _google))

        for query in mn_queries:
            async def _mn(q=query["query"]):
                async with semaphore:
                    return await _factcheck_mn_query(client, q, analysis)
            tasks.append(run_provider("factcheck_mn", query["query"], _mn))

        for query in medical_queries:
            async def _pmc(q=query["query"]):
                async with semaphore:
                    return await _europe_pmc_query(client, q, analysis)
            tasks.append(run_provider("europe_pmc", query["query"], _pmc))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Local index is synchronous and cheap; run it for the same query set.
    local_queries = [query["query"] for query in fact_check_queries]
    if index_available:
        for query in local_queries:
            items = _local_index_query(query, analysis, indexed=index)
            if items:
                collected.extend(items)
                results_by_query[query] = results_by_query.get(query, 0) + len(items)
        statuses.append(
            {
                "provider": "local_index",
                "status": "ok",
                "results": sum(
                    1 for item in collected if item.get("provider") == "local_index"
                ),
                "note": f"{len(index.get('items') or [])} indexed items",
            }
        )
    else:
        statuses.append(
            {
                "provider": "local_index",
                "status": "skipped",
                "results": 0,
                "note": "Local evidence index is empty; run scripts/refresh_evidence.py.",
            }
        )

    deduped = _dedupe(collected)
    fact_checks = _sort_items(
        [
            item
            for item in deduped
            if item.get("provider") in {"google_factcheck", "factcheck_mn", "local_index"}
            and (
                item.get("published_verdict")
                or item.get("tier") == "fact_check"
                or item.get("provider") == "factcheck_mn"
            )
            and not item.get("not_used_for_verdict")
        ]
    )[:MAX_GROUP]
    medical = _sort_items(
        [item for item in deduped if item.get("provider") == "europe_pmc"]
    )[:MAX_GROUP]
    fact_urls = {item.get("url") for item in fact_checks}
    medical_urls = {item.get("url") for item in medical}
    related = _sort_items(
        [
            item
            for item in deduped
            if item.get("url") not in fact_urls
            and item.get("url") not in medical_urls
            and item.get("relation") in {"related", "fact_check", "context"}
        ]
    )[:MAX_GROUP]

    source_links = source_search_links(
        analysis.get("claim", ""),
        scope=analysis.get("effective_scope", "auto"),
        limit=10,
        topics=_source_topics(analysis),
    )

    verdict, confidence, basis, reported = _aggregate_verdict(
        analysis, fact_checks, medical
    )
    summary = _summary(
        analysis,
        verdict,
        basis,
        reported,
        fact_checks,
        medical,
        related,
        source_links,
    )

    evidence = _sort_items([*fact_checks, *medical, *related, *source_links])[:MAX_EVIDENCE]
    return {
        "claim": analysis.get("claim"),
        "language": analysis.get("language"),
        "verdict": verdict,
        "confidence": confidence,
        "summary": summary,
        "summary_source": "template",
        "method": (
            "published_fact_check"
            if basis == "published_fact_check"
            else "medical_literature"
            if basis == "medical_literature"
            else "multi_source"
            if deduped
            else "no_match"
        ),
        "matched_claim": (
            (reported or {}).get("title") if basis == "published_fact_check" else None
        ),
        "match_score": float((reported or {}).get("relevance") or 0),
        "reported_verdict": (
            {
                "verdict": reported.get("published_verdict"),
                "rating": reported.get("rating"),
                "source": reported.get("source"),
                "url": reported.get("url"),
                "match_score": reported.get("relevance"),
                "note": (
                    "Published by that source about a closely related claim; "
                    "not this tool's independent verdict."
                ),
            }
            if reported
            else None
        ),
        "evidence": evidence,
        "evidence_groups": {
            "fact_checks": fact_checks,
            "medical": medical,
            "related": related,
            "source_links": source_links,
        },
        "analysis": analysis,
        "queries": _query_status(queries, results_by_query, errors_by_query),
        "source_status": statuses,
        "source_scope": analysis.get("effective_scope"),
        "index_status": {
            "available": index_available,
            "fetched_at": index.get("fetched_at"),
            "indexed_items": len(index.get("items") or []),
            "hits": sum(1 for item in collected if item.get("provider") == "local_index"),
        },
        "limitations": _limitations(analysis, statuses),
        "recommended_action": (
            "Open the strongest linked review and compare its exact claim, date, and context with the post you pasted."
            if evidence
            else "Try a shorter claim or select the medical scope to search health literature."
        ),
    }


def source_catalog(
    *,
    scope: str = "all",
    category: str | None = None,
    language: str | None = None,
    medical: bool | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Return the trusted-source registry for the Claim tab's source browser."""
    from .trusted_sources import list_sources, registry_summary

    sources = list_sources(
        scope=scope,
        category=category,
        language=language,
        medical=medical,
        query=query,
    )
    return {
        "sources": sources,
        "total": len(sources),
        **registry_summary(sources),
        "scope": scope,
    }
