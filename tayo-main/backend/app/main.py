from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import settings
from .schemas import (
    FactCheckRequest,
    ImageUrlRequest,
    TextRequest,
    UrlRequest,
    UrlCheckRequest,
    VideoUrlRequest,
    VirusTotalKeyRequest,
)
from .services.factcheck import factcheck_claim
from .services.claim_retrieval import source_catalog
from .services.image_analysis import analyze_image, analyze_image_url
from .services.link_dossier import factcheck_overview, overall_safety
from .services.model_registry import model_status
from .services.net_utils import UnsafeUrlError
from .services.text_analysis import analyze_text
from .services.url_scan import scan_url, validate_virustotal_key
from .services.web_content import ContentFetchError, extract_web_content
from .services.video_analysis import analyze_video
from .services.video_url_fetch import VideoFetchError, fetch_video_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tayo")


def _site_host(value: str) -> str:
    try:
        host = (urlparse(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


app = FastAPI(
    title="TAYO API",
    version="0.1.0",
    description=(
        "Local-first fact-checking and synthetic-media triage API for journalists. "
        "Every model output is a signal, not proof."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "image/avif",
}
VIDEO_CONTENT_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
    "video/x-msvideo",
    "video/mpeg",
    "video/ogg",
    "application/octet-stream",
}


@app.exception_handler(UnsafeUrlError)
async def unsafe_url_handler(_: Request, exc: UnsafeUrlError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "error": "unsafe_url"},
    )


@app.get("/")
async def root() -> dict:
    return {
        "name": "TAYO API",
        "version": app.version,
        "docs": "/docs",
        "health": "/api/health",
        "note": "Local demo. Results are triage signals, not proof.",
    }


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "uptime_seconds": round(time.monotonic() - STARTED, 1),
        "models": model_status(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


STARTED = time.monotonic()


@app.get("/api/config")
async def config() -> dict:
    """Public, non-secret runtime information for the extension UI."""
    return {
        "max_upload_mb": settings.max_upload_mb,
        "max_image_url_mb": settings.max_image_url_mb,
        "video_frames": settings.video_frames,
        "device": model_status()["device"],
        "features": {
            "factcheck_corpus": True,
            "google_factcheck": bool(settings.google_factcheck_api_key),
            "ollama_summary": bool(settings.ollama_url),
            "image_model": True,
            "text_model": settings.enable_text_model,
            "video": True,
            "url_scan": True,
            "safe_browsing": bool(settings.google_safe_browsing_api_key),
            "virustotal": bool(settings.virustotal_api_key),
            "urlhaus": bool(settings.urlhaus_auth_key),
        },
        "models": {
            "image_primary": settings.image_model_primary,
            "image_fallback": settings.image_model_fallback,
            "image_secondary": (
                settings.image_model_secondary
                if settings.enable_secondary_image_model
                else None
            ),
            "text": settings.text_model_id if settings.enable_text_model else None,
            "smaug_crops": settings.smaug_crops,
        },
        "limitations": [
            "AI detectors are probabilistic and can be fooled or produce false positives.",
            "The curated fact-check corpus is small; connect the Google Fact Check Tools API for broader coverage.",
            "The combined Link check fetches static HTML after security screening but does not execute page JavaScript or submit forms. VirusTotal receives unknown URLs when configured.",
            "This is a demonstration, not a forensic or editorial decision system.",
        ],
    }


@app.post("/api/factcheck")
async def api_factcheck(payload: FactCheckRequest) -> dict:
    try:
        return await factcheck_claim(
            payload.claim,
            payload.language,
            payload.source_scope,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("factcheck failed")
        raise HTTPException(status_code=500, detail=f"Fact-check failed: {exc}") from exc


@app.get("/api/factcheck/sources")
async def api_factcheck_sources(
    scope: str = "all",
    category: str | None = None,
    language: str | None = None,
    medical: bool | None = None,
    q: str | None = None,
) -> dict:
    """List the trusted-source registry used by the Claim tab.

    The browser is deliberately inspectable before a claim is checked: a reader
    can see which Mongolian health, fact-checking, government, university and
    international sources the result may draw on, and why each is listed.
    """
    try:
        return source_catalog(
            scope=scope,
            category=category,
            language=language,
            medical=medical,
            query=q,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("source catalog failed")
        raise HTTPException(
            status_code=500,
            detail=f"Source catalog failed: {exc}",
        ) from exc


@app.post("/api/analyze/text")
async def api_analyze_text(payload: TextRequest) -> dict:
    try:
        # Text analysis can load and run ModernBERT. Mongolian and other
        # non-English inputs get an explicitly experimental score only.
        return await run_in_threadpool(
            analyze_text, payload.text, payload.language
        )
    except Exception as exc:
        logger.exception("text analysis failed")
        raise HTTPException(status_code=500, detail=f"Text analysis failed: {exc}") from exc


async def _save_upload(upload: UploadFile, max_mb: int) -> tuple[Path, str, int]:
    """Stream an upload to a temporary file while enforcing a size limit."""
    suffix = Path(upload.filename or "").suffix[:10] or ".bin"
    max_bytes = max_mb * 1024 * 1024
    total = 0
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = Path(temp.name)
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File is larger than the {max_mb} MB limit.",
                )
            temp.write(chunk)
    except Exception:
        temp.close()
        path.unlink(missing_ok=True)
        raise
    temp.close()
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return path, upload.filename or path.name, total


@app.post("/api/analyze/image")
async def api_analyze_image(
    file: UploadFile = File(...),
    tiles: bool = False,
) -> dict:
    content_type = (file.content_type or "").split(";")[0].lower()
    if content_type and content_type not in IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image content type: {content_type}",
        )
    path, name, _ = await _save_upload(file, settings.max_upload_mb)
    try:
        result = await run_in_threadpool(analyze_image, path, name, None, tiles)
        return result
    except Exception as exc:
        logger.exception("image analysis failed")
        raise HTTPException(status_code=500, detail=f"Image analysis failed: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/analyze/image-url")
async def api_analyze_image_url(payload: ImageUrlRequest) -> dict:
    try:
        return await analyze_image_url(payload.url, tiles=payload.tiles)
    except UnsafeUrlError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("image URL analysis failed")
        raise HTTPException(status_code=502, detail=f"Could not analyze image URL: {exc}") from exc


@app.post("/api/analyze/video")
async def api_analyze_video(file: UploadFile = File(...)) -> dict:
    content_type = (file.content_type or "").split(";")[0].lower()
    if content_type and content_type not in VIDEO_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported video content type: {content_type}",
        )
    path, name, _ = await _save_upload(file, settings.max_upload_mb)
    try:
        # Frames live next to the upload, inside a per-request directory so the
        # TemporaryDirectory cleanup removes both the file and extracted frames.
        with tempfile.TemporaryDirectory(prefix="tayo_video_") as workdir:
            target = Path(workdir) / f"upload{Path(name).suffix[:10] or '.mp4'}"
            path.replace(target)
            result = await run_in_threadpool(analyze_video, target, name)
        return result
    except Exception as exc:
        logger.exception("video analysis failed")
        raise HTTPException(status_code=500, detail=f"Video analysis failed: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/analyze/video-url")
async def api_analyze_video_url(payload: VideoUrlRequest) -> dict:
    try:
        with tempfile.TemporaryDirectory(prefix="tayo_video_url_") as workdir:
            path, final_url, filename = await run_in_threadpool(
                fetch_video_url, payload.url, Path(workdir)
            )
            result = await run_in_threadpool(analyze_video, path, filename)
            result["source_url"] = final_url
            return result
    except UnsafeUrlError:
        raise
    except VideoFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("video URL analysis failed")
        raise HTTPException(status_code=502, detail=f"Could not analyze video URL: {exc}") from exc


@app.post("/api/scan/url")
async def api_scan_url(payload: UrlRequest) -> dict:
    try:
        api_key = (
            payload.virustotal_api_key.get_secret_value()
            if payload.virustotal_api_key
            else None
        )
        return await scan_url(payload.url, api_key)
    except UnsafeUrlError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("URL scan failed")
        raise HTTPException(status_code=502, detail=f"URL scan failed: {exc}") from exc


@app.post("/api/check/url")
async def api_check_url(payload: UrlCheckRequest) -> dict:
    """Run security checks, then safely extract and fact-check permitted content."""
    try:
        # Preserve the extension's existing connected-key workflow while
        # allowing scan_url to fall back to server-side VIRUSTOTAL_API_KEY.
        api_key = (
            payload.virustotal_api_key.get_secret_value()
            if payload.virustotal_api_key else None
        )
        security = await scan_url(payload.url, api_key)
    except UnsafeUrlError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("combined URL security scan failed")
        raise HTTPException(status_code=502, detail=f"URL security scan failed: {exc}") from exc

    response: dict = {
        "kind": "combined_url_check",
        "url": security.get("url", payload.url),
        "security": security,
        "website": None,
        "overview": None,
        "safety": None,
        "destination_security": None,
        "factcheck": None,
        "factchecks": [],
        "factcheck_overview": factcheck_overview([]),
        "workflow_status": "security_complete",
    }
    if security.get("security_status") == "malicious":
        response.update({
            "workflow_status": "blocked_malicious",
            "content_error": "Threat intelligence flagged this URL. TAYO did not open or download the page.",
            "safety": overall_safety(security),
        })
        return response

    try:
        website = await run_in_threadpool(extract_web_content, security["url"])
        website["original_url"] = security["url"]
        response["website"] = website
        response["workflow_status"] = "content_extracted"
    except (UnsafeUrlError, ContentFetchError) as exc:
        response.update({
            "workflow_status": "content_unavailable",
            "content_error": str(exc),
            "safety": overall_safety(security),
        })
        return response
    except Exception as exc:
        logger.exception("website content extraction failed")
        response.update({
            "workflow_status": "content_unavailable",
            "content_error": f"Could not extract website content: {exc}",
            "safety": overall_safety(security),
        })
        return response

    fetch = website.get("fetch") or {}
    inspection = website.get("inspection") or {}
    response["overview"] = {
        "title": website.get("title"),
        "final_url": website.get("url"),
        "summary": website.get("summary"),
        "page_type": inspection.get("page_type"),
        "language": (website.get("metadata") or {}).get("language"),
        "metadata": website.get("metadata"),
        "redirects": fetch.get("redirects") or [],
    }

    # A shortener or open redirect can hide a malicious destination. Re-check
    # the final host without automatically submitting it to VirusTotal.
    final_url = website.get("url")
    destination_security = None
    if final_url and _site_host(final_url) != _site_host(security.get("url") or ""):
        try:
            destination_security = await scan_url(
                final_url, api_key, submit_virustotal=False
            )
            response["destination_security"] = destination_security
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("redirect destination scan failed")
            response["destination_scan_error"] = str(exc)

    response["safety"] = overall_safety(
        security, website, destination_security
    )
    if (destination_security or {}).get("security_status") == "malicious":
        response["workflow_status"] = "destination_flagged"

    try:
        # Fact-check concise assertions independently. A whole page is not one
        # claim and performs poorly in similarity-based fact-check search.
        claims = website.get("claims") or [website["content"][:2_000]]
        response["factchecks"] = list(await asyncio.gather(*[
            factcheck_claim(claim, payload.language, payload.source_scope)
            for claim in claims[:3]
        ]))
        response["factcheck"] = (
            response["factchecks"][0] if response["factchecks"] else None
        )
        response["factcheck_overview"] = factcheck_overview(
            response["factchecks"]
        )
        if response["workflow_status"] != "destination_flagged":
            response["workflow_status"] = "complete"
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("website fact-check failed")
        response.update({
            "workflow_status": (
                "destination_flagged"
                if response["workflow_status"] == "destination_flagged"
                else "factcheck_unavailable"
            ),
            "factcheck_error": f"Fact-check failed: {exc}",
            "factcheck_overview": factcheck_overview([]),
        })
    return response


@app.post("/api/virustotal/validate")
async def api_validate_virustotal_key(payload: VirusTotalKeyRequest) -> dict:
    """Check a key supplied by the extension; never return or persist it."""
    return await validate_virustotal_key(payload.api_key.get_secret_value())


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - started) * 1000
    response.headers["x-request-id"] = request_id
    response.headers["x-elapsed-ms"] = f"{elapsed:.0f}"
    logger.info(
        "%s %s -> %s (%.0f ms) id=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
        request_id,
    )
    return response
