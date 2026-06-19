# viewer.py
import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QGraphicsView, QGraphicsScene
)
from PySide6.QtGui import QPen, QColor
from PySide6.QtCore import Qt
from pdf_utils import PDFDocument
from PySide6.QtWidgets import QSpinBox, QLabel
from PySide6.QtWidgets import QInputDialog, QFileDialog
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from pdf_anonymizer import anonymize_pdf
from PySide6.QtCore import Qt, QThread, QObject, Signal
from PySide6.QtWidgets import QProgressDialog
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QTextEdit, QFormLayout, QGroupBox, QCheckBox
from pdf_anonymizer import CLIENT_STORE, _normalize_client_text, compile_client_patterns
import re


class AnonymizeWorker(QObject):
    finished = Signal()
    error = Signal(str)
    progress = Signal(int)  # opcjonalnie, jeżeli w przyszłości chcesz mieć procenty

    def __init__(self, pdf, parent_window, selected_pages, output_path):
        super().__init__()
        self.pdf = pdf
        self.parent_window = parent_window
        self.selected_pages = selected_pages
        self.output_path = output_path

    def run(self):
        try:
            anonymize_pdf(
                self.pdf,
                self.parent_window,
                progress_callback=self.progress.emit,
                is_cancelled=lambda: QThread.currentThread().isInterruptionRequested(),
                selected_pages=self.selected_pages,
                output_path=self.output_path
            )
            self.finished.emit()
        except TypeError:
            anonymize_pdf(
                self.pdf,
                self.parent_window,
                selected_pages=self.selected_pages,
                output_path=self.output_path
            )
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))

class PDFViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Viewer (PySide6)")
        self.resize(1000, 700)

        # Documento PDF (lógica en otro archivo)
        self.pdf = PDFDocument()

        # Layout principal
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)

        # Panel izquierdo
        self.left_panel_widget = QWidget()
        self.left_panel_widget.setStyleSheet("background-color: #404040;")
        self.left_panel = QVBoxLayout(self.left_panel_widget)

        self.open_button = QPushButton("Open PDF")
        self.open_button.setStyleSheet("""
            QPushButton {
                background-color: #606060;
                color: white;
                font-weight: bold;
                border-radius: 5px;
                padding: 6px;
            }
            QPushButton:hover {
                background-color: #808080;
            }
            QToolTip {
                color: white;
            }
        """)
        self.open_button.clicked.connect(self.open_pdf)
        self.left_panel.addWidget(self.open_button)
        self.open_button.setToolTip("Select a PDF file to open in the viewer.")

        self.manage_clients_btn = QPushButton("Manage Clients")
        self.manage_clients_btn.setStyleSheet("""
             QPushButton {
                background-color: #606060;
                color: white;
                font-weight: bold;
                border-radius: 5px;
                padding: 6px;
            }
            QPushButton:hover {
                background-color: #808080;
            }
            QToolTip {
                color: white;
            }
        """)
        self.manage_clients_btn.clicked.connect(self.open_clients_dialog)
        self.left_panel.addWidget(self.manage_clients_btn)
        self.manage_clients_btn.setToolTip("Open the client dictionary for editing.")

        # --- Control de interlineado ---
        self.line_spacing_label = QLabel("Line spacing (px):")
        self.line_spacing_label.setStyleSheet("color: white; font-weight: bold;")

        self.line_spacing_spin = QSpinBox()
        self.line_spacing_spin.setRange(0, 50)
        self.line_spacing_spin.setValue(5)  # default
        self.line_spacing_spin.valueChanged.connect(self.update_line_spacing)
        self.line_spacing_spin.setToolTip("Set additional spacing between text lines in pixels.")

        # Estilizar el QLineEdit interno directamente
        editor = self.line_spacing_spin.lineEdit()
        editor.setStyleSheet("""
            background-color: #606060;
            color: white;
            font-weight: bold;
            border: none;
        """)

        self.left_panel.addWidget(self.line_spacing_label)
        self.left_panel.addWidget(self.line_spacing_spin)
        
        # --- Control de header ---
        self.header_label = QLabel("Header (px from top):")
        self.header_label.setStyleSheet("color: white; font-weight: bold;")
        self.header_spin = QSpinBox()
        self.header_spin.setRange(0, 1000)
        self.header_spin.setValue(0)
        self.header_spin.valueChanged.connect(self.update_header)

        editor_h = self.header_spin.lineEdit()
        editor_h.setStyleSheet("""
            background-color: #606060;
            color: white;
            font-weight: bold;
            border: none;
        """)
        self.header_spin.setToolTip("Set the header zone height measured from the top edge (pixels).")

        self.left_panel.addWidget(self.header_label)
        self.left_panel.addWidget(self.header_spin)

        # --- Control de footer ---
        self.footer_label = QLabel("Footer (px from bottom):")
        self.footer_label.setStyleSheet("color: white; font-weight: bold;")
        self.footer_spin = QSpinBox()
        self.footer_spin.setRange(0, 1000)
        self.footer_spin.setValue(0)
        self.footer_spin.valueChanged.connect(self.update_footer)

        editor_f = self.footer_spin.lineEdit()
        editor_f.setStyleSheet("""
            background-color: #606060;
            color: white;
            font-weight: bold;
            border: none;
        """)
        self.footer_spin.setToolTip("Set the footer zone height measured from the bottom edge (pixels).")

        self.left_panel.addWidget(self.footer_label)
        self.left_panel.addWidget(self.footer_spin)

        # Botón Anonimizar
        self.anon_button = QPushButton("Anonymize")
        self.anon_button.clicked.connect(self.start_anonymize)
        self.anon_button.setStyleSheet("""
            QPushButton {
                background-color: #AA336A;
                color: white;
                font-weight: bold;
                border-radius: 5px;
                padding: 6px;
            }
            QPushButton:hover {
                background-color: #CC4477;
            }
        """)
        self.left_panel.addWidget(self.anon_button)
        self.anon_button.setToolTip("Start anonymizing the selected pages and save the result.")



        self.left_panel.addStretch()

        # Panel derecho
        right_panel = QVBoxLayout()

        # Visor PDF
        self.view = QGraphicsView()
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        self.view.setAlignment(Qt.AlignCenter)
        right_panel.addWidget(self.view, stretch=9)

        # Botones navegación
        nav_layout = QHBoxLayout()
        nav_layout.setAlignment(Qt.AlignCenter)

        self.prev_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")

        style = """
            QPushButton {
                background-color: #606060;
                color: white;
                font-weight: bold;
                border-radius: 5px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: #808080;
            }
        """
        self.prev_button.setStyleSheet(style)
        self.next_button.setStyleSheet(style)

        self.prev_button.clicked.connect(self.prev_page)
        self.next_button.clicked.connect(self.next_page)
        self.prev_button.setToolTip("Go to the previous page of the document.")
        self.next_button.setToolTip("Go to the next page of the document.")

        nav_layout.addWidget(self.prev_button)
        nav_layout.addWidget(self.next_button)

        right_panel.addStretch()
        right_panel.addLayout(nav_layout)

        # Añadir paneles
        main_layout.addWidget(self.left_panel_widget, 1)
        main_layout.addLayout(right_panel, 4)

#--progres
    def _parse_pages_input(self, pages_str):
        selected = set()
        for part in pages_str.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                start_str, end_str = part.split('-', 1)
                start = int(start_str)
                end = int(end_str)
                if start <= 0 or end <= 0 or end < start:
                    raise ValueError
                selected.update(range(start - 1, end))
            else:
                value = int(part)
                if value <= 0:
                    raise ValueError
                selected.add(value - 1)
        return selected

    def start_anonymize(self):
        if not self.pdf.has_document():
            QMessageBox.warning(self, "No file", "Please open a PDF file first.")
            return

        pages_str, ok = QInputDialog.getText(
            self,
            "Select pages",
            "Enter pages to export (e.g. 1-3,5):"
        )
        if not ok or not pages_str.strip():
            return

        try:
            selected_pages = self._parse_pages_input(pages_str)
        except ValueError:
            QMessageBox.warning(self, "Invalid range", "Please enter a valid page range (e.g. 1-3,5).")
            return

        if not selected_pages:
            QMessageBox.warning(self, "No pages", "No pages were selected for processing.")
            return

        page_indexes = sorted(selected_pages)

        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save anonymized PDF",
            "",
            "PDF files (*.pdf)"
        )
        if not output_path:
            return

        self.progress_dialog = QProgressDialog("Anonymization in progress...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setAutoReset(True)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setValue(0)
        self.progress_dialog.show()

        self.thread = QThread(self)
        self.worker = AnonymizeWorker(self.pdf, self, page_indexes, output_path)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress_dialog.setValue)
        self.worker.finished.connect(self.on_anonymize_finished)
        self.worker.error.connect(self.on_anonymize_error)

        self.progress_dialog.canceled.connect(self.thread.requestInterruption)

        self.thread.start()

    def on_anonymize_finished(self):
        if hasattr(self, "progress_dialog"):
            self.progress_dialog.close()
        if hasattr(self, "thread"):
            self.thread.quit()
            self.thread.wait()
        QMessageBox.information(self, "Done", "Anonymization completed.")
        # Opcjonalnie: przeładuj bieżącą stronę
        if self.pdf.has_document():
            self.load_page(self.pdf.current_page)

    def on_anonymize_error(self, msg: str):
        if hasattr(self, "progress_dialog"):
            self.progress_dialog.close()
        if hasattr(self, "thread"):
            self.thread.quit()
            self.thread.wait()
        QMessageBox.critical(self, "Error", f"An error occurred during anonymization:\n{msg}")
    
    # ---------------- GUI Funciones ----------------

    def open_clients_dialog(self):
        dlg = ClientsDialog(self.pdf, self)
        dlg.exec()

    def open_pdf(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select PDF", "", "PDF files (*.pdf)"
        )
        if file_path:
            self.pdf.open(file_path)
            self.load_page(self.pdf.current_page)

    def load_page(self, page_num: int):
        pixmap = self.pdf.render_page(page_num)
        if pixmap:
            self.scene.clear()
            self.scene.addPixmap(pixmap)

            # Dibujar cuadros sobre párrafos
            pen = QPen(QColor("red"))
            pen.setWidth(2)
            for rect in self.pdf.get_blocks(page_num):
                x, y, w, h = rect[0:4]   # solo las 4 coords
                self.scene.addRect(x, y, w, h, pen=pen)

            # 🔹 Dibujar líneas punteadas para header y footer
            page = self.pdf.doc[page_num]
            page_height = page.rect.height * self.pdf.zoom
            page_width = page.rect.width * self.pdf.zoom

            dash_pen = QPen(QColor("blue"))
            dash_pen.setStyle(Qt.DashLine)
            dash_pen.setWidth(3)

            # Línea punteada horizontal para header
            if self.pdf.header_height > 0:
                self.scene.addLine(0, self.pdf.header_height, page_width, self.pdf.header_height, dash_pen)

            # Línea punteada horizontal para footer
            if self.pdf.footer_height > 0:
                y_footer = page_height - self.pdf.footer_height
                self.scene.addLine(0, y_footer, page_width, y_footer, dash_pen)

            self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.load_page(self.pdf.current_page)

    def next_page(self):
        if self.pdf.has_document():
            page = self.pdf.next_page()
            self.load_page(page)

    def prev_page(self):
        if self.pdf.has_document():
            page = self.pdf.prev_page()
            self.load_page(page)

    def update_line_spacing(self, value):
        self.pdf.line_spacing = value
        if self.pdf.has_document():
            self.load_page(self.pdf.current_page)

    def update_header(self, value):
        self.pdf.header_height = value
        if self.pdf.has_document():
            self.load_page(self.pdf.current_page)

    def update_footer(self, value):
        self.pdf.footer_height = value
        if self.pdf.has_document():
            self.load_page(self.pdf.current_page)

class ClientsDialog(QDialog):
    def __init__(self, pdf_doc, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clients dictionary")
        self.resize(650, 500)
        self.pdf = pdf_doc

        layout = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by name/alias...")
        self.search_edit.textChanged.connect(self.refresh_list)
        self.search_edit.setToolTip("Filter clients by canonical name or alias.")
        search_row.addWidget(self.search_edit)
        layout.addLayout(search_row)

        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self.on_item_changed)
        self.list_widget.setToolTip("View clients loaded from clients.json.")
        layout.addWidget(self.list_widget, 2)

        form_box = QGroupBox("Edit")
        form = QFormLayout(form_box)

        self.ed_canonical = QLineEdit()
        self.ed_aliases = QTextEdit()
        self.ed_patterns = QTextEdit()
        self.cb_case = QCheckBox()
        self.cb_case.setChecked(True)
        self.ed_canonical.setToolTip("Enter the canonical client name stored in the dictionary.")
        self.ed_aliases.setToolTip("Provide alternative client names, one per line.")
        self.ed_patterns.setToolTip("Provide custom regular expressions, one per line.")
        self.cb_case.setToolTip("When checked, searches ignore letter case.")

        form.addRow(self._make_label_with_help("Canonical:", "Primary client name used for anonymization output."), self.ed_canonical)
        form.addRow(self._make_label_with_help("Aliases:", "Alternative names that will also trigger a match (one per line)."), self.ed_aliases)
        form.addRow(self._make_label_with_help("Patterns:", "Optional regular expressions to match complex variations (one per line)."), self.ed_patterns)
        form.addRow(self._make_label_with_help("Case-insensitive:", "Enable to match client names without considering letter case."), self.cb_case)
        layout.addWidget(form_box, 2)

        btn_row = QHBoxLayout()
        self.btn_new = QPushButton("New")
        self.btn_save = QPushButton("Save")
        self.btn_test = QPushButton("Test on current PDF")

        for b in (self.btn_new, self.btn_save, self.btn_test):
            b.setStyleSheet("""
                QPushButton {
                    background-color: #AA336A;
                    color: white;
                    font-weight: bold;
                    border-radius: 5px;
                    padding: 6px;
                }
                QPushButton:hover { background-color: #CC4477; }
            """)

        self.btn_new.clicked.connect(self.on_new)
        self.btn_save.clicked.connect(self.on_save)
        self.btn_test.clicked.connect(self.on_test)
        self.btn_new.setToolTip("Clear the fields to add a new client entry.")
        self.btn_save.setToolTip("Save the changes to clients.json.")
        self.btn_test.setToolTip("Count matches for this client in the currently open PDF.")

        btn_row.addWidget(self.btn_new)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_test)
        layout.addLayout(btn_row)

        self.refresh_list()

    def _make_label_with_help(self, caption: str, tooltip: str) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        label = QLabel(caption)
        label.setToolTip(tooltip)

        help_icon = QLabel("?")
        help_icon.setObjectName("HelpIcon")
        help_icon.setAlignment(Qt.AlignCenter)
        help_icon.setFixedSize(16, 16)
        help_icon.setCursor(Qt.PointingHandCursor)
        help_icon.setToolTip(tooltip)
        help_icon.setStyleSheet("""
            QLabel#HelpIcon {
                color: white;
                background-color: #AA336A;
                border-radius: 8px;
                font-weight: bold;
            }
        """)

        layout.addWidget(label)
        layout.addWidget(help_icon)
        layout.addStretch()
        return wrapper

    def refresh_list(self):
        q = self.search_edit.text().strip().lower()
        self.list_widget.clear()
        for rec in CLIENT_STORE.clients:
            if rec.get("status","approved") != "approved":
                continue
            text = rec["canonical"]
            all_aliases = " | ".join(rec.get("aliases", []))
            row = f"{text} — {all_aliases}"
            if q and q not in row.lower():
                continue
            item = QListWidgetItem(row)
            item.setData(Qt.UserRole, rec)
            self.list_widget.addItem(item)

    def on_item_changed(self, cur, prev):
        if cur:
            rec = cur.data(Qt.UserRole)
            self.load_record(rec)

    def load_record(self, rec):
        self.ed_canonical.setText(rec.get("canonical",""))
        self.ed_aliases.setPlainText("\n".join(rec.get("aliases", [])))
        self.ed_patterns.setPlainText("\n".join(rec.get("patterns", [])))
        self.cb_case.setChecked(rec.get("case_insensitive", True))

    def collect_record(self):
        canonical = self.ed_canonical.text().strip()
        aliases = [a.strip() for a in self.ed_aliases.toPlainText().splitlines() if a.strip()]
        patterns = [p.strip() for p in self.ed_patterns.toPlainText().splitlines() if p.strip()]
        return {
            "id": re.sub(r"[^A-Za-z0-9_-]", "_", canonical)[:32] or "client",
            "canonical": canonical,
            "aliases": aliases,
            "patterns": patterns,
            "case_insensitive": self.cb_case.isChecked(),
            "status": "approved"
        }

    def on_new(self):
        self.ed_canonical.clear()
        self.ed_aliases.clear()
        self.ed_patterns.clear()
        self.cb_case.setChecked(True)

    def on_save(self):
        rec = self.collect_record()
        if not rec["canonical"]:
            QMessageBox.warning(self, "Missing data", "Canonical name cannot be empty.")
            return
        CLIENT_STORE.upsert(rec)
        self.refresh_list()
        QMessageBox.information(self, "Saved", "Clients dictionary updated.")

    def on_test(self):
        if not self.pdf or not self.pdf.has_document():
            QMessageBox.information(self, "No PDF", "Open a PDF first.")
            return
        rec = self.collect_record()
        if not rec.get("canonical"):
            QMessageBox.warning(self, "Missing data", "Canonical name cannot be empty.")
            return

        patterns = compile_client_patterns(rec)
        if not patterns:
            QMessageBox.information(self, "No patterns", "Unable to build search patterns for this client.")
            return

        hits = 0
        for p in range(self.pdf.page_count()):
            for (_, _, _, _, text, *_) in self.pdf.get_blocks(p):
                tnorm = _normalize_client_text(text)
                spans = set()
                for patt in patterns:
                    for match in patt.finditer(tnorm):
                        spans.add((match.start(), match.end()))
                hits += len(spans)
        QMessageBox.information(self, "Test result", f"Matches found: {hits}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = PDFViewer()
    viewer.show()
    sys.exit(app.exec())
