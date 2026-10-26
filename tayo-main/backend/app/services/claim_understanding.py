"""Read a pasted post the way a careful fact-checker would.

The old Claim tab sent the whole sentence to a handful of search boxes. That
works for a pre-written corpus entry and poorly for real social-media text:
people quote, editorialise, add hashtags, and bury one assertion in a wall of
noise. This module turns that text into:

* the sentence(s) that actually make a checkable assertion,
* the medical/public-health concepts they mention,
* the polarity and claim shape (causal, risk, treatment, statistic, ...),
* short search queries for fact-checkers and medical literature.

Everything here is deterministic and inspectable. It is a query planner, not a
truth machine: it never assigns a verdict.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from rapidfuzz import fuzz

from .mongolian import detect_language, has_negation, match_key

# ---------------------------------------------------------------------------
# Cleaning and sentence selection
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"(?<!\w)[#@][\w\u0400-\u04ff]+", re.UNICODE)
_EMOJI_RE = re.compile(
    "[" "\U0001F000-\U0001FAFF" "\u2600-\u27BF" "\u2B00-\u2BFF" "\uFE0F" "\u200D" "]"
)
_WS_RE = re.compile(r"[ \t]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_WORD_RE = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)

_SOCIAL_CHROME = {
    "like",
    "comment",
    "share",
    "reply",
    "send",
    "save",
    "follow",
    "subscribe",
    "see more",
    "show more",
    "translate",
    "all reactions",
    "most relevant",
    "top comments",
    "suggested for you",
    "sponsored",
    "promoted",
    "facebook",
    "instagram",
    "tiktok",
    "twitter",
    "telegram",
    "whatsapp",
    "youtube",
    "shorts",
    "reels",
    "threads",
    "лайк",
    "сэтгэгдэл",
    "хуваалцах",
    "хариулах",
    "илгээх",
    "хадгалах",
    "дагах",
    "орчуулах",
    "бүгдийг харах",
    "дэлгэрэнгүй",
    "санал болгож буй",
    "сурталчилгаа",
    "спонсор",
    "реакц",
    "сэтгэгдэл бичих",
}

_STOPWORDS = {
    # English
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that",
    "this", "these", "those", "is", "are", "was", "were", "be", "been",
    "being", "to", "of", "in", "on", "for", "from", "with", "without", "by",
    "as", "at", "it", "its", "they", "them", "their", "we", "our", "you",
    "your", "he", "she", "his", "her", "not", "no", "never", "can", "could",
    "will", "would", "should", "may", "might", "must", "do", "does", "did",
    "have", "has", "had", "what", "why", "how", "when", "where", "who",
    "which", "also", "very", "more", "most", "much", "many", "some", "any",
    "all", "just", "only", "about", "there", "here", "true", "false", "claim",
    "said", "says", "people", "person", "fact", "check", "video", "photo",
    "image", "breaking", "news", "share", "like", "comment", "before",
    "gets", "deleted", "follow", "subscribe", "tag", "link", "bio", "click",
    "watch", "read", "more", "full", "story", "urgent", "shocking", "alert",
    # Mongolian (folded forms)
    "ba", "buyu", "ni", "n", "bol", "baina", "bna", "bn", "gej", "gedeg",
    "gesen", "ene", "ter", "ted", "enehuu", "ene", "ene", "tan", "tany",
    "minii", "mini", "nadad", "ch", "shuu", "uu", "ve", "yu", "yum", "yamar",
    "herev", "heree", "heere", "tegeed", "teheer", "tul", "tuls", "buh",
    "buhen", "olon", "davhar", "davhard", "ug", "ugeer", "ugui", "bish",
    "hun", "humuus", "huni", "hunuus", "medee", "medeelel", "zurag", "bichleg",
    "video", "post", "postoo", "share", "like", "comment", "bichsen", "bichjee",
    "yavj", "bgaa", "bga", "baih", "baigaa", "baisan", "bolno", "bolj",
    "huvaaltsaarai", "huvaaltsah", "laik", "daraarai", "bicheerei", "taraana",
    "taraaj", "delgerengui", "orchuul", "buhdiig", "uzee", "dar",
}

_SOCIAL_LINE_RE = re.compile(
    r"^(?:like|comment|share|reply|send|save|follow|subscribe|"
    r"лайк|сэтгэгдэл|хуваалцах|хариулах|илгээх|хадгалах|дагах)"
    r"(?:\s*[·•|]\s*[\w\u0400-\u04ff .:]+)*$",
    re.IGNORECASE,
)

# "Facebook дээр бичжээ:", "Эх сурвалж:" and similar lead-ins are not part
# of the assertion. They are common when someone copies a post into a chat.
_ATTRIBUTION_PREFIX_RE = re.compile(
    r"^(?:(?:facebook|fb|twitter|x|telegram|instagram|tiktok|youtube|"
    r"whatsapp|threads|reddit|фэйсбүүк|фэйсбук|твиттер|телеграм|"
    r"инстаграм|ютүб|тикток|реддит)\b[^:：]{0,60}[:：]\s*)"
    r"|^(?:(?:эх\s+сурвалж|source|via|according\s+to|пост|post|"
    r"бичлэг|video|зураг|screenshot|screenshots|мессеж|message|"
    r"мэдээ)\s*[:：]\s*)",
    re.IGNORECASE,
)

# "Share this", "Та үүнд итгэж байна уу?" and similar calls to action are
# appended to copied social posts but are not checkable claims.
_CTA_TAIL_RE = re.compile(
    r"(?:[.!?…]\s*|\s+)(?:та\s+)?(?:үүнд\s+)?"
    r"(?:итгэж\s+байна\s+уу|юу\s+гэж\s+бодож\s+байна\s+уу|"
    r"хуваалцаарай|тараана\s+уу|коммент\s+бичээрэй|"
    r"доор\s+коммент\s+бичээрэй|дараарай|бүгдээрээ\s+хуваалцъя|"
    r"share\s+this.*|comment\s+below.*|what\s+do\s+you\s+think.*|"
    r"tag\s+(?:a\s+)?friend.*|follow\s+(?:us|me).*)\s*[.!?…]*\s*$",
    re.IGNORECASE | re.DOTALL,
)

_QUOTE_PAIRS = (("“", "”"), ("«", "»"), ("„", "“"), ('"', '"'), ("'", "'"))
_TRAILING_SAY_RE = re.compile(r"\s+(?:гэж|гэсэн|гэв|гэжээ|гэлээ)\s*[.!?…]*$", re.IGNORECASE)


def _strip_attribution(value: str) -> str:
    result = value.strip()
    for _ in range(2):
        updated = _ATTRIBUTION_PREFIX_RE.sub("", result).strip()
        if updated == result:
            break
        result = updated
    return result


def _unwrap_quote(value: str) -> str:
    result = value.strip()
    for opener, closer in _QUOTE_PAIRS:
        if not result.startswith(opener):
            continue
        end = result.rfind(closer)
        if end <= 0:
            continue
        inner = result[len(opener):end].strip()
        tail = _TRAILING_SAY_RE.sub("", result[end + 1:]).strip(" \t.!?…")
        if inner and (not tail or tail in {"гэж", "гэсэн", "гэв", "гэжээ", "гэлээ"}):
            return inner
    return _TRAILING_SAY_RE.sub("", result).strip()


def clean_claim_text(text: str) -> str:
    """Turn one copied line into just the assertion, when possible.

    Unlike ``clean_social_text`` this removes the framing that surrounds a
    claim ("Facebook дээр бичжээ:", quote marks, "гэж", CTA text). It does not
    rewrite or translate the claim itself.
    """
    value = clean_social_text(text)
    value = _strip_attribution(value)
    value = _CTA_TAIL_RE.sub("", value).strip()
    value = _unwrap_quote(value)
    value = _strip_attribution(value)
    value = _CTA_TAIL_RE.sub("", value).strip()
    value = _TRAILING_SAY_RE.sub("", value).strip()
    return value.strip(" \t\n-–—•·|\"'“”«»„")


def _is_social_chrome(line: str) -> bool:
    value = line.strip()
    if not value:
        return True
    key = match_key(value)
    if key in _SOCIAL_CHROME:
        return True
    if _SOCIAL_LINE_RE.match(value):
        return True
    # "2h", "15 мин", "3 days ago" and similar timestamps.
    if re.fullmatch(
        r"\d+\s*(?:s|m|h|d|w|min|mins|hour|hours|day|days|week|weeks|"
        r"секунд|минут|цаг|өдөр|хоног|долоо хоног)",
        value,
        re.IGNORECASE,
    ):
        return True
    # A line of only punctuation or a single character is not a claim.
    if len(value) <= 2 and not value.isdigit():
        return True
    return False


def clean_social_text(text: str) -> str:
    """Remove links, social chrome, emoji and repeated whitespace.

    Hashtags are unwrapped rather than discarded: ``#вакцин`` can be the only
    spelling of the topic in a short post.
    """
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\u200b", " ").replace("\ufeff", " ")
    value = _URL_RE.sub(" ", value)
    value = _HASHTAG_RE.sub(lambda match: match.group(0)[1:], value)
    value = _EMOJI_RE.sub(" ", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")

    lines: list[str] = []
    for raw_line in value.split("\n"):
        line = raw_line.strip(" \t-–—•·|")
        if not line or _is_social_chrome(line):
            continue
        line = _WS_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    cleaned = "\n".join(lines) if lines else value.strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_sentences(text: str) -> list[str]:
    """Split on sentence punctuation and line breaks, keeping short posts whole."""
    value = clean_social_text(text)
    if not value:
        return []
    chunks = _SENTENCE_SPLIT_RE.split(value)
    return [chunk.strip(" \t-–—") for chunk in chunks if chunk.strip(" \t-–—")]


def _token_list(text: str) -> list[str]:
    return [token for token in _WORD_RE.findall(text or "") if token]


def _stem_key(token: str) -> str:
    """A deliberately light Mongolian suffix trim used only for deduplication."""
    key = match_key(token)
    if len(key) < 5:
        return key
    for suffix in (
        "iin", "yn", "nii", "ny", "aar", "eer", "oor", "oor", "aas", "ees",
        "oos", "oos", "tai", "tei", "toi", "gui", "gvi", "uud", "uul",
    ):
        if key.endswith(suffix) and len(key) - len(suffix) >= 4:
            return key[: -len(suffix)]
    return key


def keywords(text: str, limit: int = 14) -> list[str]:
    """Return readable content words, de-duplicated by a light folded stem."""
    out: list[str] = []
    seen: set[str] = set()
    for token in _token_list(text):
        key = _stem_key(token)
        if len(key) < 3 or key in _STOPWORDS:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= limit:
            break
    return out


def _keyword_phrase(text: str, limit: int = 4) -> str:
    return " ".join(keywords(text, limit=limit))


def claim_similarity(
    claim: str,
    candidate: str,
    keywords_list: list[str] | None = None,
    *,
    polarity_aware: bool = True,
) -> float:
    """Blend fuzzy wording, token overlap and optional keyword coverage.

    ``polarity_aware`` keeps the curated corpus from lending a verdict to a
    negated near-duplicate. Retrieval uses ``False`` because a related article
    is still worth showing even when its headline has the opposite polarity;
    the caller attributes the verdict explicitly in that case.
    """
    a, b = match_key(claim), match_key(candidate)
    if not a or not b:
        return 0.0
    if polarity_aware and has_negation(claim) != has_negation(candidate):
        return 0.0
    token_a = {token for token in a.split() if len(token) > 1}
    token_b = {token for token in b.split() if len(token) > 1}
    jaccard = (
        len(token_a & token_b) / max(1, len(token_a | token_b))
        if token_a and token_b
        else 0.0
    )
    fuzzy = fuzz.token_set_ratio(a, b) / 100.0
    keyword_hits = 0.0
    if keywords_list:
        wanted = {match_key(item) for item in keywords_list if match_key(item)}
        if wanted:
            keyword_hits = len(token_a & wanted) / len(wanted)
    return round(0.45 * fuzzy + 0.35 * jaccard + 0.20 * keyword_hits, 3)


# ---------------------------------------------------------------------------
# Medical / public-health concepts
# ---------------------------------------------------------------------------

# Each concept is a small bilingual lexicon. Aliases are folded through
# match_key() before matching, so one alias covers Cyrillic and informal Latin
# spellings as well as common suffixes.
_CONCEPTS: tuple[dict[str, Any], ...] = (
    {
        "key": "vaccine",
        "label_en": "vaccine",
        "label_mn": "вакцин",
        "category": "vaccines",
        "aliases": ("вакцин", "vaksin", "vaktsin", "vaccine", "vaccines", "vaccination", "vaccinate", "immunization", "иммунжуулалт"),
        "en_terms": ("vaccine", "vaccination"),
        "mn_terms": ("вакцин",),
        "medical": True,
    },
    {
        "key": "autism",
        "label_en": "autism",
        "label_mn": "аутизм",
        "category": "vaccines",
        "aliases": ("аутизм", "autism", "autistic", "autizm", "asd"),
        "en_terms": ("autism", "autism spectrum disorder"),
        "mn_terms": ("аутизм",),
        "medical": True,
    },
    {
        "key": "mmr",
        "label_en": "MMR / measles",
        "label_mn": "улаан бурхан / MMR",
        "category": "vaccines",
        "aliases": ("mmr", "measles", "mumps", "rubella", "улаан бурхан", "гахайн хавдар", "улаанбурхан"),
        "en_terms": ("measles", "MMR"),
        "mn_terms": ("улаан бурхан", "MMR"),
        "medical": True,
    },
    {
        "key": "radiation",
        "label_en": "radiation",
        "label_mn": "цацраг",
        "category": "radiation",
        "aliases": ("цацраг", "цацрагийн", "radiation", "radioactive", "ionizing", "цацраг идэвхт"),
        "en_terms": ("radiation", "radioactive"),
        "mn_terms": ("цацраг",),
        "medical": True,
    },
    {
        "key": "nuclear_power",
        "label_en": "nuclear power plant",
        "label_mn": "цөмийн цахилгаан станц",
        "category": "radiation",
        "aliases": ("цөмийн цахилгаан станц", "атомын цахилгаан станц", "цөмийн реактор", "nuclear power plant", "nuclear plant", "nuclear reactor", "nuclear power"),
        "en_terms": ("nuclear power plant", "nuclear", "radiation"),
        "mn_terms": ("цөмийн цахилгаан станц",),
        "medical": True,
    },
    {
        "key": "nuclear_waste",
        "label_en": "nuclear waste",
        "label_mn": "цөмийн хаягдал",
        "category": "radiation",
        "aliases": ("цөмийн хаягдал", "nuclear waste", "radioactive waste"),
        "en_terms": ("nuclear waste", "radioactive waste"),
        "mn_terms": ("цөмийн хаягдал",),
        "medical": True,
    },
    {
        "key": "meltdown",
        "label_en": "nuclear accident",
        "label_mn": "цөмийн осол",
        "category": "radiation",
        "aliases": ("цөмийн осол", "nuclear accident", "meltdown", "chernobyl", "fukushima", "чернобыль", "фукушима"),
        "en_terms": ("nuclear accident", "meltdown"),
        "mn_terms": ("цөмийн осол",),
        "medical": True,
    },
    {
        "key": "livestock",
        "label_en": "livestock",
        "label_mn": "мал амьтан",
        "category": "environmental_health",
        "aliases": ("мал", "малын", "мал амьтан", "livestock", "cattle", "sheep", "goats", "animals", "animal"),
        "en_terms": ("livestock", "animals"),
        "mn_terms": ("мал амьтан",),
        "medical": True,
    },
    {
        "key": "birth_defect",
        "label_en": "birth defect",
        "label_mn": "төрөлхийн гажиг",
        "category": "environmental_health",
        "aliases": ("төрөлхийн гажиг", "гажиг", "төрөлхийн", "хоёр толгой", "2 толгой", "birth defect", "birth defects", "congenital", "congenital abnormality", "malformation", "deformity", "two-headed"),
        "en_terms": ("birth defect", "congenital abnormality", "malformation"),
        "mn_terms": ("төрөлхийн гажиг",),
        "medical": True,
    },
    {
        "key": "water",
        "label_en": "water",
        "label_mn": "ус",
        "category": "environmental_health",
        "aliases": ("ус", "усны", "water", "river", "гол", "нуур", "lake"),
        "en_terms": ("water", "drinking water"),
        "mn_terms": ("ус",),
        "medical": True,
    },
    {
        "key": "algae_bloom",
        "label_en": "algal bloom",
        "label_mn": "замаг / ногоорох",
        "category": "environmental_health",
        "aliases": ("ногоорох", "ногоон", "замаг", "algae", "algal bloom", "cyanobacteria", "цэнхэр замаг"),
        "en_terms": ("algal bloom", "cyanobacteria"),
        "mn_terms": ("замаг", "ногоорох"),
        "medical": True,
    },
    {
        "key": "plant_growth",
        "label_en": "plants / crops",
        "label_mn": "ургамал / ургац",
        "category": "environmental_health",
        "aliases": ("ургамал", "ургац", "plants", "crops", "vegetation"),
        "en_terms": ("plants", "crops"),
        "mn_terms": ("ургамал",),
        "medical": True,
    },
    {
        "key": "pollution",
        "label_en": "pollution",
        "label_mn": "бохирдол",
        "category": "environmental_health",
        "aliases": ("бохирдол", "бохирдсон", "contamination", "contaminated", "pollution", "polluted"),
        "en_terms": ("pollution", "contamination"),
        "mn_terms": ("бохирдол",),
        "medical": True,
    },
    {
        "key": "air_pollution",
        "label_en": "air pollution",
        "label_mn": "агаарын бохирдол",
        "category": "environmental_health",
        "aliases": ("агаарын бохирдол", "утаа", "air pollution", "pm2.5", "smog"),
        "en_terms": ("air pollution", "particulate matter"),
        "mn_terms": ("агаарын бохирдол",),
        "medical": True,
    },
    {
        "key": "cancer",
        "label_en": "cancer",
        "label_mn": "хорт хавдар",
        "category": "chronic_disease",
        "aliases": ("хавдар", "хорт хавдар", "cancer", "tumor", "tumour", "carcinoma", "leukemia"),
        "en_terms": ("cancer", "tumor"),
        "mn_terms": ("хорт хавдар",),
        "medical": True,
    },
    {
        "key": "diabetes",
        "label_en": "diabetes",
        "label_mn": "чихрийн шижин",
        "category": "chronic_disease",
        "aliases": ("чихрийн шижин", "диабет", "diabetes", "diabetic"),
        "en_terms": ("diabetes",),
        "mn_terms": ("чихрийн шижин",),
        "medical": True,
    },
    {
        "key": "hypertension",
        "label_en": "high blood pressure",
        "label_mn": "цусны даралт",
        "category": "chronic_disease",
        "aliases": ("цусны даралт", "даралт", "hypertension", "blood pressure"),
        "en_terms": ("hypertension", "blood pressure"),
        "mn_terms": ("цусны даралт",),
        "medical": True,
    },
    {
        "key": "heart",
        "label_en": "heart / cardiovascular",
        "label_mn": "зүрх / зүрх судас",
        "category": "chronic_disease",
        "aliases": ("зүрх", "зүрхний", "heart", "cardiac", "cardiovascular"),
        "en_terms": ("cardiovascular", "heart"),
        "mn_terms": ("зүрх",),
        "medical": True,
    },
    {
        "key": "stroke",
        "label_en": "stroke",
        "label_mn": "тархины цус харвалт",
        "category": "chronic_disease",
        "aliases": ("тархины цус харвалт", "stroke"),
        "en_terms": ("stroke",),
        "mn_terms": ("тархины цус харвалт",),
        "medical": True,
    },
    {
        "key": "pregnancy",
        "label_en": "pregnancy",
        "label_mn": "жирэмсэн",
        "category": "maternal_child",
        "aliases": ("жирэмсэн", "жирэмсэн эх", "pregnancy", "pregnant", "prenatal", "antenatal"),
        "en_terms": ("pregnancy", "prenatal"),
        "mn_terms": ("жирэмсэн",),
        "medical": True,
    },
    {
        "key": "child",
        "label_en": "children",
        "label_mn": "хүүхэд",
        "category": "maternal_child",
        "aliases": ("хүүхэд", "хүүхдийн", "children", "child", "infant", "нярай", "baby"),
        "en_terms": ("children", "infant"),
        "mn_terms": ("хүүхэд",),
        "medical": True,
    },
    {
        "key": "medication",
        "label_en": "medicine / treatment",
        "label_mn": "эм / эмчилгээ",
        "category": "medication",
        "aliases": ("эмчилгээ", "эм тариа", "эмийн", "medicine", "medication", "drug", "treatment", "therapy"),
        "en_terms": ("treatment", "medication"),
        "mn_terms": ("эмчилгээ",),
        "medical": True,
    },
    {
        "key": "side_effect",
        "label_en": "side effect",
        "label_mn": "гаж нөлөө",
        "category": "medication",
        "aliases": ("гаж нөлөө", "гаж нөлөөтэй", "сөрөг нөлөө", "сөрөг нөлөөтэй", "сөрөг нөлөөний", "side effect", "side effects", "adverse reaction", "adverse event"),
        "en_terms": ("side effect", "adverse reaction"),
        "mn_terms": ("гаж нөлөө",),
        "medical": True,
    },
    {
        "key": "secret_list",
        "label_en": "secret / hidden list",
        "label_mn": "нууц жагсаалт",
        "category": "medical",
        "aliases": (
            "нууц жагсаалт", "нууц жагсаалттай", "нууц баримт",
            "secret list", "confidential list", "hidden list", "leaked list",
        ),
        "en_terms": ("secret list", "confidential list"),
        "mn_terms": ("нууц жагсаалт",),
        "medical": True,
    },
    {
        "key": "immunity",
        "label_en": "immunity",
        "label_mn": "дархлаа",
        "category": "infectious_disease",
        "aliases": ("дархлаа", "дархлааны", "immunity", "immune", "immune system", "natural immunity", "байгалийн дархлаа"),
        "en_terms": ("immunity", "immune system"),
        "mn_terms": ("дархлаа",),
        "medical": True,
    },
    {
        "key": "infection",
        "label_en": "infection",
        "label_mn": "халдвар",
        "category": "infectious_disease",
        "aliases": ("халдвар", "халдварын", "infection", "infectious", "infectious disease"),
        "en_terms": ("infection", "infectious disease"),
        "mn_terms": ("халдвар",),
        "medical": True,
    },
    {
        "key": "virus",
        "label_en": "virus",
        "label_mn": "вирус",
        "category": "infectious_disease",
        "aliases": ("вирус", "вирусын", "virus", "viral"),
        "en_terms": ("virus", "viral"),
        "mn_terms": ("вирус",),
        "medical": True,
    },
    {
        "key": "bacteria",
        "label_en": "bacteria",
        "label_mn": "бактери",
        "category": "infectious_disease",
        "aliases": ("бактери", "бактерийн", "bacteria", "bacterial"),
        "en_terms": ("bacteria", "bacterial"),
        "mn_terms": ("бактери",),
        "medical": True,
    },
    {
        "key": "antibiotic",
        "label_en": "antibiotic",
        "label_mn": "антибиотик",
        "category": "medication",
        "aliases": ("антибиотик", "antibiotic", "antibiotics", "antimicrobial"),
        "en_terms": ("antibiotic", "antimicrobial"),
        "mn_terms": ("антибиотик",),
        "medical": True,
    },
    {
        "key": "covid",
        "label_en": "COVID-19",
        "label_mn": "Ковид-19",
        "category": "infectious_disease",
        "aliases": ("ковид", "ковид-19", "covid", "covid-19", "coronavirus", "коронавирус", "sars-cov-2"),
        "en_terms": ("COVID-19", "SARS-CoV-2"),
        "mn_terms": ("ковид", "коронавирус"),
        "medical": True,
    },
    {
        "key": "flu",
        "label_en": "influenza",
        "label_mn": "томуу",
        "category": "infectious_disease",
        "aliases": ("томуу", "инфлюэнца", "influenza", "flu"),
        "en_terms": ("influenza", "flu"),
        "mn_terms": ("томуу",),
        "medical": True,
    },
    {
        "key": "tuberculosis",
        "label_en": "tuberculosis",
        "label_mn": "сүрьеэ",
        "category": "infectious_disease",
        "aliases": ("сүрьеэ", "tuberculosis", "tb"),
        "en_terms": ("tuberculosis",),
        "mn_terms": ("сүрьеэ",),
        "medical": True,
    },
    {
        "key": "hepatitis",
        "label_en": "hepatitis",
        "label_mn": "гепатит",
        "category": "infectious_disease",
        "aliases": ("гепатит", "hepatitis"),
        "en_terms": ("hepatitis",),
        "mn_terms": ("гепатит",),
        "medical": True,
    },
    {
        "key": "hiv",
        "label_en": "HIV / AIDS",
        "label_mn": "ХДХВ / ДОХ",
        "category": "infectious_disease",
        "aliases": ("хичээнгүй дархлал", "хдхв", "дох", "hiv", "aids", "спид"),
        "en_terms": ("HIV", "AIDS"),
        "mn_terms": ("ХДХВ", "ДОХ"),
        "medical": True,
    },
    {
        "key": "malaria",
        "label_en": "malaria",
        "label_mn": "маляри",
        "category": "infectious_disease",
        "aliases": ("маляри", "malaria"),
        "en_terms": ("malaria",),
        "mn_terms": ("маляри",),
        "medical": True,
    },
    {
        "key": "polio",
        "label_en": "polio",
        "label_mn": "полио",
        "category": "vaccines",
        "aliases": ("полио", "саа", "polio", "poliomyelitis"),
        "en_terms": ("polio", "poliomyelitis"),
        "mn_terms": ("полио",),
        "medical": True,
    },
    {
        "key": "infertility",
        "label_en": "infertility",
        "label_mn": "үргүйдэл",
        "category": "reproductive_health",
        "aliases": ("үргүйдэл", "үргүй", "infertility", "fertility", "sterility"),
        "en_terms": ("infertility", "fertility"),
        "mn_terms": ("үргүйдэл",),
        "medical": True,
    },
    {
        "key": "menstrual",
        "label_en": "menstruation",
        "label_mn": "сарын тэмдэг",
        "category": "reproductive_health",
        "aliases": ("сарын тэмдэг", "menstrual", "menstruation", "period"),
        "en_terms": ("menstruation", "menstrual cycle"),
        "mn_terms": ("сарын тэмдэг",),
        "medical": True,
    },
    {
        "key": "dna",
        "label_en": "DNA / genetics",
        "label_mn": "ДНХ / ген",
        "category": "medical",
        "aliases": ("dna", "ген", "генетик", "gene", "genetic", "genome"),
        "en_terms": ("DNA", "genetic"),
        "mn_terms": ("ДНХ", "ген"),
        "medical": True,
    },
    {
        "key": "microchip",
        "label_en": "microchip",
        "label_mn": "микрочип",
        "category": "medical",
        "aliases": ("микрочип", "чип", "microchip", "implant", "nanobot"),
        "en_terms": ("microchip", "implant"),
        "mn_terms": ("микрочип",),
        "medical": True,
    },
    {
        "key": "mercury",
        "label_en": "mercury / thimerosal",
        "label_mn": "мөнгөн ус / тиомерсал",
        "category": "vaccines",
        "aliases": ("мөнгөн ус", "mercury", "thimerosal", "тиомерсал"),
        "en_terms": ("mercury", "thimerosal"),
        "mn_terms": ("мөнгөн ус",),
        "medical": True,
    },
    {
        "key": "aluminum",
        "label_en": "aluminium",
        "label_mn": "хөнгөн цагаан",
        "category": "vaccines",
        "aliases": ("хөнгөн цагаан", "aluminum", "aluminium"),
        "en_terms": ("aluminium", "aluminum"),
        "mn_terms": ("хөнгөн цагаан",),
        "medical": True,
    },
    {
        "key": "formaldehyde",
        "label_en": "formaldehyde",
        "label_mn": "формальдегид",
        "category": "vaccines",
        "aliases": ("формальдегид", "formaldehyde"),
        "en_terms": ("formaldehyde",),
        "mn_terms": ("формальдегид",),
        "medical": True,
    },
    {
        "key": "fetal_cells",
        "label_en": "fetal cells",
        "label_mn": "ураг / үр хөндөлт",
        "category": "vaccines",
        "aliases": ("үр хөндөлт", "ураг", "fetal cells", "aborted fetus", "abortion"),
        "en_terms": ("fetal cells", "abortion"),
        "mn_terms": ("үр хөндөлт",),
        "medical": True,
    },
    {
        "key": "vitamin",
        "label_en": "vitamin / supplement",
        "label_mn": "витамин / нэмэлт",
        "category": "nutrition",
        "aliases": ("витамин", "нэмэлт тэжээл", "vitamin", "supplement"),
        "en_terms": ("vitamin", "dietary supplement"),
        "mn_terms": ("витамин",),
        "medical": True,
    },
    {
        "key": "ivermectin",
        "label_en": "ivermectin",
        "label_mn": "ивермектин",
        "category": "medication",
        "aliases": ("ивермектин", "ivermectin"),
        "en_terms": ("ivermectin",),
        "mn_terms": ("ивермектин",),
        "medical": True,
    },
    {
        "key": "hydroxychloroquine",
        "label_en": "hydroxychloroquine",
        "label_mn": "гидроксихлорохин",
        "category": "medication",
        "aliases": ("гидроксихлорохин", "hydroxychloroquine", "hcq"),
        "en_terms": ("hydroxychloroquine",),
        "mn_terms": ("гидроксихлорохин",),
        "medical": True,
    },
    {
        "key": "paracetamol",
        "label_en": "paracetamol",
        "label_mn": "парацетамол",
        "category": "medication",
        "aliases": ("парацетамол", "paracetamol", "acetaminophen"),
        "en_terms": ("paracetamol", "acetaminophen"),
        "mn_terms": ("парацетамол",),
        "medical": True,
    },
    {
        "key": "food",
        "label_en": "food / nutrition",
        "label_mn": "хоол / хүнс",
        "category": "nutrition",
        "aliases": ("хоол", "хүнс", "хоолны", "food", "nutrition", "diet"),
        "en_terms": ("nutrition", "diet"),
        "mn_terms": ("хоол", "хүнс"),
        "medical": True,
    },
    {
        "key": "food_safety",
        "label_en": "food safety",
        "label_mn": "хүнсний аюулгүй байдал",
        "category": "nutrition",
        "aliases": ("хоолны хордлого", "хүнсний аюулгүй байдал", "food poisoning", "food safety", "salmonella", "сальмонелла"),
        "en_terms": ("food safety", "food poisoning"),
        "mn_terms": ("хүнсний аюулгүй байдал",),
        "medical": True,
    },
    {
        "key": "honey",
        "label_en": "honey / bee products",
        "label_mn": "зөгийн бал / зөгийн бүтээгдэхүүн",
        "category": "nutrition",
        "aliases": (
            "зөгийн бал", "зөгийн балыг", "зөгийн балын", "зөгийн",
            "бал", "балыг", "балын", "honey", "propolis", "bee pollen",
            "royal jelly",
        ),
        "en_terms": ("honey", "propolis"),
        "mn_terms": ("зөгийн бал",),
        "medical": True,
    },
    {
        "key": "blood",
        "label_en": "blood / bleeding",
        "label_mn": "цус / цус алдалт",
        "category": "public_health",
        "aliases": (
            "цус", "цусыг", "цусны", "цусаа", "цус алдалт",
            "цус алдах", "цус гар", "blood", "bleeding", "hemorrhage",
            "haemorrhage", "blood loss",
        ),
        "en_terms": ("bleeding", "blood loss", "hemorrhage"),
        "mn_terms": ("цус", "цус алдалт"),
        "medical": True,
    },
    {
        "key": "first_aid",
        "label_en": "first aid / wound care",
        "label_mn": "анхны тусламж / шарх",
        "category": "public_health",
        "aliases": (
            "анхны тусламж", "шарх", "шархыг", "гэмтэл", "боолт",
            "first aid", "wound", "wound care", "injury", "tourniquet",
        ),
        "en_terms": ("first aid", "wound care"),
        "mn_terms": ("анхны тусламж", "шарх"),
        "medical": True,
    },
    {
        "key": "suction",
        "label_en": "suction / sucking",
        "label_mn": "сорох / хөхөх",
        "category": "medical",
        "aliases": (
            "сорох", "сорж", "соруулах", "хөхөх", "suction", "aspiration",
            "suck", "sucking",
        ),
        "en_terms": ("suction", "aspiration"),
        "mn_terms": ("сорох",),
        "medical": True,
    },
    {
        "key": "tobacco",
        "label_en": "tobacco",
        "label_mn": "тамхи",
        "category": "public_health",
        "aliases": ("тамхи", "тамхидалт", "tobacco", "smoking", "cigarette"),
        "en_terms": ("tobacco", "smoking"),
        "mn_terms": ("тамхи",),
        "medical": True,
    },
    {
        "key": "alcohol",
        "label_en": "alcohol",
        "label_mn": "архи",
        "category": "public_health",
        "aliases": ("архи", "архины", "alcohol", "drinking"),
        "en_terms": ("alcohol", "alcohol consumption"),
        "mn_terms": ("архи",),
        "medical": True,
    },
    {
        "key": "mental_health",
        "label_en": "mental health",
        "label_mn": "сэтгэцийн эрүүл мэнд",
        "category": "mental_health",
        "aliases": ("сэтгэцийн эрүүл мэнд", "сэтгэл гутрал", "mental health", "depression", "anxiety"),
        "en_terms": ("mental health", "depression"),
        "mn_terms": ("сэтгэцийн эрүүл мэнд",),
        "medical": True,
    },
    {
        "key": "traditional_medicine",
        "label_en": "traditional medicine",
        "label_mn": "уламжлалт эм",
        "category": "medication",
        "aliases": ("уламжлалт эм", "уламжлалт эмчилгээ", "traditional medicine", "herbal", "herb"),
        "en_terms": ("traditional medicine", "herbal medicine"),
        "mn_terms": ("уламжлалт эм",),
        "medical": True,
    },
    {
        "key": "fluoride",
        "label_en": "fluoride",
        "label_mn": "фтор",
        "category": "public_health",
        "aliases": ("фтор", "fluoride"),
        "en_terms": ("fluoride",),
        "mn_terms": ("фтор",),
        "medical": True,
    },
    {
        "key": "chlorine",
        "label_en": "chlorine",
        "label_mn": "хлор",
        "category": "public_health",
        "aliases": ("хлор", "chlorine"),
        "en_terms": ("chlorine",),
        "mn_terms": ("хлор",),
        "medical": True,
    },
    {
        "key": "5g",
        "label_en": "5G",
        "label_mn": "5G",
        "category": "public_health",
        "aliases": ("5g", "5 g", "5ж"),
        "en_terms": ("5G",),
        "mn_terms": ("5G",),
        "medical": True,
    },
    {
        "key": "depopulation",
        "label_en": "depopulation",
        "label_mn": "хүн ам цөөлөх",
        "category": "public_health",
        "aliases": ("хүн ам цөөлөх", "хүн амыг цөөлөх", "depopulation", "population reduction"),
        "en_terms": ("depopulation", "population reduction"),
        "mn_terms": ("хүн ам цөөлөх",),
        "medical": True,
    },
    {
        "key": "lockdown",
        "label_en": "lockdown",
        "label_mn": "хөл хорио",
        "category": "public_health",
        "aliases": ("хөл хорио", "lockdown", "quarantine"),
        "en_terms": ("lockdown", "quarantine"),
        "mn_terms": ("хөл хорио",),
        "medical": True,
    },
    {
        "key": "mask",
        "label_en": "face mask",
        "label_mn": "аман хаалт",
        "category": "public_health",
        "aliases": ("аман хаалт", "маск", "mask", "face mask"),
        "en_terms": ("face mask", "mask"),
        "mn_terms": ("аман хаалт",),
        "medical": True,
    },
)

_CONCEPT_BY_KEY = {concept["key"]: concept for concept in _CONCEPTS}

_CATEGORY_LABELS = {
    "vaccines": ("Вакцин", "Vaccines"),
    "radiation": ("Цацраг / цөмийн аюулгүй байдал", "Radiation & nuclear safety"),
    "environmental_health": ("Байгаль орчин ба эрүүл мэнд", "Environmental health"),
    "chronic_disease": ("Архаг өвчин", "Chronic disease"),
    "maternal_child": ("Эх, хүүхдийн эрүүл мэнд", "Maternal & child health"),
    "medication": ("Эм, эмчилгээ", "Medicine & treatment"),
    "infectious_disease": ("Халдварт өвчин", "Infectious disease"),
    "nutrition": ("Хоол, хүнс тэжээл", "Nutrition & food"),
    "public_health": ("Нийгмийн эрүүл мэнд", "Public health"),
    "mental_health": ("Сэтгэцийн эрүүл мэнд", "Mental health"),
    "reproductive_health": ("Нөхөн үржихүйн эрүүл мэнд", "Reproductive health"),
    "medical": ("Анагаах ухаан", "Medicine"),
}

_CAUSAL_TERMS = (
    "үүсгэдэг", "үүсгэдэггүй", "үүсгэсэн", "төрдөг", "төрүүлдэг",
    "болгодог", "болдог", "хүргэдэг", "учруулдаг", "шууд холбоотой",
    "cause", "causes", "caused", "leading to", "leads to", "lead to",
    "results in", "result in", "responsible for", "linked to", "link to",
)
_RISK_TERMS = (
    "эрсдэл", "эрсдэлтэй", "эрсдэл нэмэгд", "risk", "risks", "increased risk",
    "higher risk", "chance", "probability",
)
_PREVENTION_TERMS = (
    "урьдчилан сэргийл", "сэргийлдэг", "хамгаалдаг", "prevent", "prevents",
    "protect", "protects", "reduces the risk",
)
_TREATMENT_TERMS = (
    "эмчилдэг", "эмчилнэ", "эмчилгээ", "эдгээдэг", "эдгээнэ",
    "эдгээх", "эдгэрнэ", "арилгадаг", "арилгана", "устгадаг",
    "устгана", "гайхалтай", "cure", "cures", "cured", "heal",
    "heals", "healed", "reverse", "reverses", "treat", "treats",
    "treatment", "therapy",
)
_RECOMMENDATION_TERMS = (
    "зүгээр", "хэрэгтэй", "аюулгүй", "зөв", "болно", "хэрэггүй",
    "is safe", "is fine", "is okay", "is ok", "should", "recommended",
    "necessary", "harmless",
)
_CONSPIRACY_TERMS = (
    "хуйвалдаан", "нууц", "нууцлаг", "төр засаг нуу", "big pharma",
    "government hid", "they hide", "depopulation", "хүн ам цөөл",
)
_QUESTION_START = (
    "юу", "яагаад", "хэрхэн", "хэзээ", "хэн", "аль", "хэд", "үнэн үү",
    "what", "why", "how", "when", "who", "which", "is it", "are they",
    "does", "do ", "did ", "can ", "could ",
)


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    value = text.lower()
    return any(term in value for term in terms)


def _is_question(text: str) -> bool:
    value = text.strip().lower()
    if value.endswith("?"):
        return True
    return any(value.startswith(term) for term in _QUESTION_START)


# Folded Mongolian case endings, plus common English inflections. ``match_key``
# maps ы to i, so e.g. балын -> balin and хавдрыг -> havdrig. The suffix set
# lets a short lexicon alias such as "бал" or "хавдар" match those forms
# without a heavy stemmer.
_MATCH_SUFFIXES = frozenset(
    {
        "g", "d", "t", "n", "r", "i", "s", "es", "ed", "ing",
        "ig", "iig", "yg", "ii", "ni", "in", "iin", "ny", "nii",
        "aas", "ees", "oos", "oors", "aar", "eer", "oor",
        "tai", "tei", "toi", "gui", "gvi", "aa", "ee", "oo",
        "uud", "uul",
    }
)


def _alias_variants(alias_key: str) -> set[str]:
    """Return the alias plus a vowel-dropped stem for Mongolian stem changes.

    хавдар -> хавдр, which is what хавдрыг/хавдрын actually contain. The
    vowel-drop is skipped when it would leave less than three characters.
    """
    variants = {alias_key}
    words = alias_key.split()
    if not words:
        return variants
    last = words[-1]
    for index in range(len(last) - 2, -1, -1):
        if last[index] in "aeiouy":
            stem = last[:index] + last[index + 1:]
            if len(stem) >= 3:
                variants.add(" ".join([*words[:-1], stem]))
            break
    return variants


def _word_matches_alias(word: str, alias_key: str) -> bool:
    for base in _alias_variants(alias_key):
        if " " in base:
            continue
        if word == base:
            return True
        if not word.startswith(base) or len(word) == len(base):
            continue
        remainder = word[len(base):]
        if remainder in _MATCH_SUFFIXES:
            return True
        # Long aliases may take a compound ending not listed above, but the
        # remainder must stay short enough not to swallow a different word.
        if len(base) >= 5 and len(remainder) <= 3:
            return True
    return False


def find_concepts(text: str) -> list[dict[str, Any]]:
    """Find lexicon concepts in text, in order of first appearance."""
    key_text = match_key(text)
    if not key_text:
        return []
    words = key_text.split()
    padded = f" {key_text} "
    found: list[dict[str, Any]] = []
    for concept in _CONCEPTS:
        positions: list[int] = []
        matched: list[str] = []
        for alias in concept["aliases"]:
            alias_key = match_key(alias)
            if not alias_key:
                continue
            hit_positions: list[int] = []
            if " " in alias_key:
                for variant in _alias_variants(alias_key):
                    position = padded.find(f" {variant}")
                    if position != -1:
                        hit_positions.append(position)
            else:
                for word in words:
                    if _word_matches_alias(word, alias_key):
                        position = key_text.find(word)
                        if position != -1:
                            hit_positions.append(position)
            if hit_positions:
                matched.append(alias)
                positions.append(min(hit_positions))
        if matched:
            found.append(
                {
                    **concept,
                    "matched_terms": matched[:6],
                    "position": min(positions),
                }
            )
    found.sort(key=lambda item: (item["position"], item["key"]))
    return found


def concept_keys(text: str) -> set[str]:
    return {concept["key"] for concept in find_concepts(text)}


def _canonical_concepts(concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a broad parent concept when a more specific one is present."""
    keys = {concept["key"] for concept in concepts}
    dropped: set[str] = set()
    if "nuclear_power" in keys and "radiation" in keys:
        dropped.add("radiation")
    if "nuclear_waste" in keys and "radiation" in keys:
        dropped.add("radiation")
    return [concept for concept in concepts if concept["key"] not in dropped]


def _sentence_score(sentence: str, all_concepts: list[dict[str, Any]]) -> float:
    value = sentence.strip()
    if not value:
        return -10.0
    length = len(value)
    score = 0.0
    if 20 <= length <= 400:
        score += 2.0
    elif 10 <= length < 20:
        score += 1.0
    elif length > 700:
        score -= 1.0
    if re.search(r"\d", value):
        score += 0.4
    if sentence.endswith("?"):
        score += 0.3
    if _contains_any(value, _CAUSAL_TERMS):
        score += 1.2
    if _contains_any(value, _RISK_TERMS):
        score += 0.6
    if _contains_any(value, _PREVENTION_TERMS + _TREATMENT_TERMS):
        score += 0.5
    own_concepts = find_concepts(value)
    score += min(4.0, len(own_concepts) * 0.9)
    # Reward sentences that mention concepts from the post as a whole, which
    # helps when the subject is in one sentence and the effect in the next.
    all_keys = {concept["key"] for concept in all_concepts}
    if all_keys:
        score += min(2.0, len({c["key"] for c in own_concepts} & all_keys) * 0.4)
    if _is_social_chrome(value):
        score -= 4.0
    if value.upper() == value and len(value) > 12:
        score -= 0.2
    return score


def select_claims(text: str, limit: int = 3) -> list[str]:
    """Pick the most checkable sentences from a post, strongest first."""
    cleaned = clean_social_text(text)
    if not cleaned:
        return []
    sentences = [clean_claim_text(sentence) for sentence in split_sentences(cleaned)]
    sentences = [sentence for sentence in sentences if sentence]
    fallback = clean_claim_text(cleaned) or cleaned.replace("\n", " ")
    if not sentences:
        return [fallback[:800]]
    if len(sentences) == 1:
        return [sentences[0][:800]]
    all_concepts = find_concepts(" ".join(sentences))
    scored = sorted(
        ((_sentence_score(sentence, all_concepts), sentence) for sentence in sentences),
        key=lambda pair: (-pair[0], -len(pair[1])),
    )
    selected: list[str] = []
    for score, sentence in scored:
        if score < 0 or len(sentence) < 8:
            continue
        if any(
            claim_similarity(sentence, existing, polarity_aware=False) > 0.92
            for existing in selected
        ):
            continue
        selected.append(sentence[:800])
        if len(selected) >= limit:
            break
    if not selected:
        selected = [fallback[:800]]
    # For a very short post, keeping the whole post gives retrieval the subject
    # even if the highest-scoring sentence is a fragment.
    if len(cleaned) <= 320 and len(selected) == 1 and len(selected[0]) < 60:
        selected[0] = fallback[:800]
    return selected


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------

_PROVIDER_LABELS = {
    "google_factcheck": ("Google Fact Check", "Google Fact Check"),
    "factcheck_mn": ("FACTCHECK.mn", "FACTCHECK.mn"),
    "europe_pmc": ("PubMed / Europe PMC", "PubMed / Europe PMC"),
    "source_search": ("Эх сурвалжийн хайлт", "Trusted-source search"),
}

_PURPOSE_LABELS = {
    "fact_check": ("Баримт шалгалт", "Fact-check search"),
    "medical": ("Эрдэм шинжилгээний эх сурвалж", "Medical literature"),
    "web": ("Эх сурвалж хайх", "Source search"),
}


def _query(
    query: str,
    purpose: str,
    language: str,
    providers: list[str],
    direct: bool,
    concept_keys: list[str],
) -> dict[str, Any]:
    return {
        "query": query,
        "purpose": purpose,
        "language": language,
        "providers": providers,
        "direct": direct,
        "concept_keys": concept_keys,
    }


def _dedupe_queries(queries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        key = query["query"].strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(query)
        if len(out) >= limit:
            break
    return out


def _concept_terms(concepts: list[dict[str, Any]], language: str) -> list[str]:
    terms: list[str] = []
    for concept in concepts:
        values = concept.get("mn_terms" if language == "mn" else "en_terms") or ()
        if values:
            terms.append(values[0])
    return terms


# A broad parent term such as "water" or "children" is often present only as
# context. When the query budget is small, the more specific concepts should
# survive: birth defect, not water; blood, not first aid.
_GENERIC_QUERY_KEYS = {
    "water", "food", "child", "pregnancy", "livestock", "plant_growth",
    "pollution", "infection", "virus", "bacteria", "medication", "dna",
    "first_aid", "covid", "vaccine", "side_effect",
}

# Concepts that help identify a claim but do not describe a biomedical topic.
# They belong in fact-check queries, not in Europe PMC search strings.
_NON_BIOMEDICAL_QUERY_KEYS = {"secret_list"}


def _query_concepts(concepts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        enumerate(concepts),
        key=lambda pair: (pair[1]["key"] in _GENERIC_QUERY_KEYS, pair[0]),
    )
    return [concept for _, concept in ranked[:limit]]


def _build_fact_check_queries(
    claim: str,
    concepts: list[dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    if language == "en":
        selected = _query_concepts(concepts, 4)
        terms = _concept_terms(selected, "en")[:4] or [claim]
        keys = [concept["key"] for concept in selected[: len(terms)]]
        primary = " ".join(terms)
        queries.append(
            _query(primary, "fact_check", "en", ["google_factcheck"], True, keys)
        )
        if _contains_any(claim, _TREATMENT_TERMS):
            queries.append(
                _query(
                    f"{primary} cure",
                    "fact_check",
                    "en",
                    ["google_factcheck"],
                    True,
                    keys,
                )
            )
        short = " ".join(terms[:2])
        if short and short != primary:
            queries.append(
                _query(short, "fact_check", "en", ["google_factcheck"], True, keys[:2])
            )
        return _dedupe_queries(queries, 3)

    selected_mn = _query_concepts(concepts, 3)
    mn_terms = _concept_terms(selected_mn, "mn")[:3]
    keys_mn = [concept["key"] for concept in selected_mn[: len(mn_terms)]]
    primary = " ".join(mn_terms) if mn_terms else _keyword_phrase(claim, 4)
    if primary:
        queries.append(
            _query(
                primary,
                "fact_check",
                "mn",
                ["google_factcheck", "factcheck_mn"],
                True,
                keys_mn,
            )
        )
    selected_en = _query_concepts(concepts, 4)
    en_terms = _concept_terms(selected_en, "en")[:4]
    keys_en = [concept["key"] for concept in selected_en[: len(en_terms)]]
    if en_terms:
        queries.append(
            _query(
                " ".join(en_terms),
                "fact_check",
                "en",
                ["google_factcheck"],
                False,
                keys_en,
            )
        )
    if _contains_any(claim, _TREATMENT_TERMS) and primary:
        queries.append(
            _query(
                f"{primary} эдгээх",
                "fact_check",
                "mn",
                ["google_factcheck", "factcheck_mn"],
                True,
                keys_mn,
            )
        )
    if len(mn_terms) >= 2:
        queries.append(
            _query(
                " ".join(mn_terms[:2]),
                "fact_check",
                "mn",
                ["google_factcheck", "factcheck_mn"],
                True,
                keys_mn[:2],
            )
        )
    return _dedupe_queries(queries, 3)


def _build_medical_queries(
    concepts: list[dict[str, Any]],
    claim_forms: list[str] | None = None,
) -> list[dict[str, Any]]:
    groups: list[str] = []
    keys: list[str] = []
    medical_concepts = [
        concept
        for concept in concepts
        if concept["key"] not in _NON_BIOMEDICAL_QUERY_KEYS
    ]
    for concept in _query_concepts(medical_concepts, 3):
        terms = [term for term in concept.get("en_terms", ()) if term][:3]
        if not terms:
            continue
        keys.append(concept["key"])
        if len(terms) == 1:
            groups.append(f'TITLE_ABS:"{terms[0]}"')
        else:
            groups.append(
                "(" + " OR ".join(f'TITLE_ABS:"{term}"' for term in terms) + ")"
            )
    if not groups:
        return []
    primary = " AND ".join(groups)
    queries = [
        _query(primary, "medical", "en", ["europe_pmc"], False, keys),
    ]
    if "treatment" in (claim_forms or []) and len(groups) >= 2:
        queries.append(
            _query(
                primary
                + ' AND (TITLE_ABS:"treatment" OR TITLE_ABS:"therapy" OR TITLE_ABS:"cure")',
                "medical",
                "en",
                ["europe_pmc"],
                False,
                keys,
            )
        )
    if len(groups) >= 2:
        queries.append(
            _query(
                primary
                + ' AND (TITLE:"systematic review" OR TITLE:"meta-analysis" OR TITLE:"review")',
                "medical",
                "en",
                ["europe_pmc"],
                False,
                keys,
            )
        )
    if len(groups) >= 3:
        queries.append(
            _query(
                " AND ".join(groups[:2]),
                "medical",
                "en",
                ["europe_pmc"],
                False,
                keys[:2],
            )
        )
    return _dedupe_queries(queries, 4)


def _build_web_queries(
    claim: str,
    concepts: list[dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    selected = _query_concepts(concepts, 4)
    keys = [concept["key"] for concept in selected]
    mn_terms = _concept_terms(selected, "mn")[:4]
    en_terms = _concept_terms(selected, "en")[:4]
    queries: list[dict[str, Any]] = []
    primary_language = "en" if language == "en" else "mn"
    primary_terms = en_terms if primary_language == "en" else mn_terms
    primary = " ".join(primary_terms) if primary_terms else _keyword_phrase(claim, 4)
    if primary:
        queries.append(
            _query(primary, "web", primary_language, ["source_search"], True, keys[:4])
        )
    if language != "en" and en_terms:
        queries.append(
            _query(" ".join(en_terms), "web", "en", ["source_search"], False, keys[:4])
        )
    if language == "en" and mn_terms:
        queries.append(
            _query(" ".join(mn_terms), "web", "mn", ["source_search"], False, keys[:4])
        )
    return _dedupe_queries(queries, 2)


def normalize_scope(scope: str | None) -> str:
    value = (scope or "auto").strip().lower()
    return value if value in {"auto", "medical", "fact_check", "all"} else "auto"


def _domain_for(concepts: list[dict[str, Any]], text: str) -> str:
    categories = {concept["category"] for concept in concepts}
    if categories & {
        "radiation",
        "environmental_health",
        "nutrition",
        "public_health",
    }:
        return "public_health"
    if categories & {
        "vaccines",
        "chronic_disease",
        "maternal_child",
        "medication",
        "infectious_disease",
        "mental_health",
        "reproductive_health",
        "medical",
    }:
        return "medical"
    # A few common health words are not in the concept lexicon because they are
    # too generic to search on their own, but they still mark the domain.
    if _contains_any(
        text,
        (
            "эрүүл мэнд", "эмнэлэг", "эмч", "өвчин", "өвдөх", "халуурах",
            "цус", "хавдар", "эдгээ", "шарх", "гэмтэл", "хорд", "бал",
            "жирэмс", "ханиалга", "зовуур", "health", "hospital", "doctor",
            "disease", "symptom", "patient", "cancer", "bleeding", "wound",
        ),
    ):
        return "medical"
    return "general"


def _claim_forms(text: str) -> list[str]:
    forms: list[str] = []
    if _contains_any(text, _CAUSAL_TERMS):
        forms.append("causal")
    if _contains_any(text, _RISK_TERMS):
        forms.append("risk")
    if _contains_any(text, _PREVENTION_TERMS):
        forms.append("prevention")
    if _contains_any(text, _TREATMENT_TERMS):
        forms.append("treatment")
    if _contains_any(text, _RECOMMENDATION_TERMS):
        forms.append("recommendation")
    # "2 толгойтой" is a description, not a statistic. Require a percentage,
    # a three-digit/grouped number, a ratio, or an explicit multiplier.
    if (
        re.search(r"\d+(?:[.,]\d+)?\s*%", text)
        or re.search(r"\b\d{3,}(?:[.,]\d+)*\b", text)
        or re.search(r"\b\d+\s*/\s*\d+\b", text)
        or re.search(r"\b\d+(?:[.,]\d+)?\s*(?:дахин|удаа|fold)\b", text, re.IGNORECASE)
    ):
        forms.append("statistic")
    if _contains_any(text, _CONSPIRACY_TERMS):
        forms.append("conspiracy")
    if _is_question(text):
        forms.append("question")
    if has_negation(text):
        forms.append("negated")
    return forms


def analyze_claim(
    text: str,
    language_hint: str = "auto",
    source_scope: str = "auto",
) -> dict[str, Any]:
    """Return a transparent reading of the post and the queries to run."""
    original = (text or "").strip()
    cleaned = clean_social_text(original)
    language = (
        detect_language(cleaned or original)
        if language_hint in {"", "auto", None}
        else language_hint
    )
    if language == "other":
        latin = len(re.findall(r"[a-z]", (cleaned or original).lower()))
        cyrillic = len(re.findall(r"[а-яёөү]", (cleaned or original).lower()))
        if latin >= 6 and cyrillic == 0:
            language = "en"
    claims = select_claims(cleaned or original)
    claim = claims[0] if claims else (cleaned or original)
    claim = (clean_claim_text(claim) or claim).replace("\n", " ").strip()

    concepts = find_concepts(claim)
    if len(concepts) < 2:
        context_concepts = find_concepts(cleaned)
        if len(context_concepts) > len(concepts):
            concepts = context_concepts
    concepts = _canonical_concepts(concepts)
    domain = _domain_for(concepts, f"{claim} {cleaned}")
    medical_relevant = domain in {"medical", "public_health"}
    requested_scope = normalize_scope(source_scope)
    if requested_scope == "auto":
        effective_scope = "medical" if medical_relevant else "fact_check"
    else:
        effective_scope = requested_scope
    claim_forms = _claim_forms(claim)

    fact_check_queries = _build_fact_check_queries(claim, concepts, language)
    medical_queries = (
        _build_medical_queries(concepts, claim_forms)
        if effective_scope in {"medical", "all"}
        else []
    )
    web_queries = _build_web_queries(claim, concepts, language)

    queries = [*fact_check_queries]
    if effective_scope in {"medical", "all"}:
        queries.extend(medical_queries)
    queries.extend(web_queries)

    topics: list[str] = []
    for concept in concepts:
        category = concept["category"]
        if category not in topics:
            topics.append(category)
    topic_labels = [
        {
            "id": category,
            "label_mn": _CATEGORY_LABELS.get(category, (category, category))[0],
            "label_en": _CATEGORY_LABELS.get(category, (category, category))[1],
        }
        for category in topics
    ]

    keyword_list = keywords(claim, limit=14)
    if len(keyword_list) < 4:
        for item in keywords(cleaned, limit=14):
            if item not in keyword_list:
                keyword_list.append(item)
            if len(keyword_list) >= 8:
                break

    return {
        "original": original,
        "cleaned": cleaned,
        "claim": claim,
        "claims": claims,
        "language": language,
        "domain": domain,
        "medical_relevant": medical_relevant,
        "topics": topics,
        "topic_labels": topic_labels,
        "concepts": [
            {
                "key": concept["key"],
                "label_en": concept["label_en"],
                "label_mn": concept["label_mn"],
                "category": concept["category"],
                "matched_terms": concept["matched_terms"],
            }
            for concept in concepts
        ],
        "keywords": keyword_list,
        "claim_forms": claim_forms,
        "is_question": _is_question(claim),
        "negated": has_negation(claim),
        "requested_scope": requested_scope,
        "effective_scope": effective_scope,
        "queries": queries,
        "queries_by_purpose": {
            "fact_check": fact_check_queries,
            "medical": medical_queries,
            "web": web_queries,
        },
        "provider_labels": _PROVIDER_LABELS,
        "purpose_labels": _PURPOSE_LABELS,
    }
