"""
In-process PDF parsing: scanned-PDF detection and pymupdf4llm Markdown
extraction sharing a single `fitz.Document` per call.
"""

from pathlib import Path
from typing import Any, Dict, Tuple

import fitz  # PyMuPDF
import pymupdf4llm

from helpers import PDF_MIN_TEXT_CHARS, PDF_SCAN_SAMPLE_PAGES, logger


def _detect_scanned_sync(pdf_path: Path) -> Tuple[bool, int, int]:
    """
    Standalone scanned-PDF detection. Used only on the opt-in remote PyMuPDF
    path (USE_REMOTE_PYMUPDF=true); the default in-process path uses
    `_process_pdf_sync` which fuses detection and extraction on one fitz handle.
    Returns (is_scanned_or_failed, sampled_text_chars, page_count).
    """
    try:
        with fitz.open(str(pdf_path)) as doc:
            page_count = len(doc)
            pages_to_read = min(page_count, max(PDF_SCAN_SAMPLE_PAGES, 1))
            text_len = sum(
                len(doc[i].get_text("text").strip()) for i in range(pages_to_read)
            )
            return text_len < PDF_MIN_TEXT_CHARS, text_len, page_count
    except Exception as exc:
        logger.warning("PDF detection failed, routing to MinerU: %s", exc)
        return True, 0, 0


def _process_pdf_sync(pdf_path: Path) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Single fitz.open() shared by scan detection and markdown extraction. The
    Document is released by the `with` block in every path — including when
    pymupdf4llm.to_markdown raises — preventing FD leaks under load.
    Returns (is_scanned_or_failed, content, metadata).
    """
    meta: Dict[str, Any] = {
        "pdf_page_count": 0,
        "pdf_sampled_text_chars": 0,
        "pdf_detected_scanned_or_text_poor": True,
    }
    try:
        with fitz.open(str(pdf_path)) as doc:
            page_count = len(doc)
            pages_to_read = min(page_count, max(PDF_SCAN_SAMPLE_PAGES, 1))
            text_len = sum(
                len(doc[i].get_text("text").strip()) for i in range(pages_to_read)
            )
            is_scanned = text_len < PDF_MIN_TEXT_CHARS
            meta.update(
                {
                    "pdf_page_count": page_count,
                    "pdf_sampled_text_chars": text_len,
                    "pdf_detected_scanned_or_text_poor": is_scanned,
                }
            )
            if is_scanned:
                return True, "", meta
            try:
                markdown = pymupdf4llm.to_markdown(doc)
            except Exception as exc:
                logger.warning("pymupdf4llm.to_markdown failed, will fall back: %s", exc)
                meta["pymupdf4llm_error"] = str(exc)
                return True, "", meta
            meta["parser"] = "pymupdf4llm"
            return False, markdown or "", meta
    except Exception as exc:
        logger.warning("Failed to open PDF, routing to MinerU: %s", exc)
        meta["pdf_open_error"] = str(exc)
        return True, "", meta
