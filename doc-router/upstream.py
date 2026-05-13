"""
HTTP calls to the three upstream parsers (MinerU, Tika, optional remote
PyMuPDF4LLM), the retry helper for transient failures, and the top-level
routing logic that picks an upstream based on extension/MIME.

Config values that may be monkeypatched in tests (UPSTREAM_RETRIES,
UPSTREAM_RETRY_BACKOFF_SECONDS, USE_REMOTE_PYMUPDF) are read via the `helpers`
module namespace so a `setattr(helpers, "X", v)` is observed at call time.
"""

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

import httpx

import helpers
from helpers import (
    MINERU_ROUTER_URL,
    MINERU_TIMEOUT_SECONDS,
    PYMUPDF4LLM_URL,
    PYMUPDF_TIMEOUT_SECONDS,
    RETRYABLE_STATUSES,
    RouterResult,
    TIKA_TIMEOUT_SECONDS,
    TIKA_URL,
    extract_text_recursively,
    get_extension,
    is_image,
    is_pdf,
    is_tika_type,
    logger,
    merge_metadata,
    mineru_sem,
    pymupdf_sem,
    tika_sem,
)
from pdf import _detect_scanned_sync, _process_pdf_sync


async def request_with_retry(
    attempt: Callable[[], Awaitable[httpx.Response]],
    label: str,
) -> httpx.Response:
    """
    Retry transient upstream failures: 502/503/504 and httpx.TransportError
    (TCP reset, connection refused, transport-level read timeout).

    `attempt` is an async callable returning an `httpx.Response`. It is invoked
    fresh on each retry so callers can re-open file handles between attempts.
    Reads UPSTREAM_RETRIES/UPSTREAM_RETRY_BACKOFF_SECONDS from `helpers` at
    call time so tests can monkeypatch them.
    """
    max_attempts = max(helpers.UPSTREAM_RETRIES, 0) + 1
    for i in range(max_attempts):
        is_last = i == max_attempts - 1
        try:
            response = await attempt()
        except httpx.TransportError as exc:
            if is_last:
                raise
            logger.warning(
                "upstream_transport_error_retrying",
                extra={
                    "upstream": label,
                    "error": str(exc),
                    "attempt": i + 1,
                    "max_attempts": max_attempts,
                },
            )
        else:
            if response.status_code not in RETRYABLE_STATUSES or is_last:
                return response
            logger.warning(
                "upstream_status_retrying",
                extra={
                    "upstream": label,
                    "status_code": response.status_code,
                    "attempt": i + 1,
                    "max_attempts": max_attempts,
                },
            )
        await asyncio.sleep(helpers.UPSTREAM_RETRY_BACKOFF_SECONDS * (2 ** i))
    raise RuntimeError("request_with_retry: unreachable")


async def call_pymupdf4llm_remote(
    pdf_path: Path, filename: str, mime: str, client: httpx.AsyncClient
) -> RouterResult:
    request_headers = {"Content-Type": mime or "application/pdf", "X-Filename": filename}

    async def attempt() -> httpx.Response:
        with open(pdf_path, "rb") as fh:
            return await client.put(
                f"{PYMUPDF4LLM_URL}/process",
                content=fh,
                headers=request_headers,
                timeout=PYMUPDF_TIMEOUT_SECONDS,
            )

    async with pymupdf_sem:
        response = await request_with_retry(attempt, label="pymupdf4llm")
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        content = "\n\n".join(
            item.get("page_content", "") for item in data if isinstance(item, dict)
        )
        return RouterResult(content, {"parser": "pymupdf4llm", "upstream_shape": "list"})
    return RouterResult(
        data.get("page_content", ""),
        data.get("metadata", {"parser": "pymupdf4llm"}),
    )


async def call_tika(
    pdf_path: Path, filename: str, mime: str, client: httpx.AsyncClient
) -> RouterResult:
    request_headers = {
        "Content-Type": mime or "application/octet-stream",
        "Accept": "text/plain",
    }

    async def attempt() -> httpx.Response:
        with open(pdf_path, "rb") as fh:
            return await client.put(
                f"{TIKA_URL}/tika",
                content=fh,
                headers=request_headers,
                timeout=TIKA_TIMEOUT_SECONDS,
            )

    async with tika_sem:
        response = await request_with_retry(attempt, label="tika")
    response.raise_for_status()
    return RouterResult(response.text, {"parser": "tika", "filename": filename})


async def call_mineru(
    pdf_path: Path, filename: str, mime: str, client: httpx.AsyncClient
) -> RouterResult:
    data = {
        "return_md": "true",
        "return_content_list": "false",
        "return_middle_json": "false",
        "return_model_output": "false",
        "return_original_file": "false",
        "response_format_zip": "false",
    }

    async def attempt() -> httpx.Response:
        with open(pdf_path, "rb") as fh:
            files = {"files": (filename, fh, mime or "application/octet-stream")}
            return await client.post(
                f"{MINERU_ROUTER_URL}/file_parse",
                files=files,
                data=data,
                timeout=MINERU_TIMEOUT_SECONDS,
            )

    async with mineru_sem:
        response = await request_with_retry(attempt, label="mineru")
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        text = extract_text_recursively(payload)
        return RouterResult(
            text,
            {
                "parser": "mineru",
                "filename": filename,
                "mineru_content_type": content_type,
            },
        )
    return RouterResult(
        response.text,
        {
            "parser": "mineru",
            "filename": filename,
            "mineru_content_type": content_type,
        },
    )


# -------- routing --------


async def _route_pdf(
    pdf_path: Path, filename: str, mime: str, size: int, client: httpx.AsyncClient
) -> RouterResult:
    base_meta: Dict[str, Any] = {
        "filename": filename,
        "content_type": mime,
        "size_bytes": size,
    }

    if helpers.USE_REMOTE_PYMUPDF:
        is_scanned, sampled, pages = await asyncio.to_thread(
            _detect_scanned_sync, pdf_path
        )
        base_meta.update(
            {
                "pdf_sampled_text_chars": sampled,
                "pdf_page_count": pages,
                "pdf_detected_scanned_or_text_poor": is_scanned,
            }
        )
        if not is_scanned:
            try:
                remote = await call_pymupdf4llm_remote(
                    pdf_path, filename, mime or "application/pdf", client
                )
                if remote.content.strip():
                    return RouterResult(
                        remote.content,
                        merge_metadata(
                            base_meta,
                            remote.metadata,
                            parser=remote.metadata.get("parser", "pymupdf4llm"),
                        ),
                    )
                logger.warning("Remote PyMuPDF4LLM returned empty content, falling back to MinerU")
            except Exception as exc:
                logger.warning("Remote PyMuPDF4LLM failed, falling back to MinerU: %s", exc)
        mineru = await call_mineru(pdf_path, filename, mime or "application/pdf", client)
        return RouterResult(mineru.content, merge_metadata(base_meta, mineru.metadata, parser="mineru"))

    async with pymupdf_sem:
        is_scanned_or_failed, content, pdf_meta = await asyncio.to_thread(
            _process_pdf_sync, pdf_path
        )
    base_meta.update(pdf_meta)
    if not is_scanned_or_failed and content.strip():
        return RouterResult(content, base_meta)
    if not is_scanned_or_failed and not content.strip():
        logger.warning("Local PyMuPDF4LLM returned empty content, falling back to MinerU")
    mineru = await call_mineru(pdf_path, filename, mime or "application/pdf", client)
    return RouterResult(mineru.content, merge_metadata(base_meta, mineru.metadata, parser="mineru"))


async def route_document(
    pdf_path: Path,
    filename: str,
    mime: str,
    size: int,
    client: httpx.AsyncClient,
) -> RouterResult:
    ext = get_extension(filename)

    if is_pdf(ext, mime):
        return await _route_pdf(pdf_path, filename, mime, size, client)

    base_metadata: Dict[str, Any] = {
        "filename": filename,
        "content_type": mime,
        "size_bytes": size,
    }

    if is_image(ext, mime):
        mineru = await call_mineru(pdf_path, filename, mime, client)
        return RouterResult(mineru.content, merge_metadata(base_metadata, mineru.metadata, parser="mineru"))

    if is_tika_type(ext, mime):
        tika = await call_tika(pdf_path, filename, mime, client)
        return RouterResult(tika.content, merge_metadata(base_metadata, tika.metadata, parser="tika"))

    try:
        tika = await call_tika(pdf_path, filename, mime, client)
        if tika.content.strip():
            return RouterResult(tika.content, merge_metadata(base_metadata, tika.metadata, parser="tika"))
    except Exception as exc:
        logger.warning("Tika failed for unknown type, falling back to MinerU: %s", exc)

    mineru = await call_mineru(pdf_path, filename, mime, client)
    return RouterResult(mineru.content, merge_metadata(base_metadata, mineru.metadata, parser="mineru"))
