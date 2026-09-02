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
    QPainter,
    QColor,
    QPainterPath,
    QPixmap,
    QImage,
)

from signal_meter import SignalMeter, STYLES as METER_STYLES
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

# The box has two surfaces, and they are set separately because they are
# doing different jobs. The PANEL is the whole window -- what separates the
# box from the desktop behind it. The FIELD is the inset the transcript sits
# in, which is there to mark the text off from the buttons and the meter, and
# reads as a light lift over the panel rather than as a colour of its own.
#
# The floor is 0: with the backdrop frosted, a panel with no tint at all is a
# legitimate setting -- the blur is doing the work -- and there is no reason
# to stop someone trying it.
DEFAULT_OPACITY = 0.55
MIN_OPACITY = 0.0
MAX_OPACITY = 1.0

# Near-black rather than black: a slight blue lift reads as a deliberate
# surface at high opacity, where flat #000 reads as a hole in the screen.
DEFAULT_BG_COLOR = "#12141a"

# The field, over the panel. White at a few per cent: a lift, not a fill.
DEFAULT_FIELD_OPACITY = 0.08
DEFAULT_FIELD_COLOR = "#ffffff"

# The field's border, relative to its fill. Derived rather than set: an edge
# that always sits a little above the fill it surrounds is what makes the
# inset read as an inset, at any fill.
FIELD_BORDER_LIFT = 0.17
FIELD_FOCUS_LIFT = 0.37

# Corner radius of the box, in px.
CORNER_RADIUS = 10

# Frosted glass: what is behind the box, blurred, so a page of text
# underneath reads as texture instead of competing with the transcript.
#
# Done by snapshotting the screen under the box and blurring that, rather
# than by asking the compositor. Windows has two APIs for it and neither
# works here: SetWindowCompositionAttribute's acrylic ignores the tint's
# alpha on Windows 11 26xxx (any alpha, down to 1, renders as an opaque
# tint, so there is nothing to see the blur through), and DWM's
# SYSTEMBACKDROP_TYPE returns S_OK on a frameless tool window and then draws
# flat grey. A snapshot is not live -- it is the screen as it was when the
# box opened -- but the box is up for a few seconds over a page that is not
# moving, which is exactly the case it has to look right in.
BACKDROP_NONE = "none"
BACKDROP_FROST = "frost"
BACKDROPS = (BACKDROP_NONE, BACKDROP_FROST)
DEFAULT_BACKDROP = BACKDROP_FROST

# The blur is a downscale followed by a smooth upscale, repeated. Cheap, and
# at this scale indistinguishable from a real gaussian: the point is to
# destroy legibility, not to be a nice kernel. Two passes, because one leaves
# large type still readable as shapes.
FROST_SCALE = 0.09
FROST_PASSES = 2

# Blur alone is not enough to look like glass. A blurred dark window is just
# a dark smear, and under any tint at all it is indistinguishable from a flat
# fill -- which is what "the frost is not frosting" looks like on a dark
# desktop. Windows' own acrylic does not stop at blurring either: it boosts
# saturation hard and blends the result towards a middle luminosity, which is
# why acrylic over a black window still reads as a surface with something
# behind it rather than as black.
#
# The same two moves here. Saturation first, so what colour survived the blur
# is worth seeing; then the whole range compressed towards FROST_LUMINOSITY,
# which lifts dark content off the floor and pulls bright content down,
# leaving texture visible either way.
FROST_SATURATION = 2.0
FROST_LUMINOSITY = 108
FROST_LUMINOSITY_MIX = 0.42


def _blurred(pixmap: QPixmap) -> QPixmap:
    """A blurred, saturated, luminosity-levelled copy at the original size."""
    width = max(1, int(pixmap.width() * FROST_SCALE))
    height = max(1, int(pixmap.height() * FROST_SCALE))
    small = pixmap.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    for _ in range(FROST_PASSES - 1):
        small = small.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    # Per-pixel, but on the downscaled image -- a few hundred pixels rather
    # than the hundred thousand of the full-size one, and it is about to be
    # smoothly upscaled anyway, so the detail would be thrown away regardless.
    image = small.toImage().convertToFormat(QImage.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            hue, saturation, lightness, alpha = color.getHsl()
            saturation = min(255, int(saturation * FROST_SATURATION))
            lightness = int(lightness * (1.0 - FROST_LUMINOSITY_MIX)
                            + FROST_LUMINOSITY * FROST_LUMINOSITY_MIX)
            # A fully desaturated pixel reports hue -1, which QColor rejects.
            image.setPixelColor(x, y, QColor.fromHsl(
                max(0, hue), saturation, max(0, min(255, lightness)), alpha))
    return QPixmap.fromImage(image).scaled(
        pixmap.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _clamp_opacity(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_OPACITY
    return max(MIN_OPACITY, min(value, MAX_OPACITY))


def _rgb(color: str):
    """(r, g, b) from a #rrggbb string, falling back to the default."""
    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return _rgb(DEFAULT_BG_COLOR)


class CaptureBox(QWidget):
    """
    A borderless, semi-transparent window for live transcription display.
    """

    confirmed = Signal(str)
    cancelled = Signal()
    # Emitted when the user clicks the meter to try a different style, so the
    # choice can be written back to the config file.
    meter_style_changed = Signal(str)

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
        # Text colour and default type size only. The surface itself is
        # painted in paintEvent rather than set here: a bare QWidget does not
        # draw a stylesheet background at all, and the box had been styled for
        # a dark translucent one since it was written without ever painting
        # it. What read as the box was its children's own backgrounds, with
        # the gaps between them showing the screen straight through -- which
        # is why the transcript was unreadable over a page of text.
        self.setStyleSheet("color: white; font-size: 14px;")
        self._bg_color = DEFAULT_BG_COLOR
        self._opacity_level = DEFAULT_OPACITY
        self._backdrop = DEFAULT_BACKDROP
        # The blurred snapshot of whatever was behind the box when it opened,
        # or None when the backdrop is off or the grab failed.
        self._frost = None

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
                background-color: rgba(%(r)d, %(g)d, %(b)d, %(fill).3f);
                border: 1px solid rgba(%(r)d, %(g)d, %(b)d, %(edge).3f);
                border-radius: 8px;
                color: white;
                padding: 10px;
                font-size: %(size)dpx;
            }
            QTextEdit:focus {
                border: 1px solid rgba(%(r)d, %(g)d, %(b)d, %(focus).3f);
                outline: none;
            }
        """
        self._field_color = DEFAULT_FIELD_COLOR
        self._field_opacity = DEFAULT_FIELD_OPACITY
        self._font_size = 0
        self.set_font_size(DEFAULT_FONT_SIZE_PX)
        layout.addWidget(self.text_area)

        # Buttons
        button_row = QHBoxLayout()
        # No automatic spacing: the row's only gaps are the two stretches
        # either side of the meter, and an inter-item spacing would be added
        # to the right-hand one only -- leaving the meter a few pixels off
        # centre for no visible reason.
        button_row.setSpacing(0)
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

        # The meter lives in the otherwise empty stretch to the left of the
        # buttons: it is in view while the user is speaking and reading the
        # transcript, without taking any room from either.
        self.meter = SignalMeter(self)
        self.meter.style_changed.connect(self.meter_style_changed)

        # A stretch on each side centres the meter in the space left over by
        # the buttons, rather than pinning it to the left edge with all the
        # slack pooled on one side of it.
        button_row.addStretch()
        button_row.addWidget(self.meter, 0, Qt.AlignVCenter)
        button_row.addStretch()
        button_row.addWidget(self.confirm_button)
        button_row.addSpacing(8)
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

    def set_level_db(self, db: float):
        """Feed the meter a raw chunk level in dBFS."""
        self.meter.set_level_db(db)

    def set_meter_style(self, style: str):
        self.meter.set_style(style)

    def set_surface(self, color: str = None, opacity=None, backdrop: str = None):
        """Set the box's background colour, how solid it is, and its backdrop.

        Only the background takes the opacity -- the text, the buttons and the
        meter stay fully opaque on top of it. Fading the whole window instead
        (setWindowOpacity, or the fade effect below) would take the transcript
        down with it, which is the opposite of what this control is for.

        `backdrop` asks the compositor to blur what is behind the box. Where
        it obliges, the tint goes with it and paintEvent draws nothing; the
        opacity then reads as how much of the blurred content comes through.
        """
        if color is not None:
            self._bg_color = color
        if opacity is not None:
            self._opacity_level = _clamp_opacity(opacity)
        if backdrop is not None:
            self._backdrop = backdrop if backdrop in BACKDROPS else BACKDROP_NONE
        self.update()

    def _grab_frost(self):
        """Snapshot and blur what is behind the box, before it is shown.

        Called with the box positioned but still hidden, which is the only
        moment the screen underneath is both in its final place and not
        covered by the box itself.
        """
        self._frost = None
        if self._backdrop != BACKDROP_FROST:
            return
        geometry = self.geometry()
        screen = (QGuiApplication.screenAt(geometry.center())
                  or QApplication.primaryScreen())
        if screen is None:
            return
        try:
            shot = screen.grabWindow(
                0, geometry.x(), geometry.y(), geometry.width(), geometry.height())
        except Exception:
            return
        if shot.isNull():
            return
        # Drawn into a rect measured in logical pixels, so a ratio inherited
        # from a scaled display would halve the image inside it.
        shot.setDevicePixelRatio(1.0)
        self._frost = _blurred(shot)

    def set_field(self, color: str = None, opacity=None):
        """Set the colour and fill of the inset the transcript sits in.

        Separate from set_surface() because the two are answering different
        questions: the panel's opacity is about the desktop behind the box,
        the field's is about telling the transcript apart from the rest of
        the box. Turning one up is rarely a reason to touch the other.
        """
        if color is not None:
            self._field_color = color
        if opacity is not None:
            self._field_opacity = _clamp_opacity(opacity)
        self._apply_field_style()

    def _apply_field_style(self):
        r, g, b = _rgb(self._field_color)
        fill = self._field_opacity
        self.text_area.setStyleSheet(self._text_style % {
            "r": r, "g": g, "b": b,
            "fill": fill,
            "edge": min(1.0, fill + FIELD_BORDER_LIFT),
            "focus": min(1.0, fill + FIELD_FOCUS_LIFT),
            "size": self._font_size or DEFAULT_FONT_SIZE_PX,
        })

    def _surface_color(self) -> QColor:
        r, g, b = _rgb(self._bg_color)
        return QColor(r, g, b, int(round(self._opacity_level * 255)))

    def paintEvent(self, event):
        """Draw the box's surface: one rounded rectangle, and nothing else.

        Done here rather than in the stylesheet because a plain QWidget never
        draws a styled background, and because a border-radius on a
        translucent window leaves the corners aliased against whatever is
        behind them. An explicit path can be antialiased.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        # A half-pixel inset so the antialiased edge has somewhere to land;
        # without it the outermost row of pixels is clipped and the curve
        # goes hard-edged again.
        path.addRoundedRect(
            self.rect().adjusted(0, 0, -1, -1), CORNER_RADIUS, CORNER_RADIUS)
        if self._frost is not None:
            # Clipped to the same path as the tint, so the frost stops at the
            # rounded corners instead of squaring them off.
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(self.rect(), self._frost)
            painter.restore()
        painter.fillPath(path, self._surface_color())

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
        self._apply_field_style()

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
        self.meter.reset()
        self._shown_at = time.monotonic()
        self.adjustSize()

        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

        self._move_near(self._anchor_point())
        # Before show(), so the box does not photograph itself.
        self._grab_frost()
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
        self.meter.reset()
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

    # Fake speech in dBFS so the meters can be judged without a mic: a room
    # tone floor with phrases riding on top of it, syllable-rate detail inside
    # each phrase, and one deliberately quieter phrase to show the auto-range
    # re-scaling to it.
    import math as _math
    import random as _random

    _tick = {"n": 0}

    def _drive_meter():
        _tick["n"] += 1
        t = _tick["n"] * 0.064
        phrase = _math.sin(t * 0.8)
        loud = -14.0 if (t % 24.0) < 12.0 else -26.0
        if phrase > 0.1:
            syllables = 0.5 + 0.5 * _math.sin(t * 11.0)
            db = loud - 16.0 * (1.0 - phrase * syllables)
        else:
            db = -58.0
        capture_box.set_level_db(db + _random.uniform(-1.5, 1.5))

    meter_timer = QTimer()
    meter_timer.timeout.connect(_drive_meter)
    meter_timer.start(64)

    sys.exit(app.exec())
