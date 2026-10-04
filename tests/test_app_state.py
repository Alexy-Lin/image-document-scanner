import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from app import DocumentPage, ScannerWindow, _show_unhandled_exception


class AppStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_changing_scan_mode_invalidates_cached_page_results(self):
        window = ScannerWindow()
        page = DocumentPage(
            path="sample.png",
            original=np.zeros((20, 20, 3), dtype=np.uint8),
            points=[(0, 0), (19, 0), (19, 19), (0, 19)],
            processed=np.zeros((10, 10, 3), dtype=np.uint8),
        )
        window.pages.append(page)
        window.page_list.addItem(page.name)
        window.page_list.setCurrentRow(0)
        window.preview_buttons.setCurrentData("result")
        window._refresh_current_page()

        current_mode_button = next(
            button
            for button in window.mode_buttons.findChildren(QPushButton)
            if button.text() == "纯白文档"
        )
        current_mode_button.click()
        self.assertIsNotNone(page.processed)

        grayscale_button = next(
            button
            for button in window.mode_buttons.findChildren(QPushButton)
            if button.text() == "灰度"
        )
        grayscale_button.click()

        self.assertIsNone(page.processed)
        self.assertEqual(window.preview_buttons.currentData(), "original")
        window.close()

    def test_unhandled_exception_writes_a_readable_log(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": temp_directory}):
                with patch("app.QMessageBox.critical"):
                    error = RuntimeError("sample startup failure")
                    _show_unhandled_exception(RuntimeError, error, None)

            log_path = Path(temp_directory) / "ImageDocumentScanner" / "app.log"
            self.assertIn("sample startup failure", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
