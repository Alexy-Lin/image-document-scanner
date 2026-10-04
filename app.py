"""Native PySide6 interface for the local document scanner."""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from scanner import ScanOptions, detect_document_corners, make_pdf, process_image, read_image


class ButtonOptions(QWidget):
    """A compact set of mutually exclusive, directly clickable choices."""

    selectionChanged = Signal(object)

    def __init__(
        self,
        choices: list[tuple[str, object]],
        selected: object,
        columns: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._values: dict[int, object] = {}
        self._labels: dict[int, str] = {}
        self._buttons: list[QPushButton] = []
        self._preferred_columns = columns
        self._active_columns = 0
        self._current_value = selected
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(5)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        for index, (label, value) in enumerate(choices):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setMinimumHeight(32)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._group.addButton(button, index)
            self._values[index] = value
            self._labels[index] = label
            self._buttons.append(button)
            button.clicked.connect(
                lambda _checked=False, choice=value: self._choice_clicked(choice)
            )
            if value == selected:
                button.setChecked(True)
        self._reflow_buttons(columns)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt event override
        super().resizeEvent(event)
        if not self._buttons:
            return
        widest_label = max(button.sizeHint().width() for button in self._buttons)
        spacing = self._grid.horizontalSpacing()
        available_width = self.contentsRect().width()
        columns = max(1, (available_width + spacing) // (widest_label + spacing))
        self._reflow_buttons(min(self._preferred_columns, columns))

    def _reflow_buttons(self, columns: int) -> None:
        if columns == self._active_columns:
            return
        for button in self._buttons:
            self._grid.removeWidget(button)
        max_columns = max(self._preferred_columns, self._active_columns, columns)
        for column in range(max_columns):
            self._grid.setColumnStretch(column, int(column < columns))
        for index, button in enumerate(self._buttons):
            self._grid.addWidget(button, index // columns, index % columns)
        self._active_columns = columns
        rows = (len(self._buttons) + columns - 1) // columns
        button_height = max(
            max(button.sizeHint().height(), button.minimumHeight())
            for button in self._buttons
        )
        required_height = rows * button_height + (rows - 1) * self._grid.verticalSpacing()
        self.setMinimumHeight(required_height)
        self.updateGeometry()

    def currentData(self) -> object:  # noqa: N802 - mirrors Qt selection APIs
        return self._values.get(self._group.checkedId())

    def _choice_clicked(self, value: object) -> None:
        if value == self._current_value:
            return
        self._current_value = value
        self.selectionChanged.emit(value)

    def currentText(self) -> str:  # noqa: N802 - mirrors Qt selection APIs
        return self._labels.get(self._group.checkedId(), "")

    def setCurrentData(self, value: object) -> None:  # noqa: N802 - mirrors Qt selection APIs
        for index, choice in self._values.items():
            if choice == value:
                button = self._group.button(index)
                if button is not None:
                    button.setChecked(True)
                    self._current_value = value
                return


@dataclass
class DocumentPage:
    path: str
    original: np.ndarray
    points: list[tuple[float, float]] | None = None
    processed: np.ndarray | None = None
    rotation_degrees: int = 0

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
        self.settings = QSettings("Alexy-Lin", "ImageDocumentScanner")
        last_directory = self.settings.value("last_image_directory", str(Path.home()), type=str)
        self._last_image_directory = last_directory if Path(last_directory).is_dir() else str(Path.home())
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
            QPushButton:checked { color: white; border-color: #1268d3; background: #1268d3; font-weight: 600; }
            QPushButton:checked:hover { background: #0d57b5; }
            QSlider::groove:horizontal { height: 4px; background: #dce1e8; }
            QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #1268d3; }
            """
        )

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(12)

        header = QHBoxLayout()
        self.add_button = QPushButton("添加图片")
        self.remove_button = QPushButton("移除")
        self.clear_button = QPushButton("清空任务")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.auto_button = QPushButton("自动检测页面边缘")
        self.reset_points_button = QPushButton("清除角点")
        self.points_label = QLabel("尚未选择角点")
        self.points_label.setStyleSheet("color: #687385;")
        self.add_button.clicked.connect(self._add_images)
        self.remove_button.clicked.connect(self._remove_page)
        self.clear_button.clicked.connect(self._clear_task)
        self.up_button.clicked.connect(lambda: self._move_page(-1))
        self.down_button.clicked.connect(lambda: self._move_page(1))
        self.auto_button.clicked.connect(self._auto_detect)
        self.reset_points_button.clicked.connect(self._reset_points)
        for button in (
            self.add_button,
            self.remove_button,
            self.clear_button,
            self.up_button,
            self.down_button,
            self.auto_button,
            self.reset_points_button,
        ):
            header.addWidget(button)
        header.addWidget(self.points_label)
        header.addStretch(1)
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

        self.settings_group = QGroupBox("扫描设置")
        settings_form = QFormLayout(self.settings_group)
        self.mode_buttons = ButtonOptions(
            [(name, name) for name in ["纯白文档", "保真彩色", "灰度", "黑白"]],
            selected="纯白文档",
            columns=2,
        )
        settings_form.addRow("输出模式", self.mode_buttons)
        self.shadow_buttons = ButtonOptions(
            [(name, name) for name in ["轻度", "标准", "强力"]],
            selected="标准",
            columns=3,
        )
        self.shadow_buttons.setToolTip("强力模式会更积极地提白背景，浅灰色文字可能变淡")
        self.mode_buttons.selectionChanged.connect(self._enhancement_options_changed)
        self.shadow_buttons.selectionChanged.connect(self._enhancement_options_changed)
        settings_form.addRow("去阴影力度", self.shadow_buttons)
        self.dpi_buttons = ButtonOptions(
            [(value, int(value)) for value in ["150", "200", "300"]],
            selected=200,
            columns=3,
        )
        settings_form.addRow("PDF 分辨率", self.dpi_buttons)
        self.pdf_size_buttons = ButtonOptions(
            [(name, name) for name in ["原始比例", "A4（按页方向）"]],
            selected="原始比例",
            columns=2,
        )
        self.pdf_size_buttons.setToolTip(
            "原始比例：页面尺寸随图片变化；A4：每页按旋转后的方向使用纵向或横向 A4，等比缩放居中并补白"
        )
        settings_form.addRow("PDF 页面尺寸", self.pdf_size_buttons)

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
        self.background_slider.valueChanged.connect(
            lambda value: self.background_value.setText(str(value))
        )
        self.background_slider.valueChanged.connect(self._enhancement_options_changed)

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
        self.ink_slider.setToolTip(
            "数值越高，越容易保留浅灰色文字和图形；过高时，浅阴影也可能被当作内容保留"
        )
        self.ink_slider.valueChanged.connect(lambda value: self.ink_value.setText(str(value)))
        self.ink_slider.valueChanged.connect(self._enhancement_options_changed)
        self._update_enhancement_controls(self.mode_buttons.currentText())
        controls.addWidget(self.settings_group)

        preview_group = QGroupBox("预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_buttons = ButtonOptions(
            [("原图（选择角点）", "original"), ("处理结果", "result")],
            selected="original",
            columns=2,
        )
        self.preview_buttons.selectionChanged.connect(self._refresh_current_page)
        preview_layout.addWidget(self.preview_buttons)
        preview_layout.addWidget(QLabel("当前页旋转（每次 90°）"))
        rotation_buttons = QVBoxLayout()
        self.rotate_ccw_button = QPushButton("↶ 逆时针 90°")
        self.rotate_cw_button = QPushButton("顺时针 90° ↷")
        self.rotate_ccw_button.setToolTip("将当前页逆时针旋转 90°")
        self.rotate_cw_button.setToolTip("将当前页顺时针旋转 90°")
        self.rotate_ccw_button.clicked.connect(lambda: self._rotate_current_page(-90))
        self.rotate_cw_button.clicked.connect(lambda: self._rotate_current_page(90))
        rotation_buttons.addWidget(self.rotate_ccw_button)
        rotation_buttons.addWidget(self.rotate_cw_button)
        preview_layout.addLayout(rotation_buttons)
        controls.addWidget(preview_group)

        self.process_current_button = QPushButton("处理当前页")
        self.process_current_button.clicked.connect(self._process_current)
        controls.addWidget(self.process_current_button)
        self.export_button = QPushButton("保存多页 PDF")
        self.export_button.setToolTip("每个已导入的图片文件对应 PDF 中的一页；未处理页面会先自动处理")
        self.export_button.clicked.connect(self._export_pdf)
        controls.addWidget(self.export_button)
        controls.addStretch(1)
        controls_panel = QWidget()
        controls_panel.setLayout(controls)
        controls_panel.setMinimumWidth(0)
        controls_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setMinimumWidth(300)
        controls_scroll.setWidget(controls_panel)
        body.addWidget(controls_scroll)
        controls.setStretch(0, 0)

        layout.addLayout(body, 1)
        self.setCentralWidget(root)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.hide()
        self.statusBar().showMessage("就绪 · 图片只在本机处理")
        self.statusBar().addPermanentWidget(self.progress)

    def _update_enhancement_controls(self, mode: str) -> None:
        is_color = mode == "保真彩色"
        self.shadow_buttons.setEnabled(not is_color)
        self.background_slider.setEnabled(not is_color)
        self.ink_slider.setEnabled(mode == "纯白文档")

    def _enhancement_options_changed(self, *_args) -> None:
        """Discard results made with previous image-enhancement settings."""
        self._update_enhancement_controls(self.mode_buttons.currentText())
        invalidated = False
        for index, page in enumerate(self.pages):
            if page.processed is not None:
                page.processed = None
                self._update_page_label(index)
                invalidated = True
        if invalidated:
            self.preview_buttons.setCurrentData("original")
            self._refresh_current_page()
            self.statusBar().showMessage(
                "扫描设置已更改，旧处理结果已清除；导出时会按新设置重新处理。",
                6000,
            )
        self._update_actions()

    def _add_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择文档照片",
            self._last_image_directory,
            "图片文件 (*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp)",
        )
        if not paths:
            return
        self._last_image_directory = str(Path(paths[0]).parent)
        self.settings.setValue("last_image_directory", self._last_image_directory)
        self.settings.sync()
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

    def _clear_task(self) -> None:
        if not self.pages:
            return
        answer = QMessageBox.question(
            self,
            "清空当前任务",
            "清空当前导入的图片、角点和处理结果，已保存的 PDF 不受影响。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._pending_export_path = None
        self.page_list.blockSignals(True)
        self.page_list.clear()
        self.pages.clear()
        self.page_list.blockSignals(False)
        self.preview_buttons.setCurrentData("original")
        self.canvas.set_content(None)
        self.points_label.setText("尚未选择角点")
        self.statusBar().showMessage("当前任务已清空，可以添加下一批图片", 5000)
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
        show_result = self.preview_buttons.currentData() == "result"
        if show_result and page.processed is not None:
            rotated = self._rotated_image(page.processed, page.rotation_degrees)
            self.canvas.set_content(rotated, editable=False)
        else:
            self.canvas.set_content(page.original, page.points, editable=True)
        count = len(page.points or [])
        self.points_label.setText(f"已选 {count}/4 个角点" if count else "尚未选择角点")

    @staticmethod
    def _rotated_image(image: np.ndarray, degrees: int) -> np.ndarray:
        turns = (degrees // 90) % 4
        if turns == 0:
            return image
        return np.ascontiguousarray(np.rot90(image, k=(-turns) % 4))

    def _rotate_current_page(self, degrees: int) -> None:
        row = self.page_list.currentRow()
        if row < 0 or row >= len(self.pages):
            return
        page = self.pages[row]
        page.rotation_degrees = (page.rotation_degrees + degrees) % 360
        self._update_page_label(row)
        if page.processed is not None:
            self.preview_buttons.setCurrentData("result")
        self._refresh_current_page()

    def _points_changed(self, points) -> None:
        row = self.page_list.currentRow()
        if row < 0:
            return
        page = self.pages[row]
        page.points = [(float(x), float(y)) for x, y in points]
        page.processed = None
        self._update_page_label(row)
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
        page.processed = None
        self._update_page_label(row)
        self._refresh_current_page()
        self.statusBar().showMessage("已检测到页面边缘，可拖动蓝色控制点微调", 5000)
        self._update_actions()

    def _reset_points(self) -> None:
        row = self.page_list.currentRow()
        if row < 0:
            return
        self.pages[row].points = []
        self.pages[row].processed = None
        self._update_page_label(row)
        self._refresh_current_page()
        self._update_actions()

    def _process_current(self) -> None:
        row = self.page_list.currentRow()
        if row >= 0:
            self._start_processing([row])

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
            mode=self.mode_buttons.currentText(),
            shadow_strength=self.shadow_buttons.currentText(),
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
            self._update_page_label(index)
        self._set_busy(False)
        self.preview_buttons.setCurrentData("result")
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
            self.clear_button,
            self.up_button,
            self.down_button,
            self.auto_button,
            self.reset_points_button,
            self.process_current_button,
            self.export_button,
            self.rotate_ccw_button,
            self.rotate_cw_button,
        ):
            button.setEnabled(not busy)
        self.settings_group.setEnabled(not busy)
        self.preview_buttons.setEnabled(not busy)
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
            self.clear_button.setEnabled(bool(self.pages))
            self.up_button.setEnabled(has_page and row > 0)
            self.down_button.setEnabled(has_page and row < len(self.pages) - 1)
            self.auto_button.setEnabled(has_page)
            self.reset_points_button.setEnabled(has_page and bool(self.pages[row].points))
            self.process_current_button.setEnabled(has_page)
            self.export_button.setEnabled(bool(self.pages))
            self.rotate_ccw_button.setEnabled(has_page)
            self.rotate_cw_button.setEnabled(has_page)

    def _update_page_label(self, index: int) -> None:
        page = self.pages[index]
        if page.processed is not None:
            label = f"{page.name}  ✓"
        else:
            label = page.name
        rotation_label = {
            90: "↻90°",
            180: "↻180°",
            270: "↺90°",
        }.get(page.rotation_degrees)
        if rotation_label:
            label += f"  {rotation_label}"
        self.page_list.item(index).setText(label)

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
        to_process = [
            index
            for index, page in enumerate(self.pages)
            if page.processed is None
        ]
        if to_process:
            self._pending_export_path = path
            if not self._start_processing(to_process):
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
                dpi=int(self.dpi_buttons.currentData()),
                page_size=self.pdf_size_buttons.currentText(),
                page_rotations=[page.rotation_degrees for page in self.pages],
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


def _show_unhandled_exception(exc_type, exc_value, exc_traceback) -> None:
    """Log uncaught GUI/startup errors and show a useful message under pythonw."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    log_root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    log_path = log_root / "ImageDocumentScanner" / "app.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}]\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=log_file)
    except OSError:
        log_path = None

    try:
        if QApplication.instance() is None:
            QApplication(sys.argv[:1])
        message = f"程序发生未处理错误：{exc_value}"
        if log_path is not None:
            message += f"\n\n详细错误日志：{log_path}"
        QMessageBox.critical(None, "图片文档扫描工具", message)
    except Exception:
        # Keep the traceback available to any console-based launch as a fallback.
        sys.__excepthook__(exc_type, exc_value, exc_traceback)


if __name__ == "__main__":
    sys.excepthook = _show_unhandled_exception
    raise SystemExit(main())
