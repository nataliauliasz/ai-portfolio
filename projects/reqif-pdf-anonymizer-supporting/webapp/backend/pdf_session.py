from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Dict, List, Tuple

import fitz


BlockTuple = Tuple[float, float, float, float, str, float, str, str]


class WebPDFDocument:
    """Stateful helper around PyMuPDF used by the web viewer."""

    def __init__(self, file_path: Path, zoom: int = 3) -> None:
        self.file_path = Path(file_path)
        self.doc = fitz.open(str(self.file_path))
        self.zoom = zoom
        self.line_spacing = 5
        self.header_height = 0
        self.footer_height = 0
        self._lock = threading.RLock()

    def has_document(self) -> bool:
        return self.doc is not None

    def clone(self) -> "WebPDFDocument":
        if self.doc is None:
            raise RuntimeError("Document already closed")
        clone = WebPDFDocument(self.file_path, zoom=self.zoom)
        clone.line_spacing = self.line_spacing
        clone.header_height = self.header_height
        clone.footer_height = self.footer_height
        return clone

    def page_count(self) -> int:
        if self.doc is None:
            return 0
        return len(self.doc)

    def render_page(self, page_num: int) -> Tuple[str, int, int]:
        if self.doc is None:
            raise RuntimeError("Document not available")
        with self._lock:
            page = self.doc[page_num]
            pix = page.get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom))
            data = base64.b64encode(pix.tobytes("png")).decode("ascii")
            return data, pix.width, pix.height

    def get_blocks(self, page_num: int) -> List[BlockTuple]:
        if self.doc is None:
            return []
        with self._lock:
            if page_num < 0 or page_num >= len(self.doc):
                return []

            page = self.doc[page_num]
            spans_data = page.get_text("dict")
            if not spans_data or "blocks" not in spans_data:
                return []

            page_height = page.rect.height * self.zoom
            blocks: List[BlockTuple] = []

            for block in spans_data["blocks"]:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if not text:
                            continue

                        x0, y0, x1, y1 = span["bbox"]
                        size = span["size"]
                        font = span["font"]
                        flags = span.get("flags", 0)

                        style_parts = []
                        if flags & 16:
                            style_parts.append("bold")
                        if flags & 2:
                            style_parts.append("italic")
                        if not style_parts:
                            style_parts.append("normal")
                        font_style = "-".join(style_parts)

                        x0_s, y0_s, x1_s, y1_s = (
                            x0 * self.zoom,
                            y0 * self.zoom,
                            x1 * self.zoom,
                            y1 * self.zoom,
                        )

                        if y0_s < self.header_height or y1_s > (page_height - self.footer_height):
                            continue

                        blocks.append(
                            (
                                x0_s,
                                y0_s,
                                x1_s - x0_s,
                                y1_s - y0_s,
                                text,
                                size,
                                font,
                                font_style,
                            )
                        )

            return blocks

    def close(self) -> None:
        with self._lock:
            if self.doc is not None:
                self.doc.close()
                self.doc = None


class PDFSession:
    """Tracks an uploaded PDF and user-specific settings."""

    def __init__(self, file_path: Path, original_name: str | None = None) -> None:
        self.file_path = Path(file_path)
        self.original_name = original_name or self.file_path.name
        self.document = WebPDFDocument(self.file_path)
        self.lock = threading.RLock()

    def update_settings(
        self,
        *,
        line_spacing: int | None = None,
        header: int | None = None,
        footer: int | None = None,
    ) -> None:
        with self.lock:
            if line_spacing is not None:
                self.document.line_spacing = max(0, int(line_spacing))
            if header is not None:
                self.document.header_height = max(0, int(header))
            if footer is not None:
                self.document.footer_height = max(0, int(footer))

    def page_count(self) -> int:
        with self.lock:
            return self.document.page_count()

    def render_page(self, page_num: int) -> Dict[str, object]:
        with self.lock:
            data, width, height = self.document.render_page(page_num)
            blocks = self.document.get_blocks(page_num)

            header_line = self.document.header_height or None
            footer_line = None
            if self.document.footer_height:
                page = self.document.doc[page_num]
                page_height = page.rect.height * self.document.zoom
                footer_line = page_height - self.document.footer_height

            return {
                "image": data,
                "width": width,
                "height": height,
                "blocks": blocks,
                "header": header_line,
                "footer": footer_line,
            }

    def clone_document(self) -> WebPDFDocument:
        with self.lock:
            return self.document.clone()

    def close(self) -> None:
        with self.lock:
            self.document.close()
