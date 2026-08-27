"""PDF text extraction: embedded text first, OCR only when there is nothing to read.

OCR is expensive and lossy, so it is a fallback, not a default. A PDF goes to OCR only when
the text layer yields fewer than `ocr_trigger_chars_per_page` characters per page, which is
the signature of a scanned document.

The OCR backend (pytesseract + a rasteriser) is an optional dependency. When it is missing,
extraction reports `ocr_unavailable` rather than failing the ingestion run.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from app.enums import ExtractionMethod, ExtractionStatus
from app.logging_config import get_logger

logger = get_logger(__name__)

PDF_MAGIC = b"%PDF-"


@dataclass(slots=True)
class PdfExtraction:
    text: str
    status: ExtractionStatus
    method: ExtractionMethod
    page_count: int = 0
    error: str | None = None
    #: Per-page text, used to build page-accurate evidence locators.
    pages: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status in (
            ExtractionStatus.TEXT_EXTRACTED,
            ExtractionStatus.OCR_EXTRACTED,
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def looks_like_pdf(data: bytes, mime_type: str | None = None) -> bool:
    if data[:5] == PDF_MAGIC:
        return True
    return (mime_type or "").lower() == "application/pdf" and bool(data)


def extract_pdf_text(
    data: bytes,
    *,
    mime_type: str | None = None,
    max_chars: int = 60_000,
    enable_ocr: bool = True,
    ocr_trigger_chars_per_page: int = 40,
) -> PdfExtraction:
    """Extract text from a PDF, escalating to OCR only for scanned documents."""
    if not data:
        return PdfExtraction("", ExtractionStatus.SKIPPED, ExtractionMethod.NONE,
                             error="empty attachment")
    if not looks_like_pdf(data, mime_type):
        return PdfExtraction("", ExtractionStatus.SKIPPED, ExtractionMethod.NONE,
                             error=f"not a PDF (mime={mime_type})")

    pages, page_count, error = _extract_text_layer(data)

    if error is not None and page_count == 0:
        return PdfExtraction("", ExtractionStatus.FAILED, ExtractionMethod.NONE, error=error)

    text = "\n".join(pages).strip()
    density = (len(text) / page_count) if page_count else 0

    if text and density >= ocr_trigger_chars_per_page:
        return PdfExtraction(
            text[:max_chars],
            ExtractionStatus.TEXT_EXTRACTED,
            ExtractionMethod.PYPDF,
            page_count=page_count,
            pages=tuple(pages),
        )

    # Sparse text layer -> the document is probably scanned.
    if not enable_ocr:
        return PdfExtraction(
            text[:max_chars],
            ExtractionStatus.TEXT_EXTRACTED if text else ExtractionStatus.SKIPPED,
            ExtractionMethod.PYPDF if text else ExtractionMethod.NONE,
            page_count=page_count,
            pages=tuple(pages),
            error=None if text else "sparse text layer and OCR disabled",
        )

    ocr_pages, ocr_error = _ocr_pdf(data)
    if ocr_error is not None:
        status = (
            ExtractionStatus.OCR_UNAVAILABLE
            if "not installed" in ocr_error
            else ExtractionStatus.FAILED
        )
        # Keep whatever the text layer gave us rather than discarding partial evidence.
        return PdfExtraction(
            text[:max_chars],
            status,
            ExtractionMethod.PYPDF if text else ExtractionMethod.NONE,
            page_count=page_count,
            pages=tuple(pages),
            error=ocr_error,
        )

    ocr_text = "\n".join(ocr_pages).strip()
    return PdfExtraction(
        ocr_text[:max_chars],
        ExtractionStatus.OCR_EXTRACTED,
        ExtractionMethod.OCR_TESSERACT,
        page_count=page_count or len(ocr_pages),
        pages=tuple(ocr_pages),
    )


def _extract_text_layer(data: bytes) -> tuple[list[str], int, str | None]:
    """Read the embedded text layer. Prefers PyMuPDF when installed, else pypdf."""
    try:
        import fitz  # type: ignore  # PyMuPDF, optional

        with fitz.open(stream=data, filetype="pdf") as doc:
            return [page.get_text() or "" for page in doc], doc.page_count, None
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - corrupt PDF path
        logger.warning("pymupdf extraction failed, falling back to pypdf", extra={"error": str(exc)})

    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An empty-password decrypt covers the common "protected but not secret" case.
            try:
                reader.decrypt("")
            except Exception:
                return [], 0, "PDF is password protected"
        pages = [(page.extract_text() or "") for page in reader.pages]
        return pages, len(pages), None
    except PdfReadError as exc:
        return [], 0, f"unreadable PDF: {exc}"
    except Exception as exc:
        return [], 0, f"pdf extraction failed: {exc}"


def _ocr_pdf(data: bytes) -> tuple[list[str], str | None]:
    """OCR every page. Returns ``(pages, error)``; error is set when OCR cannot run."""
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return [], "OCR backend not installed (pip install pytesseract, plus tesseract-ocr)"

    images, error = _rasterise(data)
    if error is not None:
        return [], error
    try:
        return [pytesseract.image_to_string(img) for img in images], None
    except Exception as exc:  # pragma: no cover - depends on system tesseract
        return [], f"OCR failed: {exc}"


def _rasterise(data: bytes) -> tuple[list, str | None]:
    """Turn PDF pages into images using whichever rasteriser is available."""
    try:
        import fitz  # type: ignore

        from PIL import Image  # type: ignore

        images = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=200)
                images.append(Image.open(io.BytesIO(pix.tobytes("png"))))
        return images, None
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover
        return [], f"rasterisation failed: {exc}"

    try:
        from pdf2image import convert_from_bytes  # type: ignore

        return convert_from_bytes(data, dpi=200), None
    except ImportError:
        return [], "OCR backend not installed (no PyMuPDF/Pillow or pdf2image rasteriser)"
    except Exception as exc:  # pragma: no cover
        return [], f"rasterisation failed: {exc}"
