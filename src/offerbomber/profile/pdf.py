"""PDF text extraction layer.

A scanned PDF yields little or no text; the importer reports that explicitly
instead of pretending the parser understood the document.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pymupdf as fitz  # PyMuPDF; new import name avoids the deprecated fitz alias


@dataclass(frozen=True)
class PDFDocument:
    file_name: str
    file_sha256: str
    page_count: int
    pages: tuple[str, ...]
    text: str
    extractable: bool

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class PDFTextExtractor(Protocol):
    def extract(self, path: str | Path) -> PDFDocument: ...


class PyMuPDFExtractor:
    """Deterministic text extraction. No OCR and no model inference."""

    min_chars_per_page = 20

    def extract(self, path: str | Path) -> PDFDocument:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"PDF not found: {source}")
        raw = source.read_bytes()
        with fitz.open(stream=raw, filetype="pdf") as document:
            pages = tuple(page.get_text("text") for page in document)
            page_count = document.page_count
        text = "\n".join(pages)
        extractable = page_count > 0 and sum(len(p.strip()) for p in pages) >= self.min_chars_per_page
        return PDFDocument(
            file_name=source.name,
            file_sha256=hashlib.sha256(raw).hexdigest(),
            page_count=page_count,
            pages=pages,
            text=text,
            extractable=extractable,
        )
