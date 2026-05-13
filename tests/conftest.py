import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def monkeypatch_session():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="session")
def _doc_router_modules(tmp_path_factory, monkeypatch_session):
    """
    Load the four doc-router submodules (helpers, pdf, upstream, main) in a
    way that supports their intra-package imports (`from helpers import ...`).
    The doc-router directory is not a valid Python package (hyphen in name),
    so we put it on sys.path and let regular `import` resolve siblings.
    Returns a dict of loaded modules.
    """
    tmp = tmp_path_factory.mktemp("doc_router")
    monkeypatch_session.setenv("TMP_DIR", str(tmp))

    doc_router_dir = REPO_ROOT / "doc-router"
    sys.path.insert(0, str(doc_router_dir))
    try:
        # Drop any pre-existing cached modules with the same names (e.g. from
        # a previous test session) so env vars are re-read.
        for name in ("helpers", "pdf", "upstream", "main"):
            sys.modules.pop(name, None)
        helpers = importlib.import_module("helpers")
        pdf = importlib.import_module("pdf")
        upstream = importlib.import_module("upstream")
        main = importlib.import_module("main")
    finally:
        # Keep doc-router on sys.path for the session — tests may re-import.
        pass

    return {"helpers": helpers, "pdf": pdf, "upstream": upstream, "main": main}


@pytest.fixture(scope="session")
def doc_router_main(_doc_router_modules):
    """
    The FastAPI module. Exposes `app` and re-exports config/helpers/types from
    sibling modules for test convenience (e.g. `doc_router_main.normalize_filename`).
    For monkeypatching config values that drive runtime behavior, use the
    `doc_router_helpers` fixture instead — patches there reach the consumers.
    """
    return _doc_router_modules["main"]


@pytest.fixture(scope="session")
def doc_router_helpers(_doc_router_modules):
    """
    The helpers module. Patch config values here (UPSTREAM_RETRIES,
    EXTERNAL_API_KEY, MAX_UPLOAD_BYTES, USE_REMOTE_PYMUPDF, ...) — `upstream.py`
    reads them via the `helpers` namespace at call time.
    """
    return _doc_router_modules["helpers"]


@pytest.fixture(scope="session")
def doc_router_pdf(_doc_router_modules):
    """The in-process PDF processing module."""
    return _doc_router_modules["pdf"]


@pytest.fixture(scope="session")
def pymupdf_main(tmp_path_factory, monkeypatch_session):
    tmp = tmp_path_factory.mktemp("pymupdf_api")
    monkeypatch_session.setenv("TMP_DIR", str(tmp))
    return _load_module("pymupdf_main", REPO_ROOT / "pymupdf4llm-api" / "main.py")


@pytest.fixture
def stub_pymupdf4llm(doc_router_pdf, monkeypatch):
    """
    Replace pymupdf4llm.to_markdown with a deterministic stub so routing tests
    do not depend on the real markdown extraction. Patches the module attribute
    so all importers (pdf.py uses `pymupdf4llm.to_markdown(doc)`) see the stub.
    """
    calls = []

    def fake_to_markdown(doc, **kwargs):
        calls.append({"page_count": len(doc), "kwargs": kwargs})
        return "# Stubbed Markdown\n\nFake extracted content."

    monkeypatch.setattr(doc_router_pdf.pymupdf4llm, "to_markdown", fake_to_markdown)
    return calls


@pytest.fixture
def text_pdf_bytes():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Hello, this is a textual PDF for unit tests.\n" * 10
        + "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    )
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def empty_pdf_bytes():
    import fitz
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def text_pdf_path(text_pdf_bytes, tmp_path):
    p = tmp_path / "text.pdf"
    p.write_bytes(text_pdf_bytes)
    return p


@pytest.fixture
def empty_pdf_path(empty_pdf_bytes, tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(empty_pdf_bytes)
    return p


@pytest.fixture
def invalid_pdf_path(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf")
    return p
