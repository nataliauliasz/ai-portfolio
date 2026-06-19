# pdf_utils.py
import fitz  # PyMuPDF
from PySide6.QtGui import QPixmap, QImage


# Variable global para controlar zoom (se puede cambiar desde la GUI)
ZOOM = 3


class PDFDocument:
    def __init__(self, zoom: int = ZOOM):
        self.doc = None
        self.current_page = 0
        self.zoom = zoom
        self.line_spacing = 5  # umbral en píxeles para agrupar líneas
        self.line_spacing = 5
        self.header_height = 0   # comiemzo del header en píxeles
        self.footer_height = 0   # comienzo del header en píxeles


    def open(self, file_path: str):
        """Abrir un PDF desde ruta"""
        self.doc = fitz.open(file_path)
        self.current_page = 0

    def has_document(self) -> bool:
        return self.doc is not None

    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def render_page(self, page_num: int):
        """Renderizar página a QPixmap"""
        if not self.doc:
            return None
        page = self.doc[page_num]
        matrix = fitz.Matrix(self.zoom, self.zoom)
        pix = page.get_pixmap(matrix=matrix)
        img = QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888
        )
        return QPixmap.fromImage(img)

    def get_blocks(self, page_num: int):
        if not self.doc:
            return []

        page = self.doc[page_num]
        spans_data = page.get_text("dict")
        if not spans_data or "blocks" not in spans_data:
            return []

        # Altura de página en píxeles escalada
        page_height = page.rect.height * self.zoom

        blocks = []

        for b in spans_data["blocks"]:
            if "lines" not in b:
                continue  # ignorar imágenes u otros tipos

            for l in b["lines"]:
                for s in l["spans"]:
                    text = s["text"].strip()
                    if not text:
                        continue

                    x0, y0, x1, y1 = s["bbox"]
                    size = s["size"]
                    font = s["font"]
                    flags = s.get("flags", 0)

                    # Detectar estilo
                    style = []
                    if flags & 16:
                        style.append("bold")
                    if flags & 2:
                        style.append("italic")
                    if not style:
                        style.append("normal")
                    font_style = "-".join(style)

                    # Escalar coordenadas al zoom
                    x0_s, y0_s, x1_s, y1_s = x0 * self.zoom, y0 * self.zoom, x1 * self.zoom, y1 * self.zoom

                    # Filtrar header/footer
                    if y0_s < self.header_height or y1_s > (page_height - self.footer_height):
                        continue

                    blocks.append((
                        x0_s,                 # x
                        y0_s,                 # y
                        x1_s - x0_s,          # ancho
                        y1_s - y0_s,          # alto
                        text,                 # texto
                        size,                 # tamaño de fuente
                        font,                 # fuente
                        font_style            # estilo detectado
                    ))


        return blocks



    def get_blocks_(self, page_num: int):
        if not self.doc:
            return []

        page = self.doc[page_num]
        words = page.get_text("words")
        spans_data = page.get_text("dict")  # 🔹 para fuente y tamaño
        if not words:
            return []

        # Altura total de la página en píxeles (con zoom aplicado)
        page_height = page.rect.height * self.zoom

        scaled_words = [
            (x0 * self.zoom, y0 * self.zoom, x1 * self.zoom, y1 * self.zoom, text)
            for (x0, y0, x1, y1, text, *_rest) in words
        ]

        # 🔹 Filtrar header y footer
        scaled_words = [
            w for w in scaled_words
            if w[1] >= self.header_height and w[3] <= (page_height - self.footer_height)
        ]

        # Ordenar por Y primero
        scaled_words.sort(key=lambda w: w[1])  # orden por y0

        blocks = []
        current_line = []
        current_texts = []
        last_y = None

        for (x0, y0, x1, y1, text) in scaled_words:
            if last_y is None:
                current_line.append((x0, y0, x1, y1, text))
                current_texts.append(text)
                last_y = y0
                continue

            vertical_gap = abs(y0 - last_y)

            if vertical_gap <= self.line_spacing:
                # Misma línea
                current_line.append((x0, y0, x1, y1, text))
                current_texts.append(text)
            else:
                # Cerrar línea actual → bloque
                x0_b = min(w[0] for w in current_line)
                y0_b = min(w[1] for w in current_line)
                x1_b = max(w[2] for w in current_line)
                y1_b = max(w[3] for w in current_line)
                block_text = " ".join(current_texts)

                # 🔹 Calcular font size y estilo aproximado
                font_size, font_name, font_style = self._extract_font_info(spans_data, x0_b, y0_b, x1_b, y1_b)
                blocks.append((x0_b, y0_b, x1_b - x0_b, y1_b - y0_b, block_text, font_size, font_name, font_style))

                # Nueva línea
                current_line = [(x0, y0, x1, y1, text)]
                current_texts = [text]

            last_y = y0

        # Guardar última línea
        if current_line:
            x0_b = min(w[0] for w in current_line)
            y0_b = min(w[1] for w in current_line)
            x1_b = max(w[2] for w in current_line)
            y1_b = max(w[3] for w in current_line)
            block_text = " ".join(current_texts)

            font_size, font_name, font_style = self._extract_font_info(spans_data, x0_b, y0_b, x1_b, y1_b)
            blocks.append((x0_b, y0_b, x1_b - x0_b, y1_b - y0_b, block_text, font_size, font_name, font_style))


        # 🔹 DEBUG: imprimir info de cada bloque
        for i, (x, y, w, h, text, size, font, style) in enumerate(blocks, start=1):
            print(f"[Página {page_num+1}] Bloque {i}: "
                  f"x={x:.2f}, y={y:.2f}, w={w:.2f}, h={h:.2f}, "
                  f"texto='{text}', size={size:.1f}, font='{font}', estilo='{style}'")
        return blocks


    def next_page(self) -> int:
        if self.doc and self.current_page < len(self.doc) - 1:
            self.current_page += 1
        return self.current_page

    def prev_page(self) -> int:
        if self.doc and self.current_page > 0:
            self.current_page -= 1
        return self.current_page
