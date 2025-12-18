import sys
from PySide6.QtWidgets import (
    QWidget,
    QPushButton,
    QVBoxLayout,
    QApplication,
    QHBoxLayout,
    QTextEdit,
    QGraphicsOpacityEffect,
)
from PySide6.QtCore import (
    Qt,
    Signal,
    QEvent,
    QPropertyAnimation,
    QAbstractAnimation,
)
from PySide6.QtGui import (
    QCursor,
    QKeyEvent,
    QFocusEvent,
    QTextOption,
    QGuiApplication,
    QTextCursor,
)


class CaptureBox(QWidget):
    """
    A borderless, semi-transparent window for live transcription display.
    """

    confirmed = Signal(str)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self._closing = False
        self._fade_duration_ms = 140

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # Prevents it from appearing in the taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(
            """
            background-color: rgba(0, 0, 0, 0.7);
            color: white;
            border-radius: 10px;
            font-size: 14px;
        """
        )

        # Subtle fade effect
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Transcription text area (read-only, auto-wrap, limited height)
        self.text_area = QTextEdit()
        self.text_area.setPlaceholderText("Listening...")
        self.text_area.setReadOnly(True)
        self.text_area.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.text_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.text_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_area.setStyleSheet(
            """
            QTextEdit {
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.25);
                border-radius: 8px;
                color: white;
                padding: 10px;
            }
            QTextEdit:focus {
                border: 1px solid rgba(255, 255, 255, 0.45);
                outline: none;
            }
            """
        )
        # Size constraints: ~5 lines visible, scroll after that
        fm = self.text_area.fontMetrics()
        line_height = fm.lineSpacing()
        target_lines = 5
        min_height = int(line_height * target_lines + 24)
        max_height = int(line_height * 10 + 32)
        self.text_area.setMinimumHeight(min_height)
        self.text_area.setMaximumHeight(max_height)
        layout.addWidget(self.text_area)

        # Buttons
        button_row = QHBoxLayout()
        self.confirm_button = QPushButton("Confirm")
        self.cancel_button = QPushButton("Cancel")

        button_style = """
            QPushButton {
                background-color: #4fa2f0;
                color: white;
                border: none;
                padding: 8px 14px;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #6bb1f2;
            }
            QPushButton#cancel {
                background-color: #555;
            }
            QPushButton#cancel:hover {
                background-color: #666;
            }
        """
        self.confirm_button.setStyleSheet(button_style)
        self.cancel_button.setStyleSheet(button_style)
        self.cancel_button.setObjectName("cancel")

        button_row.addStretch()
        button_row.addWidget(self.confirm_button)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        self.setMinimumWidth(420)
        self.setMaximumWidth(720)

        # Connections
        self.confirm_button.clicked.connect(self.on_confirm)
        self.cancel_button.clicked.connect(self.on_cancel)

    def on_confirm(self):
        self._closing = True
        self.confirmed.emit(self.text_area.toPlainText())
        self._fade_out_and_hide()

    def on_cancel(self):
        self._closing = True
        self.cancelled.emit()
        self._fade_out_and_hide()

    def set_text(self, text):
        self.text_area.setPlainText(text)
        self.text_area.moveCursor(QTextCursor.End)

    def show_at_cursor(self):
        self._closing = False
        self.adjustSize()

        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

        width = self.width()
        height = self.height()

        anchor_rect = QGuiApplication.inputMethod().cursorRectangle()
        if anchor_rect.width() > 0 and anchor_rect.height() > 0:
            anchor_center = anchor_rect.center().toPoint()
            x = anchor_center.x() - width // 2
            y = int(anchor_rect.top()) - height - 12
        else:
            cursor_pos = QCursor.pos()
            x = cursor_pos.x() - width // 2
            y = cursor_pos.y() - height - 12

        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left() + 10, min(x, screen.right() - width - 10))
        y = max(screen.top() + 10, min(y, screen.bottom() - height - 10))

        self.move(x, y)
        self._opacity.setOpacity(0.0)
        self.show()
        self.activateWindow()
        self.setFocus()
        self._animate_opacity(0.0, 1.0)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.on_confirm()
        elif event.key() == Qt.Key_Escape:
            self.on_cancel()
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent):
        super().focusOutEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and not self._closing:
            # Ignore clicks inside this widget or its children
            if hasattr(obj, "isWidgetType") and obj.isWidgetType():
                if obj is self or self.isAncestorOf(obj):
                    return super().eventFilter(obj, event)

            pos = event.globalPosition().toPoint()
            if not self.geometry().contains(self.mapFromGlobal(pos)):
                self.on_cancel()
                return True
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def _animate_opacity(self, start: float, end: float, on_finished=None):
        animation = QPropertyAnimation(self._opacity, b"opacity", self)
        animation.setDuration(self._fade_duration_ms)
        animation.setStartValue(start)
        animation.setEndValue(end)
        if on_finished:
            animation.finished.connect(on_finished)
        animation.start(QAbstractAnimation.DeleteWhenStopped)

    def _fade_out_and_hide(self):
        def _finish():
            self.hide()
            self._opacity.setOpacity(0.0)

        self._animate_opacity(self._opacity.opacity(), 0.0, _finish)


if __name__ == "__main__":
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

    from PySide6.QtCore import QTimer

    QTimer.singleShot(1000, lambda: capture_box.set_text("This is a test transcription."))
    QTimer.singleShot(2000, lambda: capture_box.set_text("This is a test transcription that is getting longer."))

    sys.exit(app.exec())
