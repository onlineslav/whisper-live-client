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
    QPoint,
    QPropertyAnimation,
    QAbstractAnimation,
)
from PySide6.QtGui import (
    QCursor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QFocusEvent,
    QTextOption,
    QGuiApplication,
    QTextCursor,
)

from win_focus import focus_window

# Distance from the anchor point to the box's top-left corner. The box hangs
# down and to the right, like a tooltip; the offset is enough to clear the
# mouse pointer's own bitmap so the box never opens underneath it.
ANCHOR_OFFSET_X = 16
ANCHOR_OFFSET_Y = 18

DEFAULT_FONT_SIZE_PX = 14
MIN_FONT_SIZE_PX = 9
MAX_FONT_SIZE_PX = 48

# Lines of transcription shown before the text area starts scrolling, and the
# ceiling it may grow to. Both scale with the font so the box shows the same
# amount of text at any size.
VISIBLE_LINES = 5
MAX_LINES = 10


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

        # Never shown (the window is frameless), but it makes the box
        # identifiable in window lists and in logs when tracing a stray paste.
        self.setWindowTitle("WhisperBoard Capture")
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
        # The type size is user-configurable and also drives the box's height,
        # so the stylesheet is a template and both are applied together in
        # set_font_size().
        self._text_style = """
            QTextEdit {
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.25);
                border-radius: 8px;
                color: white;
                padding: 10px;
                font-size: %dpx;
            }
            QTextEdit:focus {
                border: 1px solid rgba(255, 255, 255, 0.45);
                outline: none;
            }
        """
        self._font_size = 0
        self.set_font_size(DEFAULT_FONT_SIZE_PX)
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

    def set_font_size(self, size_px: int):
        """Set the transcription type size and resize the box to match.

        The height limits are derived from the line height rather than fixed,
        so the box shows about the same number of lines at any size -- a fixed
        height would show two lines of large type, or waste half a screen on
        small.
        """
        try:
            size_px = int(size_px)
        except (TypeError, ValueError):
            size_px = DEFAULT_FONT_SIZE_PX
        size_px = max(MIN_FONT_SIZE_PX, min(size_px, MAX_FONT_SIZE_PX))
        if size_px == self._font_size:
            return
        self._font_size = size_px
        self.text_area.setStyleSheet(self._text_style % size_px)

        # Measured from an explicit QFont rather than the widget's own metrics:
        # a stylesheet font is not applied until the widget is next polished,
        # so fontMetrics() here would still report the previous size.
        font = QFont(self.text_area.font())
        font.setPixelSize(size_px)
        line_height = QFontMetrics(font).lineSpacing()
        self.text_area.setMinimumHeight(int(line_height * VISIBLE_LINES + 24))
        self.text_area.setMaximumHeight(int(line_height * MAX_LINES + 32))
        # The box was sized for the old type; let it shrink as well as grow.
        self.resize(self.sizeHint())

    def show_at_cursor(self):
        self._closing = False
        self._shown_at = time.monotonic()
        self.adjustSize()

        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

        self._move_near(self._anchor_point())
        self._opacity.setOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._take_foreground()
        self._animate_opacity(0.0, 1.0)

    def _anchor_point(self) -> QPoint:
        """The point the box hangs from: the text caret if visible, else the mouse.

        cursorRectangle() only reports carets inside our own process, so in
        practice this is the mouse position almost every time; the caret branch
        is kept for when real caret tracking lands.
        """
        caret = QGuiApplication.inputMethod().cursorRectangle()
        if caret.width() > 0 and caret.height() > 0:
            # Bottom-left of the caret, so the box hangs below the line being
            # typed rather than covering it.
            return QPoint(int(caret.left()), int(caret.bottom()))
        return QCursor.pos()

    def _move_near(self, anchor: QPoint):
        """Place the box below and to the right of `anchor`, flipping at edges."""
        width = self.width()
        height = self.height()

        # The screen under the anchor, not the primary one: on a multi-monitor
        # desktop the primary screen's geometry would push the box onto the
        # wrong display whenever the pointer is on a secondary one.
        screen = QGuiApplication.screenAt(anchor) or QApplication.primaryScreen()
        available = screen.availableGeometry()

        x = anchor.x() + ANCHOR_OFFSET_X
        y = anchor.y() + ANCHOR_OFFSET_Y

        # Flip to the other side rather than sliding along the edge. Sliding
        # would drag the box back underneath the pointer, which is the one
        # thing the offset exists to prevent.
        if x + width > available.right():
            x = anchor.x() - ANCHOR_OFFSET_X - width
        if y + height > available.bottom():
            y = anchor.y() - ANCHOR_OFFSET_Y - height

        # A box with room on neither side still has to land somewhere visible.
        x = max(available.left(), min(x, available.right() - width))
        y = max(available.top(), min(y, available.bottom() - height))

        self.move(x, y)

    def _take_foreground(self):
        """Give the box real keyboard focus.

        The box is shown from a global hotkey while another app owns the
        foreground, and Windows denies SetForegroundWindow to a background
        process — so activateWindow() leaves the box visible but unfocused and
        Enter/Esc go to the app underneath. focus_window() forces it through
        with the AttachThreadInput workaround.
        """
        try:
            # Fewer retries than the paste path uses: this runs while the user
            # is waiting for the box to appear, so a long retry loop would read
            # as lag. Losing focus here is recoverable (the box is still
            # visible and clickable); a slow box is not.
            focus_window(int(self.winId()), attempts=2)
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
