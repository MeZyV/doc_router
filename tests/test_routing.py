import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response


@pytest.fixture
def client(doc_router_main):
    with TestClient(doc_router_main.app) as c:
        yield c


def test_process_rejects_empty_body(client):
    r = client.put("/process", content=b"", headers={"X-Filename": "x.pdf"})
    assert r.status_code == 400


def test_process_rejects_oversize_by_content_length(client, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(doc_router_helpers, "MAX_UPLOAD_MB", 0)
    r = client.put(
        "/process",
        content=b"x" * 100,
        headers={"X-Filename": "big.pdf", "Content-Length": "100"},
    )
    assert r.status_code == 413


def test_process_rejects_oversize_by_stream(client, doc_router_helpers, monkeypatch):
    """
    Even if Content-Length is missing or lying, the chunked stream check must
    still cut off the upload at MAX_UPLOAD_BYTES.
    """
    monkeypatch.setattr(doc_router_helpers, "MAX_UPLOAD_BYTES", 10)
    r = client.put(
        "/process",
        content=b"x" * 100,
        headers={"X-Filename": "big.pdf"},
    )
    assert r.status_code == 413


def test_process_rejects_unauthorized(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    with TestClient(doc_router_main.app) as c:
        r = c.put("/process", content=b"abc", headers={"X-Filename": "x.pdf"})
    assert r.status_code == 401


def test_process_accepts_valid_token(doc_router_main, doc_router_helpers, monkeypatch, text_pdf_bytes, stub_pymupdf4llm):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    with TestClient(doc_router_main.app) as c:
        r = c.put(
            "/process",
            content=text_pdf_bytes,
            headers={
                "X-Filename": "x.pdf",
                "Content-Type": "application/pdf",
                "Authorization": "Bearer secret",
            },
        )
    assert r.status_code == 200


def test_pdf_mime_variants_routed_as_pdf(client, doc_router_main, text_pdf_bytes, stub_pymupdf4llm):
    """
    `application/x-pdf` and an upper-cased `Content-Type` with charset must
    both be recognized as PDF.
    """
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "weird.pdf", "Content-Type": "Application/X-PDF; charset=binary"},
    )
    assert r.status_code == 200
    assert r.json()["metadata"]["parser"] == "pymupdf4llm"


# ---------- Local pymupdf4llm path (default) ----------

def test_text_pdf_uses_local_pymupdf(client, text_pdf_bytes, stub_pymupdf4llm):
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "text.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["metadata"]["parser"] == "pymupdf4llm"
    assert "Stubbed Markdown" in data["page_content"]
    assert data["metadata"]["pdf_detected_scanned_or_text_poor"] is False
    assert data["metadata"]["pdf_page_count"] >= 1
    assert len(stub_pymupdf4llm) == 1


@respx.mock
def test_scanned_pdf_falls_back_to_mineru(client, doc_router_main, empty_pdf_bytes, stub_pymupdf4llm):
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(200, json={"results": [{"md_content": "OCRed content"}]})
    )
    r = client.put(
        "/process",
        content=empty_pdf_bytes,
        headers={"X-Filename": "scan.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["metadata"]["parser"] == "mineru"
    assert "OCRed content" in data["page_content"]
    assert data["metadata"]["pdf_detected_scanned_or_text_poor"] is True
    assert len(stub_pymupdf4llm) == 0


@respx.mock
def test_local_pymupdf_empty_falls_back_to_mineru(client, doc_router_main, text_pdf_bytes, monkeypatch):
    monkeypatch.setattr(doc_router_main.pymupdf4llm, "to_markdown", lambda doc, **kw: "")
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(200, json={"md_content": "Fallback content"})
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "edge.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["metadata"]["parser"] == "mineru"
    assert "Fallback content" in data["page_content"]


@respx.mock
def test_local_pymupdf_crash_falls_back_to_mineru(client, doc_router_main, text_pdf_bytes, monkeypatch):
    def boom(doc, **kw):
        raise RuntimeError("simulated parser crash")
    monkeypatch.setattr(doc_router_main.pymupdf4llm, "to_markdown", boom)
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(200, json={"md_content": "Recovered"})
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "broken.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200
    assert r.json()["metadata"]["parser"] == "mineru"


# ---------- Remote pymupdf4llm path (opt-in) ----------

@respx.mock
def test_remote_pymupdf_path(doc_router_main, doc_router_helpers, monkeypatch, text_pdf_bytes):
    monkeypatch.setattr(doc_router_helpers, "USE_REMOTE_PYMUPDF", True)
    respx.put(f"{doc_router_main.PYMUPDF4LLM_URL}/process").mock(
        return_value=Response(
            200,
            json={"page_content": "remote content", "metadata": {"parser": "pymupdf4llm"}},
        )
    )
    with TestClient(doc_router_main.app) as c:
        r = c.put(
            "/process",
            content=text_pdf_bytes,
            headers={"X-Filename": "text.pdf", "Content-Type": "application/pdf"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["metadata"]["parser"] == "pymupdf4llm"
    assert data["page_content"] == "remote content"


# ---------- Non-PDF routing ----------

@respx.mock
def test_docx_routes_to_tika(client, doc_router_main):
    respx.put(f"{doc_router_main.TIKA_URL}/tika").mock(
        return_value=Response(200, text="extracted docx text")
    )
    r = client.put(
        "/process",
        content=b"fake docx bytes",
        headers={
            "X-Filename": "test.docx",
            "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["metadata"]["parser"] == "tika"
    assert "extracted docx text" in data["page_content"]


@respx.mock
def test_image_routes_to_mineru(client, doc_router_main):
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(200, json={"md_content": "image OCR result"})
    )
    r = client.put(
        "/process",
        content=b"\x89PNG\r\n\x1a\n" + b"fake",
        headers={"X-Filename": "scan.png", "Content-Type": "image/png"},
    )
    assert r.status_code == 200
    assert r.json()["metadata"]["parser"] == "mineru"


@respx.mock
def test_unknown_type_tries_tika_then_mineru(client, doc_router_main):
    respx.put(f"{doc_router_main.TIKA_URL}/tika").mock(
        return_value=Response(200, text="   ")
    )
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(200, json={"md_content": "mineru wins"})
    )
    r = client.put(
        "/process",
        content=b"some bytes",
        headers={"X-Filename": "weird.bin", "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200
    assert r.json()["metadata"]["parser"] == "mineru"


# ---------- Error paths ----------

@respx.mock
def test_all_pdf_upstreams_fail_returns_502(client, doc_router_main, text_pdf_bytes, monkeypatch):
    def boom(doc, **kw):
        raise RuntimeError("pymupdf4llm crashed")
    monkeypatch.setattr(doc_router_main.pymupdf4llm, "to_markdown", boom)
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(500, text="boom")
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "boom.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 502


# ---------- Upstream retry ----------

@respx.mock
def test_retry_recovers_from_503(client, doc_router_main, doc_router_helpers, doc_router_pdf, text_pdf_bytes, monkeypatch):
    """Transient 503 on first MinerU call, success on second → 200 from /process."""
    def boom(doc, **kw):
        raise RuntimeError("pymupdf4llm crashed, fallback to MinerU")
    monkeypatch.setattr(doc_router_pdf.pymupdf4llm, "to_markdown", boom)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRIES", 1)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRY_BACKOFF_SECONDS", 0.0)

    route = respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        side_effect=[Response(503, text="busy"), Response(200, json={"md_content": "recovered"})]
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "retry.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["parser"] == "mineru"
    assert "recovered" in r.json()["page_content"]
    assert route.call_count == 2


@respx.mock
def test_retry_exhausts_returns_502(client, doc_router_main, doc_router_helpers, doc_router_pdf, text_pdf_bytes, monkeypatch):
    """Two consecutive 503s with UPSTREAM_RETRIES=1 → still 502 to OpenWebUI."""
    def boom(doc, **kw):
        raise RuntimeError("pymupdf4llm crashed")
    monkeypatch.setattr(doc_router_pdf.pymupdf4llm, "to_markdown", boom)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRIES", 1)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRY_BACKOFF_SECONDS", 0.0)

    route = respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        side_effect=[Response(503, text="busy"), Response(503, text="still busy")]
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "retry.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 502
    assert route.call_count == 2


@respx.mock
def test_retry_disabled_does_not_retry(client, doc_router_main, doc_router_helpers, doc_router_pdf, text_pdf_bytes, monkeypatch):
    """UPSTREAM_RETRIES=0 → one attempt only, 503 surfaces immediately."""
    def boom(doc, **kw):
        raise RuntimeError("pymupdf4llm crashed")
    monkeypatch.setattr(doc_router_pdf.pymupdf4llm, "to_markdown", boom)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRIES", 0)

    route = respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(503, text="busy")
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "retry.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 502
    assert route.call_count == 1


@respx.mock
def test_retry_does_not_apply_to_non_retryable_status(client, doc_router_main, doc_router_helpers, doc_router_pdf, text_pdf_bytes, monkeypatch):
    """500 is NOT in RETRYABLE_STATUSES → no retry, single call."""
    def boom(doc, **kw):
        raise RuntimeError("pymupdf4llm crashed")
    monkeypatch.setattr(doc_router_pdf.pymupdf4llm, "to_markdown", boom)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRIES", 3)

    route = respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(500, text="hard fail")
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "retry.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 502
    assert route.call_count == 1


@respx.mock
def test_retry_recovers_from_transport_error(client, doc_router_main, doc_router_helpers, doc_router_pdf, text_pdf_bytes, monkeypatch):
    """httpx.ConnectError on first attempt, success on second → 200."""
    import httpx as _httpx

    def boom(doc, **kw):
        raise RuntimeError("pymupdf4llm crashed")
    monkeypatch.setattr(doc_router_pdf.pymupdf4llm, "to_markdown", boom)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRIES", 1)
    monkeypatch.setattr(doc_router_helpers, "UPSTREAM_RETRY_BACKOFF_SECONDS", 0.0)

    route = respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        side_effect=[
            _httpx.ConnectError("connection reset"),
            Response(200, json={"md_content": "recovered after transport error"}),
        ]
    )
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "retry.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200, r.text
    assert "recovered after transport error" in r.json()["page_content"]
    assert route.call_count == 2


# ---------- Tempfile cleanup ----------

def test_tempfile_cleaned_up_on_success(client, doc_router_main, text_pdf_bytes, stub_pymupdf4llm):
    import os
    tmp_dir = doc_router_main.TMP_DIR
    before = set(os.listdir(tmp_dir))
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "text.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200
    after = set(os.listdir(tmp_dir))
    assert after == before, f"Leftover temp files: {after - before}"


@respx.mock
def test_tempfile_cleaned_up_on_upstream_failure(client, doc_router_main, text_pdf_bytes, monkeypatch):
    import os
    monkeypatch.setattr(doc_router_main.pymupdf4llm, "to_markdown", lambda doc, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    respx.post(f"{doc_router_main.MINERU_ROUTER_URL}/file_parse").mock(
        return_value=Response(500, text="boom")
    )
    tmp_dir = doc_router_main.TMP_DIR
    before = set(os.listdir(tmp_dir))
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "text.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 502
    after = set(os.listdir(tmp_dir))
    assert after == before


# ---------- Health ----------

def test_health_live_public(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_health_live_public_even_with_api_key(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    with TestClient(doc_router_main.app) as c:
        r = c.get("/health/live")
    assert r.status_code == 200


def test_health_ready_requires_auth_when_key_set(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    with TestClient(doc_router_main.app) as c:
        r = c.get("/health/ready")
    assert r.status_code == 401


@respx.mock
def test_health_ready_accepts_valid_auth(doc_router_main, doc_router_helpers, monkeypatch):
    monkeypatch.setattr(doc_router_helpers, "EXTERNAL_API_KEY", "secret")
    respx.get(f"{doc_router_main.MINERU_ROUTER_URL}/health").mock(return_value=Response(200))
    respx.get(f"{doc_router_main.TIKA_URL}/tika").mock(return_value=Response(200))
    with TestClient(doc_router_main.app) as c:
        r = c.get("/health/ready", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


@respx.mock
def test_health_ready_local_mode_open_when_no_key(client, doc_router_main):
    respx.get(f"{doc_router_main.MINERU_ROUTER_URL}/health").mock(return_value=Response(200))
    respx.get(f"{doc_router_main.TIKA_URL}/tika").mock(return_value=Response(200))
    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    assert data["checks"]["pymupdf4llm"]["mode"] == "local"


@respx.mock
def test_health_ready_degraded(client, doc_router_main):
    respx.get(f"{doc_router_main.MINERU_ROUTER_URL}/health").mock(return_value=Response(500))
    respx.get(f"{doc_router_main.TIKA_URL}/tika").mock(return_value=Response(200))
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


# ---------- Logging middleware ----------

def test_logging_middleware_captures_parser_and_size(client, doc_router_main, text_pdf_bytes, stub_pymupdf4llm, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="doc-router"):
        r = client.put(
            "/process",
            content=text_pdf_bytes,
            headers={"X-Filename": "log.pdf", "Content-Type": "application/pdf"},
        )
    assert r.status_code == 200
    records = [rec for rec in caplog.records if rec.name == "doc-router" and rec.getMessage() == "request"]
    assert records, "logging middleware did not emit a 'request' event"
    rec = records[-1]
    assert rec.status_code == 200
    assert rec.method == "PUT"
    assert rec.path == "/process"
    assert rec.parser == "pymupdf4llm"
    assert rec.filename == "log.pdf"
    assert rec.size_bytes == len(text_pdf_bytes)
    assert isinstance(rec.duration_ms, float)


def test_logging_middleware_records_http_errors(client, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="doc-router"):
        r = client.put("/process", content=b"", headers={"X-Filename": "empty.pdf"})
    assert r.status_code == 400
    records = [rec for rec in caplog.records if rec.name == "doc-router" and rec.getMessage() == "request"]
    assert records
    assert records[-1].status_code == 400
