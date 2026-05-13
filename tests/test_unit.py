import pytest
from fastapi import HTTPException


def test_normalize_filename_none(doc_router_main):
    assert doc_router_main.normalize_filename(None) == "document"
    assert doc_router_main.normalize_filename("") == "document"


def test_normalize_filename_strips_path(doc_router_main):
    assert doc_router_main.normalize_filename("/etc/passwd") == "passwd"
    assert doc_router_main.normalize_filename("../../boot.ini") == "boot.ini"
    assert doc_router_main.normalize_filename("nested/path/file.pdf") == "file.pdf"


def test_get_extension_lowercases(doc_router_main):
    assert doc_router_main.get_extension("file.PDF") == ".pdf"
    assert doc_router_main.get_extension("file.DOCX") == ".docx"
    assert doc_router_main.get_extension("noext") == ""


def test_parse_mime_defaults(doc_router_main):
    assert doc_router_main.parse_mime(None) == "application/octet-stream"
    assert doc_router_main.parse_mime("") == "application/octet-stream"


def test_parse_mime_strips_params_and_lowercases(doc_router_main):
    assert doc_router_main.parse_mime("Application/PDF; charset=binary") == "application/pdf"
    assert doc_router_main.parse_mime("  text/PLAIN  ") == "text/plain"


def test_is_pdf_accepts_variants(doc_router_main):
    assert doc_router_main.is_pdf(".pdf", "application/octet-stream")
    assert doc_router_main.is_pdf("", "application/pdf")
    assert doc_router_main.is_pdf("", "application/x-pdf")
    assert not doc_router_main.is_pdf("", "image/png")


def test_is_image(doc_router_main):
    assert doc_router_main.is_image(".png", "")
    assert doc_router_main.is_image("", "image/jpeg")
    assert not doc_router_main.is_image("", "application/pdf")


def test_is_tika_type(doc_router_main):
    assert doc_router_main.is_tika_type(".docx", "application/octet-stream")
    assert doc_router_main.is_tika_type("", "text/csv")
    assert not doc_router_main.is_tika_type("", "image/png")


def test_extract_text_recursively_basic_types(doc_router_main):
    extract = doc_router_main.extract_text_recursively
    assert extract(None) == ""
    assert extract("hello") == "hello"
    assert extract([]) == ""
    assert extract({}) == ""
    assert extract(["a", "b"]) == "a\n\nb"


def test_extract_text_recursively_priority_first_hit(doc_router_main):
    payload = {"md_content": "primary", "text": "ignored"}
    result = doc_router_main.extract_text_recursively(payload)
    assert result == "primary"


def test_extract_text_recursively_mineru_shape(doc_router_main):
    payload = {"results": [{"md_content": "# Title\n\nBody"}]}
    result = doc_router_main.extract_text_recursively(payload)
    assert "Title" in result
    assert "Body" in result


def test_extract_text_recursively_falls_back_to_values(doc_router_main):
    payload = {"unknown_key": "fallback content"}
    result = doc_router_main.extract_text_recursively(payload)
    assert "fallback content" in result


def test_extract_text_recursively_drops_non_string_scalars(doc_router_main):
    assert doc_router_main.extract_text_recursively(42) == ""
    assert doc_router_main.extract_text_recursively(True) == ""


def test_ensure_authorized_no_key_configured(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", None)
    doc_router_main.ensure_authorized(None)
    doc_router_main.ensure_authorized("anything")


def test_ensure_authorized_match(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    doc_router_main.ensure_authorized("Bearer secret")


def test_ensure_authorized_mismatch(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    with pytest.raises(HTTPException) as exc_info:
        doc_router_main.ensure_authorized("Bearer wrong")
    assert exc_info.value.status_code == 401


def test_ensure_authorized_missing_header(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    with pytest.raises(HTTPException):
        doc_router_main.ensure_authorized(None)


def test_ensure_authorized_uses_constant_time(doc_router_main, doc_router_helpers, monkeypatch):
    """
    Sanity check: we don't time the comparison here (unreliable in CI), but we
    confirm the function delegates to hmac.compare_digest.
    """
    import hmac
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    called = []
    real = hmac.compare_digest

    def spy(a, b):
        called.append((a, b))
        return real(a, b)

    monkeypatch.setattr(doc_router_helpers.hmac, "compare_digest", spy)
    doc_router_main.ensure_authorized("Bearer secret")
    assert called and called[0][1] == "Bearer secret"


def test_detect_scanned_textual_pdf(doc_router_main, text_pdf_path):
    is_scanned, text_len, pages = doc_router_main._detect_scanned_sync(text_pdf_path)
    assert is_scanned is False
    assert text_len > doc_router_main.PDF_MIN_TEXT_CHARS
    assert pages >= 1


def test_detect_scanned_empty_pdf(doc_router_main, empty_pdf_path):
    is_scanned, text_len, pages = doc_router_main._detect_scanned_sync(empty_pdf_path)
    assert is_scanned is True
    assert text_len == 0
    assert pages == 1


def test_detect_scanned_invalid_bytes(doc_router_main, invalid_pdf_path):
    is_scanned, text_len, pages = doc_router_main._detect_scanned_sync(invalid_pdf_path)
    assert is_scanned is True
    assert text_len == 0
    assert pages == 0


def test_process_pdf_sync_textual(doc_router_main, text_pdf_path, stub_pymupdf4llm):
    is_scanned, content, meta = doc_router_main._process_pdf_sync(text_pdf_path)
    assert is_scanned is False
    assert content
    assert meta["parser"] == "pymupdf4llm"
    assert meta["pdf_detected_scanned_or_text_poor"] is False
    assert len(stub_pymupdf4llm) == 1


def test_process_pdf_sync_empty_is_scanned(doc_router_main, empty_pdf_path, stub_pymupdf4llm):
    is_scanned, content, meta = doc_router_main._process_pdf_sync(empty_pdf_path)
    assert is_scanned is True
    assert content == ""
    assert meta["pdf_detected_scanned_or_text_poor"] is True
    assert len(stub_pymupdf4llm) == 0


def test_process_pdf_sync_invalid_bytes(doc_router_main, invalid_pdf_path):
    is_scanned, content, meta = doc_router_main._process_pdf_sync(invalid_pdf_path)
    assert is_scanned is True
    assert content == ""
    assert "pdf_open_error" in meta


def test_process_pdf_sync_to_markdown_failure(doc_router_main, text_pdf_path, monkeypatch):
    def boom(doc, **kwargs):
        raise RuntimeError("simulated parser crash")
    monkeypatch.setattr(doc_router_main.pymupdf4llm, "to_markdown", boom)
    is_scanned_or_failed, content, meta = doc_router_main._process_pdf_sync(text_pdf_path)
    assert is_scanned_or_failed is True
    assert content == ""
    assert "pymupdf4llm_error" in meta


# ---------- RouterResult / merge_metadata ----------

def test_router_result_destructures_like_tuple(doc_router_main):
    result = doc_router_main.RouterResult("hello", {"parser": "pymupdf4llm"})
    content, metadata = result
    assert content == "hello"
    assert metadata == {"parser": "pymupdf4llm"}
    assert result.content == "hello"
    assert result.metadata["parser"] == "pymupdf4llm"


def test_merge_metadata_combines_in_order(doc_router_main):
    a = {"filename": "x.pdf", "size_bytes": 100}
    b = {"parser": "tika", "filename": "overridden.pdf"}
    merged = doc_router_main.merge_metadata(a, b)
    assert merged["filename"] == "overridden.pdf"
    assert merged["size_bytes"] == 100
    assert merged["parser"] == "tika"


def test_merge_metadata_explicit_parser_wins(doc_router_main):
    a = {"parser": "pymupdf4llm"}
    merged = doc_router_main.merge_metadata(a, parser="mineru")
    assert merged["parser"] == "mineru"


def test_merge_metadata_handles_none_and_empty(doc_router_main):
    merged = doc_router_main.merge_metadata(None, {}, {"k": "v"})
    assert merged == {"k": "v"}


# ---------- JsonFormatter ----------

def test_json_formatter_emits_valid_json(doc_router_main):
    import json
    import logging
    formatter = doc_router_main.JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x", lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    record.duration_ms = 42.5
    record.parser = "pymupdf4llm"
    out = formatter.format(record)
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello world"
    assert payload["duration_ms"] == 42.5
    assert payload["parser"] == "pymupdf4llm"


def test_json_formatter_includes_exception(doc_router_main):
    import json
    import logging
    formatter = doc_router_main.JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="x", lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    out = formatter.format(record)
    payload = json.loads(out)
    assert "ValueError" in payload["exc"]
    assert "boom" in payload["exc"]
