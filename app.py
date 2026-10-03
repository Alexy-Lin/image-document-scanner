"""Native PySide6 interface for the local document scanner."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from scanner import ScanOptions, detect_document_corners, make_pdf, process_image, read_image


@dataclass
class DocumentPage:
    path: str
    original: np.ndarray
    points: list[tuple[float, float]] | None = None
    processed: np.ndarray | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name


class ImageCanvas(QWidget):
    """Aspect-fit image view with click-to-select and drag-to-adjust corners."""

    pointsChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 420)
        self.setMouseTracking(True)
        self._pixmap = QPixmap()
        self._image_width = 0
        self._image_height = 0
        self._points: list[tuple[float, float]] = []
        self._editable = True
        self._drag_index: int | None = None

    def set_content(
        self,
        image_rgb: np.ndarray | None,
        points: list[tuple[float, float]] | None = None,
        editable: bool = True,
    ) -> None:
        if image_rgb is None:
            self._pixmap = QPixmap()
            self._image_width = 0
            self._image_height = 0
        else:
            image = np.ascontiguousarray(image_rgb, dtype=np.uint8)
            height, width = image.shape[:2]
            qimage = QImage(
                image.data,
                width,
                height,
                image.strides[0],
                QImage.Format.Format_RGB888,
            ).copy()
            self._pixmap = QPixmap.fromImage(qimage)
            self._image_width = width
            self._image_height = height
        self._points = list(points or [])
        self._editable = editable
        self._drag_index = None
        self.update()

    def _image_rect(self) -> QRectF:
        if self._pixmap.isNull():
            return QRectF()
        available = QRectF(self.rect()).adjusted(18, 18, -18, -18)
        if available.width() <= 0 or available.height() <= 0:
            return QRectF()
        scale = min(
            available.width() / self._pixmap.width(),
            available.height() / self._pixmap.height(),
        )
        width = self._pixmap.width() * scale
        height = self._pixmap.height() * scale
        return QRectF(
            available.center().x() - width / 2,
            available.center().y() - height / 2,
            width,
            height,
        )

    def _to_screen(self, point: tuple[float, float], image_rect: QRectF) -> QPointF:
        x = image_rect.left() + point[0] / max(1, self._image_width - 1) * image_rect.width()
        y = image_rect.top() + point[1] / max(1, self._image_height - 1) * image_rect.height()
        return QPointF(x, y)

    def _to_image(self, position: QPointF, image_rect: QRectF) -> tuple[float, float]:
        x_ratio = (position.x() - image_rect.left()) / max(1.0, image_rect.width())
        y_ratio = (position.y() - image_rect.top()) / max(1.0, image_rect.height())
        x = min(max(x_ratio, 0.0), 1.0) * max(0, self._image_width - 1)
        y = min(max(y_ratio, 0.0), 1.0) * max(0, self._image_height - 1)
        return (x, y)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt event override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f0f2f5"))
        if self._pixmap.isNull():
            painter.setPen(QColor("#687385"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "添加图片后，在画面中依次点击页面四角\n左上 → 右上 → 右下 → 左下",
            )
            return

        image_rect = self._image_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(image_rect, self._pixmap, QRectF(self._pixmap.rect()))
        painter.setClipRect(image_rect)

        screen_points = [self._to_screen(point, image_rect) for point in self._points]
        for point in screen_points:
            painter.setPen(QPen(QColor(0, 0, 0, 190), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(image_rect.left(), point.y()), QPointF(image_rect.right(), point.y()))
            painter.drawLine(QPointF(point.x(), image_rect.top()), QPointF(point.x(), image_rect.bottom()))

        if len(screen_points) >= 2:
            path = QPainterPath(screen_points[0])
            for point in screen_points[1:]:
                path.lineTo(point)
            if len(screen_points) == 4:
                path.closeSubpath()
            painter.setPen(QPen(QColor("#1683ff"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        painter.setClipping(False)
        for index, point in enumerate(screen_points):
            painter.setPen(QPen(QColor("white"), 2))
            painter.setBrush(QColor("#0866d8"))
            painter.drawEllipse(point, 8, 8)
            painter.setPen(QColor("white"))
            painter.drawText(QPointF(point.x() + 10, point.y() - 9), str(index + 1))

        if not self._editable:
            painter.setPen(QColor("#536174"))
            painter.drawText(
                QRectF(0, 0, self.width(), 28),
                Qt.AlignmentFlag.AlignCenter,
                "处理结果预览",
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt event override
        if not self._editable or self._pixmap.isNull():
            return
        position = event.position()
        image_rect = self._image_rect()
        if event.button() == Qt.MouseButton.RightButton:
            if self._points:
                self._points.pop()
                self.pointsChanged.emit(list(self._points))
                self.update()
            return
        if event.button() != Qt.MouseButton.LeftButton or not image_rect.contains(position):
            return

        if len(self._points) == 4:
            nearest = [
                (self._to_screen(point, image_rect) - position).manhattanLength()
                for point in self._points
            ]
            index = int(np.argmin(nearest))
            if nearest[index] <= 18:
                self._drag_index = index
                return
            self._points = []
        self._points.append(self._to_image(position, image_rect))
        self.pointsChanged.emit(list(self._points))
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt event override
        if self._drag_index is None:
            return
        self._points[self._drag_index] = self._to_image(event.position(), self._image_rect())
        self.pointsChanged.emit(list(self._points))
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt event override
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_index = None


class ProcessingWorker(QObject):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, jobs, options: ScanOptions) -> None:
        super().__init__()
        self.jobs = jobs
        self.options = options

    def run(self) -> None:
        results: dict[int, np.ndarray] = {}
        try:
            total = len(self.jobs)
            for number, (page_index, image_rgb, points, name) in enumerate(self.jobs, start=1):
                results[page_index] = process_image(image_rgb, points, self.options)
                self.progress.emit(number, total, name)
            self.completed.emit(results)
        except Exception as exc:  # pass worker errors back to the window
            self.failed.emit(str(exc))


class ScannerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("图片文档扫描工具")
        self.resize(1440, 900)
        self.setMinimumSize(1080, 700)
        self.pages: list[DocumentPage] = []
        self._thread: QThread | None = None
        self._worker: ProcessingWorker | None = None
        self._pending_export_path: str | None = None
        self._build_ui()
        self._update_actions()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f7f8fa; }
            QGroupBox { font-weight: 600; border: 1px solid #dce1e8; border-radius: 8px; margin-top: 10px; padding: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QPushButton { min-height: 34px; padding: 0 10px; border: 1px solid #ccd3dd; border-radius: 6px; background: white; }
            QPushButton:hover { background: #edf4ff; border-color: #8bbcff; }
            QPushButton:disabled { color: #9299a4; background: #f0f1f3; }
            QPushButton#primary { color: white; border: 0; background: #1268d3; font-weight: 600; }
            QPushButton#primary:hover { background: #0d57b5; }
            QListWidget { background: white; border: 1px solid #dce1e8; border-radius: 6px; padding: 4px; }
            QComboBox { min-height: 30px; padding: 0 6px; }
            QSlider::groove:horizontal { height: 4px; background: #dce1e8; }
            QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #1268d3; }
            """
        )

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("图片文档扫描工具")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #1f2937;")
        subtitle = QLabel("本地透视校正 · 文档增强 · 多页 PDF")
        subtitle.setStyleSheet("color: #687385;")
        title_column = QVBoxLayout()
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addLayout(title_column)
        header.addStretch(1)
        self.add_button = QPushButton("添加图片")
        self.remove_button = QPushButton("移除")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.add_button.clicked.connect(self._add_images)
        self.remove_button.clicked.connect(self._remove_page)
        self.up_button.clicked.connect(lambda: self._move_page(-1))
        self.down_button.clicked.connect(lambda: self._move_page(1))
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            header.addWidget(button)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(12)

        page_panel = QVBoxLayout()
        page_panel.addWidget(QLabel("页面顺序"))
        self.page_list = QListWidget()
        self.page_list.setMinimumWidth(190)
        self.page_list.setMaximumWidth(240)
        self.page_list.currentRowChanged.connect(self._page_changed)
        page_panel.addWidget(self.page_list, 1)
        body.addLayout(page_panel)

        self.canvas = ImageCanvas()
        self.canvas.pointsChanged.connect(self._points_changed)
        body.addWidget(self.canvas, 1)

        controls = QVBoxLayout()
        controls.setSpacing(10)

        selection_group = QGroupBox("页面校正")
        selection_layout = QVBoxLayout(selection_group)
        selection_help = QLabel("依次点击左上、右上、右下、左下。右键撤销一步，选完后可拖动角点微调。")
        selection_help.setWordWrap(True)
        selection_help.setStyleSheet("color: #687385;")
        selection_layout.addWidget(selection_help)
        self.auto_button = QPushButton("自动检测页面边缘")
        self.reset_points_button = QPushButton("清除角点")
        self.points_label = QLabel("尚未选择角点")
        self.points_label.setStyleSheet("color: #687385;")
        self.auto_button.clicked.connect(self._auto_detect)
        self.reset_points_button.clicked.connect(self._reset_points)
        selection_layout.addWidget(self.auto_button)
        selection_layout.addWidget(self.reset_points_button)
        selection_layout.addWidget(self.points_label)
        controls.addWidget(selection_group)

        settings_group = QGroupBox("扫描设置")
        settings_form = QFormLayout(settings_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["纯白文档", "保真彩色", "灰度", "黑白"])
        settings_form.addRow("输出模式", self.mode_combo)
        self.shadow_combo = QComboBox()
        self.shadow_combo.addItems(["轻度", "标准", "强力"])
        self.shadow_combo.setCurrentText("标准")
        self.shadow_combo.setToolTip("强力模式会更积极地提白背景，浅灰色文字可能变淡")
        self.mode_combo.currentTextChanged.connect(
            lambda mode: self.shadow_combo.setEnabled(mode != "保真彩色")
        )
        settings_form.addRow("去阴影力度", self.shadow_combo)
        self.dpi_combo = QComboBox()
        self.dpi_combo.addItems(["150", "200", "300"])
        self.dpi_combo.setCurrentText("200")
        settings_form.addRow("PDF 分辨率", self.dpi_combo)

        self.background_slider = QSlider(Qt.Orientation.Horizontal)
        self.background_slider.setRange(21, 151)
        self.background_slider.setSingleStep(2)
        self.background_slider.setPageStep(10)
        self.background_slider.setValue(51)
        self.background_value = QLabel("51")
        background_row = QHBoxLayout()
        background_row.addWidget(self.background_slider, 1)
        background_row.addWidget(self.background_value)
        background_widget = QWidget()
        background_widget.setLayout(background_row)
        settings_form.addRow("背景估计", background_widget)
        self.background_slider.valueChanged.connect(lambda value: self.background_value.setText(str(value)))

        self.ink_slider = QSlider(Qt.Orientation.Horizontal)
        self.ink_slider.setRange(5, 60)
        self.ink_slider.setValue(18)
        self.ink_value = QLabel("18")
        ink_row = QHBoxLayout()
        ink_row.addWidget(self.ink_slider, 1)
        ink_row.addWidget(self.ink_value)
        ink_widget = QWidget()
        ink_widget.setLayout(ink_row)
        settings_form.addRow("墨迹保留", ink_widget)
        self.ink_slider.valueChanged.connect(lambda value: self.ink_value.setText(str(value)))
        controls.addWidget(settings_group)

        preview_group = QGroupBox("预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_combo = QComboBox()
        self.preview_combo.addItem("原图（选择角点）", "original")
        self.preview_combo.addItem("处理结果", "result")
        self.preview_combo.currentIndexChanged.connect(self._refresh_current_page)
        preview_layout.addWidget(self.preview_combo)
        controls.addWidget(preview_group)

        self.process_current_button = QPushButton("处理当前页")
        self.process_current_button.clicked.connect(self._process_current)
        controls.addWidget(self.process_current_button)
        self.process_all_button = QPushButton("处理全部页面")
        self.process_all_button.setObjectName("primary")
        self.process_all_button.clicked.connect(self._process_all)
        controls.addWidget(self.process_all_button)
        self.export_button = QPushButton("保存多页 PDF")
        self.export_button.setToolTip("每个已导入的图片文件对应 PDF 中的一页；未处理页面会先自动处理")
        self.export_button.clicked.connect(self._export_pdf)
        controls.addWidget(self.export_button)
        controls.addStretch(1)
        body.addLayout(controls)
        controls.setStretch(0, 0)

        layout.addLayout(body, 1)
        self.setCentralWidget(root)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.hide()
        self.statusBar().showMessage("就绪 · 图片只在本机处理")
        self.statusBar().addPermanentWidget(self.progress)

    def _add_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择文档照片",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp)",
        )
        if not paths:
            return
        first_new = len(self.pages)
        errors: list[str] = []
        for path in paths:
            try:
                image = read_image(path)
                page = DocumentPage(path=path, original=image)
                self.pages.append(page)
                self.page_list.addItem(page.name)
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        if self.page_list.currentRow() < 0 and self.pages:
            self.page_list.setCurrentRow(first_new)
        self._update_actions()
        if errors:
            QMessageBox.warning(self, "部分图片无法读取", "\n".join(errors))
        elif paths:
            self.statusBar().showMessage(f"已添加 {len(paths)} 张图片", 4000)

    def _remove_page(self) -> None:
        row = self.page_list.currentRow()
        if row < 0:
            return
        self.page_list.blockSignals(True)
        self.page_list.takeItem(row)
        self.pages.pop(row)
        self.page_list.blockSignals(False)
        if self.pages:
            self.page_list.setCurrentRow(min(row, len(self.pages) - 1))
        else:
            self.canvas.set_content(None)
            self.points_label.setText("尚未选择角点")
        self._update_actions()

    def _move_page(self, offset: int) -> None:
        row = self.page_list.currentRow()
        destination = row + offset
        if row < 0 or destination < 0 or destination >= len(self.pages):
            return
        self.pages[row], self.pages[destination] = self.pages[destination], self.pages[row]
        self.page_list.blockSignals(True)
        item = self.page_list.takeItem(row)
        self.page_list.insertItem(destination, item)
        self.page_list.setCurrentRow(destination)
        self.page_list.blockSignals(False)
        self._refresh_current_page()
        self._update_actions()

    def _page_changed(self, row: int) -> None:
        self._refresh_current_page()
        self._update_actions()

    def _refresh_current_page(self, *_args) -> None:
        row = self.page_list.currentRow()
        if row < 0 or row >= len(self.pages):
            self.canvas.set_content(None)
            self.points_label.setText("尚未选择角点")
            return
        page = self.pages[row]
        show_result = self.preview_combo.currentData() == "result" and page.processed is not None
        if show_result:
            self.canvas.set_content(page.processed, editable=False)
        else:
            self.canvas.set_content(page.original, page.points, editable=True)
        count = len(page.points or [])
        self.points_label.setText(f"已选 {count}/4 个角点" if count else "尚未选择角点")

    def _points_changed(self, points) -> None:
        row = self.page_list.currentRow()
        if row < 0:
            return
        self.pages[row].points = [(float(x), float(y)) for x, y in points]
        count = len(points)
        self.points_label.setText(f"已选 {count}/4 个角点" if count else "尚未选择角点")
        if count == 4:
            self.statusBar().showMessage("四角已选好；可拖动蓝色控制点微调", 5000)
        self._update_actions()

    def _auto_detect(self) -> None:
        row = self.page_list.currentRow()
        if row < 0:
            return
        page = self.pages[row]
        corners = detect_document_corners(page.original)
        if corners is None:
            QMessageBox.information(self, "未找到页面边缘", "没有检测到可靠的四边形，请手动点击页面四角。")
            return
        page.points = [(float(x), float(y)) for x, y in corners]
        self._refresh_current_page()
        self.statusBar().showMessage("已检测到页面边缘，可拖动蓝色控制点微调", 5000)
        self._update_actions()

    def _reset_points(self) -> None:
        row = self.page_list.currentRow()
        if row < 0:
            return
        self.pages[row].points = []
        self._refresh_current_page()
        self._update_actions()

    def _process_current(self) -> None:
        row = self.page_list.currentRow()
        if row >= 0:
            self._start_processing([row])

    def _process_all(self) -> None:
        self._start_processing(list(range(len(self.pages))))

    def _start_processing(self, indices: list[int]) -> bool:
        if not indices:
            return False
        jobs = []
        missing: list[str] = []
        for index in indices:
            page = self.pages[index]
            if page.points is None or len(page.points) != 4:
                corners = detect_document_corners(page.original)
                if corners is not None:
                    page.points = [(float(x), float(y)) for x, y in corners]
                else:
                    missing.append(page.name)
            if page.points is not None and len(page.points) == 4:
                jobs.append((index, page.original, list(page.points), page.name))
        if missing:
            QMessageBox.warning(
                self,
                "需要选择页面四角",
                "以下页面没有检测到边缘，请先手动选择四角：\n" + "\n".join(missing),
            )
            self._refresh_current_page()
            return False

        options = ScanOptions(
            mode=self.mode_combo.currentText(),
            shadow_strength=self.shadow_combo.currentText(),
            background_kernel=self.background_slider.value(),
            ink_threshold=self.ink_slider.value(),
        )
        self._set_busy(True, len(jobs))
        self.statusBar().showMessage("正在处理图片…")
        self._thread = QThread(self)
        self._worker = ProcessingWorker(jobs, options)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._processing_progress)
        self._worker.completed.connect(self._processing_done)
        self._worker.failed.connect(self._processing_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        return True

    def _processing_progress(self, current: int, total: int, name: str) -> None:
        self.progress.setValue(current)
        self.statusBar().showMessage(f"正在处理 {name}（{current}/{total}）")

    def _processing_done(self, results: dict[int, np.ndarray]) -> None:
        for index, result in results.items():
            self.pages[index].processed = result
            self.page_list.item(index).setText(f"{self.pages[index].name}  ✓")
        self._set_busy(False)
        self.preview_combo.setCurrentIndex(1)
        self._refresh_current_page()
        self.statusBar().showMessage(f"已完成 {len(results)} 页处理", 6000)
        self._update_actions()
        if self._pending_export_path is not None:
            path = self._pending_export_path
            self._pending_export_path = None
            self._write_pdf(path)

    def _processing_failed(self, message: str) -> None:
        self._pending_export_path = None
        self._set_busy(False)
        self.statusBar().showMessage("处理失败", 5000)
        QMessageBox.critical(self, "处理失败", message)

    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._update_actions()

    def _set_busy(self, busy: bool, total: int = 0) -> None:
        for button in (
            self.add_button,
            self.remove_button,
            self.up_button,
            self.down_button,
            self.auto_button,
            self.reset_points_button,
            self.process_current_button,
            self.process_all_button,
            self.export_button,
        ):
            button.setEnabled(not busy)
        self.page_list.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(0)
            self.progress.show()
        else:
            self.progress.hide()
            self._update_actions()

    def _update_actions(self) -> None:
        busy = self._thread is not None and self._thread.isRunning()
        row = self.page_list.currentRow()
        has_page = 0 <= row < len(self.pages)
        if not busy:
            self.remove_button.setEnabled(has_page)
            self.up_button.setEnabled(has_page and row > 0)
            self.down_button.setEnabled(has_page and row < len(self.pages) - 1)
            self.auto_button.setEnabled(has_page)
            self.reset_points_button.setEnabled(has_page and bool(self.pages[row].points))
            self.process_current_button.setEnabled(has_page)
            self.process_all_button.setEnabled(bool(self.pages))
            self.export_button.setEnabled(bool(self.pages))

    def _export_pdf(self) -> None:
        if not self.pages:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存多页 PDF",
            str(Path.home() / "scanned_document.pdf"),
            "PDF 文件 (*.pdf)",
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if any(page.processed is None for page in self.pages):
            self._pending_export_path = path
            if not self._start_processing(list(range(len(self.pages)))):
                self._pending_export_path = None
            return
        self._write_pdf(path)

    def _write_pdf(self, path: str) -> None:
        try:
            content = make_pdf(
                (
                    page.processed if page.processed is not None else page.original
                    for page in self.pages
                ),
                dpi=int(self.dpi_combo.currentText()),
            )
            Path(path).write_bytes(content)
        except Exception as exc:
            QMessageBox.critical(self, "PDF 保存失败", str(exc))
            return
        self.statusBar().showMessage(f"已保存 {len(self.pages)} 页（一张源图片对应一页）：{path}", 8000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt event override
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("图片文档扫描工具")
    window = ScannerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
