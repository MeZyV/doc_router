"""
Shared utilities for doc-router: logging, env-loaded config, request helpers,
response shaping, and small pure functions.

Imported by `pdf.py`, `upstream.py` and `main.py`. Has no internal dependencies
on those modules to keep the import graph acyclic.
"""

import asyncio
import hmac
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


# -------- logging --------


class JsonFormatter(logging.Formatter):
    STANDARD_ATTRS = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "taskName", "color_message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "text").lower()
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


_configure_logging()
logger = logging.getLogger("doc-router")


# -------- configuration (env-loaded constants & semaphores) --------

MINERU_ROUTER_URL = os.getenv("MINERU_ROUTER_URL", "http://mineru-router:8002").rstrip("/")
PYMUPDF4LLM_URL = os.getenv("PYMUPDF4LLM_URL", "http://pymupdf4llm-api:8000").rstrip("/")
TIKA_URL = os.getenv("TIKA_URL", "http://tika:9998").rstrip("/")
EXTERNAL_API_KEY = os.getenv("EXTERNAL_API_KEY")
USE_REMOTE_PYMUPDF = os.getenv("USE_REMOTE_PYMUPDF", "false").lower() in {"1", "true", "yes", "on"}

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
PDF_MIN_TEXT_CHARS = int(os.getenv("PDF_MIN_TEXT_CHARS", "150"))
PDF_SCAN_SAMPLE_PAGES = int(os.getenv("PDF_SCAN_SAMPLE_PAGES", "3"))
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/doc-router"))
TMP_DIR.mkdir(parents=True, exist_ok=True)

PYMUPDF_TIMEOUT_SECONDS = float(os.getenv("PYMUPDF_TIMEOUT_SECONDS", "300"))
TIKA_TIMEOUT_SECONDS = float(os.getenv("TIKA_TIMEOUT_SECONDS", "300"))
MINERU_TIMEOUT_SECONDS = float(os.getenv("MINERU_TIMEOUT_SECONDS", "1700"))
HEALTH_TIMEOUT_SECONDS = float(os.getenv("HEALTH_TIMEOUT_SECONDS", "5"))

HTTPX_MAX_KEEPALIVE = int(os.getenv("HTTPX_MAX_KEEPALIVE", "20"))
HTTPX_MAX_CONNECTIONS = int(os.getenv("HTTPX_MAX_CONNECTIONS", "50"))

UPSTREAM_RETRIES = int(os.getenv("UPSTREAM_RETRIES", "1"))
UPSTREAM_RETRY_BACKOFF_SECONDS = float(os.getenv("UPSTREAM_RETRY_BACKOFF_SECONDS", "0.5"))
RETRYABLE_STATUSES = frozenset({502, 503, 504})

mineru_sem = asyncio.Semaphore(int(os.getenv("MINERU_MAX_CONCURRENCY", "2")))
pymupdf_sem = asyncio.Semaphore(int(os.getenv("PYMUPDF_MAX_CONCURRENCY", "4")))
tika_sem = asyncio.Semaphore(int(os.getenv("TIKA_MAX_CONCURRENCY", "4")))

PDF_EXTENSIONS = {".pdf"}
PDF_MIMES = {"application/pdf", "application/x-pdf", "application/acrobat", "applications/vnd.pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
TIKA_EXTENSIONS = {
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".tsv", ".txt", ".md", ".html", ".htm",
    ".xml", ".rtf", ".odt", ".ods", ".odp",
}


# -------- types --------


class RouterResult(NamedTuple):
    content: str
    metadata: Dict[str, Any]


def merge_metadata(*sources: Dict[str, Any], parser: Optional[str] = None) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for source in sources:
        if source:
            merged.update(source)
    if parser is not None:
        merged["parser"] = parser
    return merged


# -------- pure helpers --------


def normalize_filename(filename: Optional[str]) -> str:
    if not filename:
        return "document"
    return Path(filename).name or "document"


def get_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def parse_mime(raw: Optional[str]) -> str:
    if not raw:
        return "application/octet-stream"
    return raw.split(";", 1)[0].strip().lower() or "application/octet-stream"


def is_pdf(ext: str, mime: str) -> bool:
    return ext in PDF_EXTENSIONS or mime in PDF_MIMES


def is_image(ext: str, mime: str) -> bool:
    return ext in IMAGE_EXTENSIONS or mime.startswith("image/")


def is_tika_type(ext: str, mime: str) -> bool:
    return ext in TIKA_EXTENSIONS or mime.startswith("text/")


def ensure_authorized(authorization: Optional[str]) -> None:
    if not EXTERNAL_API_KEY:
        return
    expected = f"Bearer {EXTERNAL_API_KEY}"
    provided = authorization or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid external API key")


def owui_response(result: RouterResult) -> JSONResponse:
    return JSONResponse(
        {
            "page_content": result.content or "",
            "metadata": {**result.metadata, "router": "doc-router"},
        }
    )


def extract_text_recursively(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n\n".join(filter(None, (extract_text_recursively(item) for item in value)))
    if isinstance(value, dict):
        priority_keys = [
            "md_content", "markdown", "md", "content", "text",
            "page_content", "result", "data",
        ]
        for key in priority_keys:
            if key in value:
                extracted = extract_text_recursively(value[key])
                if extracted:
                    return extracted
        return "\n\n".join(
            filter(None, (extract_text_recursively(item) for item in value.values()))
        )
    return ""


async def stream_body_to_tempfile(request: Request, suffix: str) -> Tuple[Path, int]:
    content_length_hdr = request.headers.get("content-length")
    if content_length_hdr:
        try:
            advertised = int(content_length_hdr)
        except ValueError:
            advertised = None
        if advertised is not None and advertised > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max is {MAX_UPLOAD_MB} MB",
            )

    fd, tmp_name = tempfile.mkstemp(dir=str(TMP_DIR), suffix=suffix)
    tmp_path = Path(tmp_name)
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max is {MAX_UPLOAD_MB} MB",
                    )
                fh.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    if size == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty request body")

    return tmp_path, size
