import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pymupdf4llm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


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
logger = logging.getLogger("pymupdf4llm-api")

app = FastAPI(title="PyMuPDF4LLM API", version="0.3.0")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/pymupdf4llm"))
TMP_DIR.mkdir(parents=True, exist_ok=True)


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


def normalize_filename(filename: Optional[str]) -> str:
    if not filename:
        return "document.pdf"
    return Path(filename).name or "document.pdf"


async def stream_body_to_tempfile(request: Request, suffix: str):
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
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if size == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty request body")

    return tmp_path, size


@app.put("/process")
async def process_document(
    request: Request,
    x_filename: Optional[str] = Header(None, alias="X-Filename"),
):
    filename = normalize_filename(x_filename)
    suffix = Path(filename).suffix or ".pdf"

    tmp_path, size = await stream_body_to_tempfile(request, suffix=suffix)
    request.state.process_info = {"filename": filename, "size_bytes": size}

    try:
        markdown = pymupdf4llm.to_markdown(str(tmp_path))
        return JSONResponse(
            {
                "page_content": markdown or "",
                "metadata": {
                    "parser": "pymupdf4llm",
                    "filename": filename,
                    "size_bytes": size,
                },
            }
        )
    except Exception as exc:
        logger.exception("PyMuPDF4LLM parsing failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to cleanup temporary file %s", tmp_path)


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    return {
        "status": "ready",
        "parser": "pymupdf4llm",
        "tmp_dir": str(TMP_DIR),
    }


@app.get("/health")
async def health():
    return await health_ready()
