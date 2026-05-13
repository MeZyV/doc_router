"""
FastAPI surface of doc-router: lifespan-managed httpx client, request-logging
middleware, and the three HTTP endpoints (/process, /health/*).

Heavy lifting lives in sibling modules:
  - helpers.py  : logging, env-loaded config, types, pure helpers
  - pdf.py      : in-process fitz/pymupdf4llm processing
  - upstream.py : MinerU/Tika/remote-PyMuPDF calls, retry, routing

The submodule names are re-exported below so that tests (which load `main.py`
as `doc_router_main`) can keep accessing `doc_router_main.foo` for helpers,
constants and types without knowing the internal split.
"""

import contextlib
import hmac  # noqa: F401 — re-exported for monkeypatching in tests
import time
from pathlib import Path
from typing import Any, Dict, Optional

import fitz  # noqa: F401 — re-exported for completeness
import httpx
import pymupdf4llm  # noqa: F401 — re-exported so tests can patch to_markdown via doc_router_main.pymupdf4llm

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import helpers as _helpers
from helpers import (  # noqa: F401 — re-exports for test convenience
    EXTERNAL_API_KEY,
    HEALTH_TIMEOUT_SECONDS,
    HTTPX_MAX_CONNECTIONS,
    HTTPX_MAX_KEEPALIVE,
    JsonFormatter,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    MINERU_ROUTER_URL,
    MINERU_TIMEOUT_SECONDS,
    PDF_MIN_TEXT_CHARS,
    PDF_SCAN_SAMPLE_PAGES,
    PYMUPDF4LLM_URL,
    PYMUPDF_TIMEOUT_SECONDS,
    RETRYABLE_STATUSES,
    RouterResult,
    TIKA_TIMEOUT_SECONDS,
    TIKA_URL,
    TMP_DIR,
    UPSTREAM_RETRIES,
    UPSTREAM_RETRY_BACKOFF_SECONDS,
    USE_REMOTE_PYMUPDF,
    ensure_authorized,
    extract_text_recursively,
    get_extension,
    is_image,
    is_pdf,
    is_tika_type,
    logger,
    merge_metadata,
    mineru_sem,
    normalize_filename,
    owui_response,
    parse_mime,
    pymupdf_sem,
    stream_body_to_tempfile,
    tika_sem,
)
from pdf import _detect_scanned_sync, _process_pdf_sync  # noqa: F401
from upstream import (  # noqa: F401
    _route_pdf,
    call_mineru,
    call_pymupdf4llm_remote,
    call_tika,
    request_with_retry,
    route_document,
)


# -------- app --------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_keepalive_connections=HTTPX_MAX_KEEPALIVE,
            max_connections=HTTPX_MAX_CONNECTIONS,
        ),
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(title="OpenWebUI Document Router", version="0.4.0", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.exception(
            "request_unhandled_error",
            extra={
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    extra: Dict[str, Any] = {
        "method": request.method,
        "path": request.url.path,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
    }
    info = getattr(request.state, "process_info", None)
    if info:
        extra.update(info)
    logger.info("request", extra=extra)
    return response


# -------- HTTP endpoints --------


@app.put("/process")
async def process_document(
    request: Request,
    x_filename: Optional[str] = Header(None, alias="X-Filename"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    ensure_authorized(authorization)

    filename = normalize_filename(x_filename)
    mime = parse_mime(request.headers.get("content-type"))
    suffix = Path(filename).suffix or ".bin"

    pdf_path, size = await stream_body_to_tempfile(request, suffix=suffix)
    request.state.process_info = {
        "filename": filename,
        "size_bytes": size,
        "mime": mime,
        "parser": None,
    }

    client: httpx.AsyncClient = request.app.state.http_client
    try:
        result = await route_document(pdf_path, filename, mime, size, client)
        request.state.process_info["parser"] = result.metadata.get("parser")
        return owui_response(result)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.exception("Upstream HTTP error")
        detail = exc.response.text[:1000]
        raise HTTPException(status_code=502, detail=f"Upstream parser failed: {detail}") from exc
    except Exception as exc:
        logger.exception("Document processing failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to remove temp file %s", pdf_path)


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


async def check_url(url: str, client: httpx.AsyncClient, method: str = "GET") -> Dict[str, Any]:
    try:
        response = await client.request(method, url, timeout=HEALTH_TIMEOUT_SECONDS)
        return {"ok": 200 <= response.status_code < 500, "status_code": response.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/health/ready")
async def health_ready(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    ensure_authorized(authorization)
    client: httpx.AsyncClient = request.app.state.http_client
    checks = {
        "mineru": await check_url(f"{MINERU_ROUTER_URL}/health", client),
        "tika": await check_url(f"{TIKA_URL}/tika", client),
    }
    if _helpers.USE_REMOTE_PYMUPDF:
        checks["pymupdf4llm"] = await check_url(f"{PYMUPDF4LLM_URL}/health", client)
    else:
        checks["pymupdf4llm"] = {"ok": True, "mode": "local"}
    ok = all(item.get("ok") for item in checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "degraded", "checks": checks},
        status_code=200 if ok else 503,
    )


@app.get("/health")
async def health(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    return await health_ready(request, authorization)
