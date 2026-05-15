"""Tests for document loader — individual loaders, factory, and backward compat."""
import textwrap
import pytest

from src.config.config_loader import LoaderConfig
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import Document
from src.loader.document_loader import DocumentLoader
from src.loader.loader_factory import DocumentLoaderFactory
from src.loader.adoc_loader import AdocLoader
from src.loader.csv_loader import CSVLoader
from src.loader.html_loader import HTMLLoader
from src.loader.text_loader import TextLoader
from src.chunker.base_chunker import GenericChunker


@pytest.fixture
def loader_cfg():
    return LoaderConfig(chunk_size=200, chunk_overlap=20)


@pytest.fixture
def factory(loader_cfg):
    return DocumentLoaderFactory(loader_cfg)


@pytest.fixture
def loader(loader_cfg):
    return DocumentLoader(loader_cfg)


# ---------------------------------------------------------------------------
# TextLoader
# ---------------------------------------------------------------------------

def test_load_text_via_factory(factory):
    doc = FetchedDocument(path="readme.txt", content="Hello world. " * 20)
    chunks = factory.load_one(doc)
    assert len(chunks) >= 1
    assert all(isinstance(c, Document) for c in chunks)


def test_load_text_direct():
    chunker = GenericChunker(chunk_size=100, chunk_overlap=10)
    tl = TextLoader(chunker)
    doc = FetchedDocument(path="a.md", content="x" * 300, metadata={})
    chunks = tl.load(doc)
    assert len(chunks) > 1
    assert all(isinstance(c, Document) for c in chunks)


# ---------------------------------------------------------------------------
# AdocLoader
# ---------------------------------------------------------------------------

def test_load_adoc_section_split(factory):
    adoc = textwrap.dedent("""\
        = Document Title

        Some preamble text.

        == Section One

        Content of section one.

        === SubSection

        Content of subsection.

        == Section Two

        Content of section two.
        """)
    doc = FetchedDocument(path="spec.adoc", content=adoc)
    chunks = factory.load_one(doc)
    sections = [c.metadata.get("section") for c in chunks]
    assert any("Section" in (s or "") for s in sections)


def test_adoc_no_split():
    chunker = GenericChunker(chunk_size=500, chunk_overlap=50)
    al = AdocLoader(chunker, section_split=False)
    doc = FetchedDocument(path="spec.adoc", content="== Title\nBody content.", metadata={})
    chunks = al.load(doc)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# CSVLoader
# ---------------------------------------------------------------------------

def test_load_csv(factory):
    csv_content = "id,title,description\n1,TC-001,Test OnOff\n2,TC-002,Test Level\n"
    doc = FetchedDocument(path="tests.csv", content=csv_content)
    chunks = factory.load_one(doc)
    assert len(chunks) >= 1
    combined = " ".join(c.page_content for c in chunks)
    assert "TC-001" in combined or "TC-002" in combined


def test_load_csv_direct():
    chunker = GenericChunker(chunk_size=200, chunk_overlap=20)
    cl = CSVLoader(chunker)
    csv_c = "a,b\n1,2\n3,4\n"
    doc = FetchedDocument(path="x.csv", content=csv_c, metadata={})
    chunks = cl.load(doc)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# GenericChunker overlap
# ---------------------------------------------------------------------------

def test_chunk_overlap(loader):
    text = "A" * 500
    doc = FetchedDocument(path="long.txt", content=text)
    chunks = loader.load(doc)
    assert len(chunks) > 1
    end = chunks[0].page_content[-20:]
    assert end[:10] in chunks[1].page_content


# ---------------------------------------------------------------------------
# DocumentLoaderFactory load_all
# ---------------------------------------------------------------------------

def test_load_all(factory):
    docs = [
        FetchedDocument(path="a.txt", content="foo bar baz"),
        FetchedDocument(path="b.adoc", content="== Title\nsome content"),
    ]
    all_chunks = factory.load_all(docs)
    assert len(all_chunks) >= 2


# ---------------------------------------------------------------------------
# Backward-compat DocumentLoader wrapper
# ---------------------------------------------------------------------------

def test_backward_compat_document_loader(loader_cfg):
    loader = DocumentLoader(loader_cfg)
    doc = FetchedDocument(path="test.txt", content="hello world")
    chunks = loader.load(doc)
    assert isinstance(chunks[0], Document)


def test_backward_compat_load_all(loader_cfg):
    loader = DocumentLoader(loader_cfg)
    docs = [FetchedDocument(path="a.txt", content="foo"), FetchedDocument(path="b.txt", content="bar")]
    all_chunks = loader.load_all(docs)
    assert len(all_chunks) >= 2


# ---------------------------------------------------------------------------
# Factory: get_loader
# ---------------------------------------------------------------------------

def test_factory_get_loader_extensions(factory):
    from src.loader.adoc_loader import AdocLoader
    from src.loader.csv_loader import CSVLoader
    from src.loader.html_loader import HTMLLoader
    from src.loader.pdf_loader import PDFLoader

    assert isinstance(factory.get_loader(".pdf"), PDFLoader)
    assert isinstance(factory.get_loader(".adoc"), AdocLoader)
    assert isinstance(factory.get_loader(".csv"), CSVLoader)
    assert isinstance(factory.get_loader(".html"), HTMLLoader)
    assert isinstance(factory.get_loader(".htm"), HTMLLoader)


# ---------------------------------------------------------------------------
# HTMLLoader
# ---------------------------------------------------------------------------

SAMPLE_HTML = textwrap.dedent("""\
    <html>
    <body>
    <p>Introduction paragraph before any heading.</p>
    <h1>Overview</h1>
    <p>This section covers the overview.</p>
    <h2>Subsection A</h2>
    <p>Content of subsection A.</p>
    <h2>Subsection B</h2>
    <p>Content of subsection B.</p>
    <h3>Deep heading</h3>
    <p>Nested content here.</p>
    </body>
    </html>
    """)


def test_html_loader_returns_documents():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = hl.load(doc)
    assert len(chunks) >= 1
    assert all(isinstance(c, Document) for c in chunks)


def test_html_loader_heading_metadata():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = hl.load(doc)
    headings = {c.metadata.get("heading") for c in chunks}
    assert "Overview" in headings
    assert "Subsection A" in headings


def test_html_loader_heading_levels():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = hl.load(doc)
    by_heading = {c.metadata["heading"]: c.metadata["heading_level"]
                  for c in chunks if c.metadata.get("heading")}
    assert by_heading.get("Overview") == 1
    assert by_heading.get("Subsection A") == 2
    assert by_heading.get("Deep heading") == 3


def test_html_loader_content_in_chunks():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = hl.load(doc)
    combined = " ".join(c.page_content for c in chunks)
    assert "overview" in combined.lower()
    assert "subsection A" in combined or "Subsection A" in combined


def test_html_loader_chunk_index_sequence():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = hl.load(doc)
    indices = [c.metadata["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))


def test_html_loader_preamble_chunk():
    """Content before the first heading should appear as a preamble or be included."""
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = hl.load(doc)
    combined = " ".join(c.page_content for c in chunks)
    assert "Introduction" in combined


def test_html_loader_structureless_falls_back():
    """HTML with no headings should still produce at least one chunk."""
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(
        path="flat.html",
        content="<p>Just some flat text without any headings at all.</p>",
        metadata={},
    )
    chunks = hl.load(doc)
    assert len(chunks) >= 1
    assert all(c.page_content for c in chunks)


def test_html_loader_empty_body():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="empty.html", content="", metadata={})
    chunks = hl.load(doc)
    assert chunks == []


def test_html_loader_via_factory(factory):
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML, metadata={})
    chunks = factory.load_one(doc)
    assert len(chunks) >= 1
    assert all(isinstance(c, Document) for c in chunks)


def test_html_loader_metadata_preserved():
    hl = HTMLLoader(GenericChunker(chunk_size=500, chunk_overlap=50))
    doc = FetchedDocument(path="page.html", content=SAMPLE_HTML,
                          metadata={"source": "url", "url": "https://example.com"})
    chunks = hl.load(doc)
    for c in chunks:
        assert c.metadata.get("source") == "url"
        assert c.metadata.get("url") == "https://example.com"
