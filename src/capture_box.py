import sys
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QApplication,
    QHBoxLayout,
    QTextEdit,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QKeyEvent, QFocusEvent


class CaptureBox(QWidget):
    """
    A borderless, semi-transparent window for live transcription display.
    """

    confirmed = Signal(str)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self._closing = False
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # Prevents it from appearing in the taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(
            """
            background-color: rgba(30, 30, 30, 0.85);
            color: white;
            border-radius: 10px;
            font-size: 14px;
        """
        )

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Transcription text area (read-only, auto-wrap, limited height)
        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        self.text_area.setWordWrapMode(self.text_area.widgetResizable())  # wrap at widget width
        self.text_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.text_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_area.setStyleSheet(
            """
            QTextEdit {
                background: transparent;
                border: none;
                color: white;
            }
            QTextEdit:focus {
                outline: none;
            }
        """
        )
        # Size constraints: twice as wide, allow up to ~10 lines before scroll
        fm = self.text_area.fontMetrics()
        line_height = fm.lineSpacing()
        max_height = int(line_height * 10 + 20)
        min_height = int(line_height * 3 + 12)
        self.text_area.setMinimumHeight(min_height)
        self.text_area.setMaximumHeight(max_height)
        layout.addWidget(self.text_area)

        # Buttons
        button_row = QHBoxLayout()
        self.confirm_button = QPushButton("✓ Confirm")
        self.cancel_button = QPushButton("✕ Cancel")

        # Styling for buttons
        button_style = """
            QPushButton {
                background-color: #555;
                border: none;
                padding: 6px 10px;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #666;
            }
        """
        self.confirm_button.setStyleSheet(button_style)
        self.cancel_button.setStyleSheet(button_style)

        button_row.addStretch()
        button_row.addWidget(self.confirm_button)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        self.setMinimumWidth(640)

        # Connections
        self.confirm_button.clicked.connect(self.on_confirm)
        self.cancel_button.clicked.connect(self.on_cancel)

    def on_confirm(self):
        self._closing = True
        self.confirmed.emit(self.text_area.toPlainText())
        self.hide()

    def on_cancel(self):
        self._closing = True
        self.cancelled.emit()
        self.hide()

    def set_text(self, text):
        self.text_area.setPlainText(text)
        self.text_area.moveCursor(self.text_area.textCursor().End)

    def show_at_cursor(self):
        self._closing = False
        self.adjustSize()
        cursor_pos = QCursor.pos()
        width = self.width()
        height = self.height()

        x = cursor_pos.x() - width // 2
        y = cursor_pos.y() - height - 12  # position above cursor

        # Ensure the box stays on-screen
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left() + 10, min(x, screen.right() - width - 10))
        y = max(screen.top() + 10, min(y, screen.bottom() - height - 10))

        self.move(x, y)
        self.show()
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.on_confirm()
        elif event.key() == Qt.Key_Escape:
            self.on_cancel()
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent):
        # No-op to avoid accidental cancel when focus shifts during confirm
        super().focusOutEvent(event)


if __name__ == "__main__":
    # Example usage for testing
    app = QApplication(sys.argv)

    capture_box = CaptureBox()

    def on_confirmed(text):
        print(f"Confirmed with text: {text}")
        app.quit()

    def on_cancelled():
        print("Cancelled.")
        app.quit()

    capture_box.confirmed.connect(on_confirmed)
    capture_box.cancelled.connect(on_cancelled)

    capture_box.show_at_cursor()

    # Simulate receiving text
    from PySide6.QtCore import QTimer

    QTimer.singleShot(1000, lambda: capture_box.set_text("This is a test transcription."))
    QTimer.singleShot(2000, lambda: capture_box.set_text("This is a test transcription that is getting longer."))

    sys.exit(app.exec())
