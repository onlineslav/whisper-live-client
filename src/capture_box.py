import sys
import time

from PySide6.QtWidgets import (
    QWidget,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QGraphicsBlurEffect,
    QPushButton,
    QVBoxLayout,
    QApplication,
    QHBoxLayout,
    QTextEdit,
    QGraphicsOpacityEffect,
)
from PySide6.QtCore import (
    Qt,
    QRect,
    QRectF,
    Signal,
    QEvent,
    QPoint,
    QTimer,
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
    QTextBlockFormat,
    QPainter,
    QPen,
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
# Empty means whatever Qt would have used, which is the system UI font. Kept
# as the default rather than naming a face: the one font certain to be
# installed and to look native is the one already in use.
DEFAULT_FONT_FAMILY = ""
MIN_FONT_SIZE_PX = 9
MAX_FONT_SIZE_PX = 48

# Lines of transcription shown before the text area starts scrolling, and the
# ceiling it may grow to. Both scale with the font so the box shows the same
# amount of text at any size.
VISIBLE_LINES = 5
MAX_LINES = 10

# Space between lines, as a percentage of the face's own line height. 100 is
# whatever the typeface asks for; above that opens the transcript up, which
# is worth having when it is being read at a glance over a busy window.
DEFAULT_LINE_SPACING = 100
MIN_LINE_SPACING = 80
MAX_LINE_SPACING = 250

# Grow the box downwards for a long transcript instead of scrolling it. Off
# by default: a box that changes size while it is being read is the more
# surprising of the two, so it is opted into.
DEFAULT_GROW_TO_FIT = False

# The most of the screen's height a grown box may take. The room it can grow
# into is reserved when the box opens -- see _reserve_grow_room -- and that
# reservation also sets how much screen has to be photographed for the frost,
# so it is bounded for the cost as well as for the look.
GROW_SCREEN_FRACTION = 0.8

# The transcript field's own padding and border, added to the height of the
# text to get the height of the widget around it.
FIELD_CHROME_PX = 24

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

# Corner radius of the box, in px, and of the transcript field inside it.
# The field's has to match the border-radius its stylesheet sets, or the
# frost drawn behind it will not line up with the edge drawn over it.
CORNER_RADIUS = 10
FIELD_RADIUS = 8

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
# The panel and the field are frosted separately: the panel's frost is what
# hides the desktop, the field's is a second sheet of glass over it, and
# wanting one is not wanting the other.
DEFAULT_PANEL_FROST = True
DEFAULT_FIELD_FROST = False

# Blur radius, in pixels -- a real gaussian one. The blur used to be a
# downscale to a few per cent followed by a smooth upscale, which is fast but
# looks it: scaling 460px down to 41 and back throws away every gradient in
# the picture and returns banding and blocky edges in their place. A gaussian
# at this size costs single-digit milliseconds, which is not worth saving.
DEFAULT_BLUR_RADIUS = 24
MIN_BLUR_RADIUS = 2
MAX_BLUR_RADIUS = 80

# How far outside the box to grab before blurring, as a multiple of the
# radius. A gaussian samples past the edges of the picture it is given, and
# past the edge there is nothing -- so blurring exactly the box's rectangle
# pulls transparency inwards and leaves a dark vignette all the way round.
# Grabbing wider and cropping back afterwards removes it, and is what the
# glass would do anyway: what is just outside the box bleeds into it.
FROST_PAD_FACTOR = 2

# Blur alone is not enough to look like glass. A blurred dark window is just
# a dark smear, and under any tint at all it is indistinguishable from a flat
# fill -- which is what "the frost is not frosting" looks like on a dark
# desktop. Windows' own acrylic does not stop at blurring either: it boosts
# saturation hard and blends the result towards a middle luminosity, which is
# why acrylic over a black window still reads as a surface with something
# behind it rather than as black.
#
# The same two moves here. Saturation first, so what colour survived the blur
# is worth seeing; then the whole range compressed towards a target
# brightness, which lifts dark content off the floor and pulls bright content
# down, leaving texture visible either way.
#
# Brightness is why a panel at 0% opacity is not transparent but grey: at 0%
# there is no tint at all, so what is left is the levelled snapshot, and
# levelling is what pulled it to the middle. Turning brightness down takes
# the frost back towards the real colours of whatever is behind it; turning
# the mix to 0 disables levelling entirely.
DEFAULT_SATURATION = 200
DEFAULT_BRIGHTNESS = 108
DEFAULT_LEVELLING = 42


def _gaussian(pixmap: QPixmap, radius: int) -> QPixmap:
    """A real gaussian blur of `pixmap`, at full resolution.

    Through a one-item QGraphicsScene because QGraphicsBlurEffect is the only
    gaussian Qt exposes, and an effect can only be applied to an item in a
    scene -- not to a pixmap directly.
    """
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pixmap)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    canvas = QImage(pixmap.size(), QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    scene.render(painter, QRectF(canvas.rect()), QRectF(pixmap.rect()))
    painter.end()
    return QPixmap.fromImage(canvas)


def _blurred(pixmap: QPixmap, radius: int, saturation: int,
             brightness: int, levelling: int) -> QPixmap:
    """A blurred, saturated, brightness-levelled copy at the original size.

    The colour work is done with the painter rather than pixel by pixel. At
    full resolution a Python loop over a hundred thousand pixels takes about
    a second, and the whole point of the gaussian is to stop working at the
    postage-stamp size where such a loop was affordable.
    """
    out = _gaussian(pixmap, _clamp_int(radius, MIN_BLUR_RADIUS, MAX_BLUR_RADIUS))
    mix = _clamp_int(levelling, 0, 100) / 100.0
    saturation = _clamp_int(saturation, 0, 400) / 100.0

    if mix:
        # Pulling every pixel a fraction of the way towards one brightness is
        # the same operation as compositing that brightness over the lot at
        # that alpha. Exactly the same arithmetic, done by the rasteriser.
        # First, so the saturation pass below works on lifted midtones
        # instead of crushing what it finds on the floor.
        brightness = _clamp_int(brightness, 0, 255)
        painter = QPainter(out)
        painter.fillRect(out.rect(),
                         QColor(brightness, brightness, brightness, int(mix * 255)))
        painter.end()

    if saturation > 1.0:
        # Overlay against itself deepens what has colour and leaves neutrals
        # about where they were, which is the part of a saturation boost that
        # matters here. Repeated for amounts past one layer's worth.
        remaining = saturation - 1.0
        while remaining > 0.001:
            layer = QPixmap(out)
            painter = QPainter(out)
            painter.setCompositionMode(QPainter.CompositionMode_Overlay)
            painter.setOpacity(min(1.0, remaining))
            painter.drawPixmap(0, 0, layer)
            painter.end()
            remaining -= 1.0
    elif saturation < 1.0:
        grey = out.toImage().convertToFormat(QImage.Format_Grayscale8)
        painter = QPainter(out)
        painter.setOpacity(1.0 - saturation)
        painter.drawImage(0, 0, grey)
        painter.end()
    return out


def _clamp_int(value, low: int, high: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(value, high))


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


class BusyButton(QPushButton):
    """A button that spins in place of its own label while it is waiting.

    The Confirm button doubles as the progress indicator rather than a
    separate spinner appearing next to it. The wait belongs to the action
    that started it, and anything new appearing in the row would shift the
    buttons sideways at the exact moment the user has just clicked one.
    """

    # Fast enough to read as motion rather than as a stutter, and slow enough
    # not to spend a repaint every frame on a window that is also being faded.
    SPIN_INTERVAL_MS = 40
    SPIN_STEP_DEG = 26
    ARC_SPAN_DEG = 100
    ARC_INSET_PX = 7
    # Qt's own "no maximum", for undoing the fixed width below. Spelled out
    # rather than imported: QWIDGETSIZE_MAX is not exported by every PySide
    # build, and it has never been anything but this.
    _NO_MAX = 16777215

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._label = text
        self._busy = False
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.SPIN_INTERVAL_MS)
        self._timer.timeout.connect(self._advance)

    def is_busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool):
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            # Pinned to the width it had with its label in it, before the
            # label goes: a button that shrinks to spinner width would drag
            # Cancel and the meter across the row with it.
            self.setFixedWidth(self.width())
            self.setText("")
            self._angle = 0
            self._timer.start()
        else:
            self._timer.stop()
            self.setText(self._label)
            self.setMinimumWidth(0)
            self.setMaximumWidth(self._NO_MAX)
        self.update()

    def _advance(self):
        self._angle = (self._angle + self.SPIN_STEP_DEG) % 360
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._busy:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(255, 255, 255, 235))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        side = min(self.width(), self.height()) - 2 * self.ARC_INSET_PX
        if side <= 0:
            return
        arc = QRectF((self.width() - side) / 2.0, (self.height() - side) / 2.0,
                     side, side)
        # Qt measures arcs in sixteenths of a degree, anticlockwise from three
        # o'clock; negated so the gap chases the arc the way round every other
        # spinner on the machine turns.
        painter.drawArc(arc, int(-self._angle * 16), int(self.ARC_SPAN_DEG * 16))


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
        # A preview box stands in the Settings window rather than over the
        # app being dictated into, so every way a real capture ends -- losing
        # activation, a click outside it, the keyboard -- would close it the
        # moment the user went back to the control they are adjusting. It is
        # the same widget otherwise, which is the point of previewing it.
        self._preview_mode = False

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
        self._panel_frost = DEFAULT_PANEL_FROST
        self._field_frost = DEFAULT_FIELD_FROST
        self._blur = DEFAULT_BLUR_RADIUS
        self._saturation = DEFAULT_SATURATION
        self._brightness = DEFAULT_BRIGHTNESS
        self._levelling = DEFAULT_LEVELLING
        # The blurred snapshot of whatever was behind the box when it opened,
        # or None when neither surface is frosted and nothing was grabbed.
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
                %(family)s
            }
            QTextEdit:focus {
                border: 1px solid rgba(%(r)d, %(g)d, %(b)d, %(focus).3f);
                outline: none;
            }
        """
        self._field_color = DEFAULT_FIELD_COLOR
        self._field_opacity = DEFAULT_FIELD_OPACITY
        self._font_family = DEFAULT_FONT_FAMILY
        self._font_size = 0
        self._line_spacing = DEFAULT_LINE_SPACING
        self._grow_to_fit = DEFAULT_GROW_TO_FIT
        # The tallest the whole box may become where it currently stands,
        # decided when it opens; 0 until then, and whenever it is not allowed
        # to grow at all.
        self._grow_room = 0
        # The field's height with nothing in it, kept because growing
        # overwrites the widget's own minimum and the floor has to survive
        # that to be restored when the transcript is short again.
        self._min_field_height = 0
        self.set_font_size(DEFAULT_FONT_SIZE_PX)
        layout.addWidget(self.text_area)

        # Buttons
        button_row = QHBoxLayout()
        # No automatic spacing: the row's only gaps are the two stretches
        # either side of the meter, and an inter-item spacing would be added
        # to the right-hand one only -- leaving the meter a few pixels off
        # centre for no visible reason.
        button_row.setSpacing(0)
        self.confirm_button = BusyButton("Confirm")
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
        if self._preview_mode:
            self.hide()
            return
        if self.confirm_button.is_busy():
            # Already confirmed and waiting on the paste. A second confirm has
            # nothing left to do and would emit the transcript twice.
            return
        self._closing = True
        self.confirmed.emit(self.text_area.toPlainText())
        # Whoever is listening may have asked the box to stay up and spin --
        # set_busy() is called from inside the emit above, synchronously --
        # in which case they will take it down when the paste is on its way.
        if not self.confirm_button.is_busy():
            self._fade_out_and_hide()

    def on_cancel(self):
        if self._preview_mode:
            self.hide()
            return
        if self.confirm_button.is_busy():
            # The transcript is already committed and the paste is in flight;
            # there is nothing here left to cancel.
            return
        self._closing = True
        self.cancelled.emit()
        self._fade_out_and_hide()

    def set_busy(self, busy: bool):
        """Show the box as still working on the transcript it handed over.

        Confirming can be followed by a second or more of nothing -- the
        final pass re-transcribes the whole capture before the text is pasted
        -- and with the box already gone that gap reads as a hang. So the box
        stays up with the Confirm button spinning until whoever asked for the
        wait takes it down.
        """
        if busy:
            # This capture is already settled, whether it was confirmed from
            # the button or from the hotkey. Saying so here is what stops a
            # click in another window, or the box losing activation to the
            # app the paste is heading for, from being read as a cancel.
            self._closing = True
        self.confirm_button.set_busy(busy)

    def set_level_db(self, db: float):
        """Feed the meter a raw chunk level in dBFS."""
        self.meter.set_level_db(db)

    def set_meter_style(self, style: str):
        self.meter.set_style(style)

    def set_surface(self, color: str = None, opacity=None, frosted=None):
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
        if frosted is not None:
            self._panel_frost = bool(frosted)
        self.update()

    def set_frost(self, blur=None, saturation=None, brightness=None, levelling=None):
        """Set how the blurred snapshot is processed, for both surfaces.

        One snapshot serves the panel and the field, so these are not per
        surface: the field's frost is a crop of the same picture.
        """
        if blur is not None:
            self._blur = _clamp_int(blur, MIN_BLUR_RADIUS, MAX_BLUR_RADIUS)
        if saturation is not None:
            self._saturation = _clamp_int(saturation, 0, 400)
        if brightness is not None:
            self._brightness = _clamp_int(brightness, 0, 255)
        if levelling is not None:
            self._levelling = _clamp_int(levelling, 0, 100)
        self.update()

    def wants_frost(self) -> bool:
        return self._panel_frost or self._field_frost

    def _grab_frost(self):
        """Snapshot and blur what is behind the box, before it is shown.

        Called with the box positioned but still hidden, which is the only
        moment the screen underneath is both in its final place and not
        covered by the box itself.
        """
        self._frost = None
        if not self.wants_frost():
            return
        geometry = self.geometry()
        # Photographed for every height the box may reach, not just the one it
        # has: a box that grows afterwards would otherwise have nothing behind
        # its lower half but a stretched copy of the top.
        if self._grow_room > geometry.height():
            geometry.setHeight(self._grow_room)
        screen = (QGuiApplication.screenAt(geometry.center())
                  or QApplication.primaryScreen())
        if screen is None:
            return
        # Wider than the box, so the blur has real pixels to reach for at the
        # edges rather than the transparency outside the picture. Clamped to
        # the screen, since a box near an edge cannot grab past it.
        pad = self._blur * FROST_PAD_FACTOR
        padded = geometry.adjusted(-pad, -pad, pad, pad).intersected(screen.geometry())
        if padded.isEmpty():
            return
        try:
            shot = screen.grabWindow(
                0, padded.x(), padded.y(), padded.width(), padded.height())
        except Exception:
            return
        if shot.isNull():
            return
        # Drawn into a rect measured in logical pixels, so a ratio inherited
        # from a scaled display would halve the image inside it.
        shot.setDevicePixelRatio(1.0)
        blurred = _blurred(shot, self._blur, self._saturation,
                           self._brightness, self._levelling)
        # Back to the box, so everything downstream can treat the frost as
        # being exactly the box's size.
        self._frost = blurred.copy(QRect(
            geometry.x() - padded.x(), geometry.y() - padded.y(),
            geometry.width(), geometry.height()))

    def set_field(self, color: str = None, opacity=None, frosted=None):
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
        if frosted is not None:
            self._field_frost = bool(frosted)
        self._apply_field_style()
        self.update()

    def set_font_family(self, family: str):
        """Set the transcript's typeface, and resize the box to match it.

        Empty means the system UI font. The box's height is derived from the
        line height, and line height is a property of the face as much as of
        the size, so a change here has to re-measure exactly as a size change
        does -- a tall face would otherwise show four lines where the last
        one showed five.
        """
        family = (family or "").strip()
        if family == self._font_family:
            return
        self._font_family = family
        self._apply_field_style()
        self._resize_to_font()

    def _apply_field_style(self):
        r, g, b = _rgb(self._field_color)
        fill = self._field_opacity
        # Quoted, because family names have spaces in them and a bare one is
        # read as a list of keywords.
        family = f'font-family: "{self._font_family}";' if self._font_family else ""
        self.text_area.setStyleSheet(self._text_style % {
            "r": r, "g": g, "b": b,
            "fill": fill,
            "edge": min(1.0, fill + FIELD_BORDER_LIFT),
            "focus": min(1.0, fill + FIELD_FOCUS_LIFT),
            "size": self._font_size or DEFAULT_FONT_SIZE_PX,
            "family": family,
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
        if self._frost is not None and self._panel_frost:
            # Clipped to the same path as the tint, so the frost stops at the
            # rounded corners instead of squaring them off.
            painter.save()
            painter.setClipPath(path)
            # Source rectangle given explicitly rather than letting the whole
            # pixmap stretch into the box: with "grow to fit" on, the frost is
            # photographed at the full height the box may reach, and only the
            # part the box currently covers may be drawn. Stretching it would
            # smear the backdrop further every time a line of transcript
            # arrived.
            painter.drawPixmap(self.rect(), self._frost, self.rect())
            painter.restore()
        painter.fillPath(path, self._surface_color())

        if self._frost is not None and self._field_frost:
            # A second sheet of glass, over the panel's tint rather than
            # under it -- which is what makes the field read as its own
            # surface rather than as a lighter patch of the panel. The crop
            # is the same snapshot, so the two stay in register and the blur
            # continues across the join instead of restarting at it.
            #
            # The field's own fill and border are still the QTextEdit's, and
            # it paints them over this. That ordering is the whole trick: the
            # fill is semi-transparent, so it tints the frost underneath.
            field = self.text_area.geometry()
            field_path = QPainterPath()
            field_path.addRoundedRect(field, FIELD_RADIUS, FIELD_RADIUS)
            painter.save()
            painter.setClipPath(field_path)
            painter.drawPixmap(field, self._frost, field)
            painter.restore()

    def set_text(self, text):
        self.text_area.setPlainText(text)
        # Block formatting does not survive setPlainText, so the spacing is
        # re-stamped on every update rather than set once.
        self._apply_line_spacing()
        self.text_area.moveCursor(QTextCursor.End)
        self._grow_to_text()

    def set_line_spacing(self, percent):
        """Set the space between transcript lines, as a percentage.

        100 is the typeface's own line height. The box's height follows,
        exactly as the type size does: five lines has to stay five lines.
        """
        percent = _clamp_int(percent, MIN_LINE_SPACING, MAX_LINE_SPACING)
        if percent == self._line_spacing:
            return
        self._line_spacing = percent
        self._apply_line_spacing()
        self._resize_to_font()

    def set_grow_to_fit(self, grow: bool):
        """Grow the box downwards for a long transcript, instead of scrolling.

        The room to grow into is reserved when the box opens, so turning this
        on for a box already on screen only takes effect from the next
        capture -- which is where it is set from anyway.
        """
        grow = bool(grow)
        if grow == self._grow_to_fit:
            return
        self._grow_to_fit = grow
        if not grow:
            self._grow_room = 0
        self._resize_to_font()
        self._grow_to_text()

    def _apply_line_spacing(self):
        """Stamp the current spacing onto every block in the transcript."""
        cursor = self.text_area.textCursor()
        cursor.select(QTextCursor.Document)
        spacing = QTextBlockFormat()
        # The height type goes in as a plain int: the overload PySide exposes
        # takes one, and passing the enum itself is a TypeError.
        spacing.setLineHeight(float(self._line_spacing),
                              QTextBlockFormat.ProportionalHeight.value)
        cursor.mergeBlockFormat(spacing)

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
        self._resize_to_font()

    def _line_height(self) -> float:
        """One line of transcript, in px, at the current face, size and spacing."""
        # Measured from an explicit QFont rather than the widget's own metrics:
        # a stylesheet font is not applied until the widget is next polished,
        # so fontMetrics() here would still report the previous one.
        font = QFont(self.text_area.font())
        if self._font_family:
            font.setFamily(self._font_family)
        font.setPixelSize(self._font_size or DEFAULT_FONT_SIZE_PX)
        return QFontMetrics(font).lineSpacing() * self._line_spacing / 100.0

    def _resize_to_font(self):
        """Re-derive the box's height from the current face, size and spacing."""
        line_height = self._line_height()
        self._min_field_height = int(line_height * VISIBLE_LINES + FIELD_CHROME_PX)
        self.text_area.setMinimumHeight(self._min_field_height)
        self.text_area.setMaximumHeight(self._field_height_cap())
        # The box was sized for the old type; let it shrink as well as grow.
        self.resize(self.sizeHint())

    def _field_height_cap(self) -> int:
        """The tallest the transcript field may get, in px.

        Without room reserved this is a fixed number of lines and the field
        scrolls past it. With it, the ceiling is the room itself, less
        everything in the box that is not the field.
        """
        if self._grow_to_fit and self._grow_room:
            chrome = self.height() - self.text_area.height()
            return max(self._min_field_height, self._grow_room - chrome)
        return int(self._line_height() * MAX_LINES + FIELD_CHROME_PX)

    def _grow_to_text(self):
        """Size the field to what it is holding, up to the room reserved.

        Both the minimum and the maximum are set, because a QTextEdit has no
        height of its own to speak of -- the layout gives it whatever the two
        allow, and pinning them together is how it is given a specific one.
        Past the ceiling the field stops growing and scrolls, which is the
        same box the setting was turned off for.
        """
        if not self._grow_to_fit:
            return
        document = self.text_area.document()
        # A document only knows its height once it knows its width, and the
        # viewport is the width the text actually wraps to.
        document.setTextWidth(self.text_area.viewport().width())
        needed = int(document.size().height()) + FIELD_CHROME_PX
        height = max(self._min_field_height, min(needed, self._field_height_cap()))
        if height == self.text_area.minimumHeight() == self.text_area.maximumHeight():
            return
        self.text_area.setMinimumHeight(height)
        self.text_area.setMaximumHeight(height)
        self.adjustSize()

    def _reserve_grow_room(self):
        """Work out how tall the box may become where it now stands.

        Downwards only, from the position it opened at, and never past the
        bottom of the screen. Fixing this once means the box grows into space
        it already owns rather than walking up the screen as the transcript
        arrives -- and means the frost behind it can be photographed for the
        whole reservation in one go, so a taller box is still in register with
        the backdrop it was given.
        """
        self._grow_room = 0
        if not self._grow_to_fit:
            return
        screen = (QGuiApplication.screenAt(self.geometry().center())
                  or QApplication.primaryScreen())
        if screen is None:
            return
        available = screen.availableGeometry()
        below = available.bottom() - self.y()
        self._grow_room = max(
            self.height(), min(below, int(available.height() * GROW_SCREEN_FRACTION)))
        self.text_area.setMaximumHeight(self._field_height_cap())

    def show_preview(self, at: QPoint = None):
        """Show the box as a live sample of itself, next to the Settings window.

        Never takes the foreground and never cancels itself, so it can sit
        there while its own appearance is adjusted behind it.
        """
        self._preview_mode = True
        self._closing = False
        self.set_busy(False)
        self.meter.reset()
        self.adjustSize()
        if at is not None:
            self._move_near(at)
        self._reserve_grow_room()
        self._grow_to_text()
        self._grab_frost()
        self._opacity.setOpacity(0.0)
        self.show()
        self.raise_()
        self._animate_opacity(0.0, 1.0)

    def refresh_backdrop(self):
        """Re-take the frost snapshot, for a box that is already up.

        Only worth calling when the backdrop has been switched on or the box
        has moved -- the snapshot does not go stale for a colour or opacity
        change, since neither moves the box or alters what is behind it. The
        box has to be out of its own photograph, hence the hide.
        """
        if not self.isVisible():
            return
        self.hide()
        QApplication.processEvents()
        self._reserve_grow_room()
        self._grab_frost()
        self.show()
        self.raise_()
        self._opacity.setOpacity(1.0)

    def show_at_cursor(self):
        self._preview_mode = False
        self._closing = False
        self.set_busy(False)
        self.meter.reset()
        self._shown_at = time.monotonic()
        self.adjustSize()

        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

        self._move_near(self._anchor_point())
        # Before the snapshot: the reservation decides how much of the screen
        # has to be photographed.
        self._reserve_grow_room()
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
        if self._preview_mode:
            # Confirming or cancelling a sample would mean pasting it.
            super().keyPressEvent(event)
            return
        if self.confirm_button.is_busy():
            # Waiting on the paste. Both keys are no-ops at this point, and
            # swallowing them stops one landing in the app underneath.
            event.accept()
            return
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
            and not self._preview_mode
            and not self._closing
            and self.isVisible()
            and not self.isActiveWindow()
            and time.monotonic() - self._shown_at > 0.35
        ):
            self.on_cancel()
        super().changeEvent(event)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.MouseButtonPress
                and not self._preview_mode and not self._closing):
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
        self.set_busy(False)
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
