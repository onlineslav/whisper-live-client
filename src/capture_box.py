import sys
import time

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

from win_focus import focus_window


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
        self._shown_at = 0.0

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
        self._shown_at = time.monotonic()
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
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._take_foreground()
        self._animate_opacity(0.0, 1.0)

    def _take_foreground(self):
        """Give the box real keyboard focus.

        The box is shown from a global hotkey while another app owns the
        foreground, and Windows denies SetForegroundWindow to a background
        process — so activateWindow() leaves the box visible but unfocused and
        Enter/Esc go to the app underneath. focus_window() forces it through
        with the AttachThreadInput workaround.
        """
        try:
            focus_window(int(self.winId()))
        except Exception:
            pass

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            self.on_confirm()
        elif event.key() == Qt.Key_Escape:
            event.accept()
            self.on_cancel()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent):
        super().focusOutEvent(event)

    def changeEvent(self, event):
        """Cancel when the user clicks away to another window.

        A QApplication event filter only sees events delivered to our own
        process, so it can never observe a click in Notepad or the browser —
        losing window activation is the only reliable signal that the user
        switched away. Deactivations in the first moments after show() are
        ignored: taking the foreground is itself a burst of activation changes.
        """
        if (
            event.type() == QEvent.ActivationChange
            and not self._closing
            and self.isVisible()
            and not self.isActiveWindow()
            and time.monotonic() - self._shown_at > 0.35
        ):
            self.on_cancel()
        super().changeEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and not self._closing:
            # An app-wide filter sees every press twice: first on the receiving
            # QWindow, then on the QWidget under it. The QWindow is not a
            # widget, so an isWidgetType()-only check misses our own window and
            # cancels the capture before the Confirm button ever sees the click.
            if obj is self.windowHandle():
                return super().eventFilter(obj, event)
            if obj.isWidgetType() and (obj is self or self.isAncestorOf(obj)):
                return super().eventFilter(obj, event)

            # geometry() is in screen coordinates for a top-level window, so it
            # must be tested against the global click position. Mapping the
            # point to widget-local coords first made every click read as
            # "outside", cancelling the capture wherever the user clicked.
            if not self.geometry().contains(event.globalPosition().toPoint()):
                self.on_cancel()
                return True
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        # Any hide ends this capture — including one driven from main.py (the
        # hotkey-to-confirm path). Marking it closing here stops the
        # deactivation that follows from being read as a click-away cancel.
        self._closing = True
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
