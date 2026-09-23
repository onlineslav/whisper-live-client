"""Draw on the place a dictated transcript is about to land.

The Capture Box tells you *what* was heard. This tells you *where* it is
going, which until now was the one thing nothing on screen said: the box opens
by the mouse, the text lands at the caret, and the two are rarely in the same
place. A capture aimed at the wrong window looked exactly like one aimed at
the right one right up until the paste.

Two marks, from caret_target.py's answer:

  * A flash when the capture opens -- the outline swells and settles. This is
    the confirmation, and it is the part that does most of the work: the eye
    is drawn to the target at the one moment the user is asking where it is.
  * A caret bar that stays for the length of the capture, breathing gently so
    it reads as live rather than as a leftover artefact on the desktop.

How much can be drawn depends on how much was found, and the difference is
deliberately visible. An exact caret gets a bar at the insertion point; a
control with no caret gets its own outline; a window and nothing more gets a
dashed outline and its name, which is the honest way to say "in here
somewhere". A target that goes away goes grey, because a marker still
confidently pointing at a closed window is worse than no marker.

The window itself has three properties that are all load-bearing:

  * Transparent for input. It sits over another application's text field, so
    a click has to go straight through it. Qt.WindowTransparentForInput is
    WS_EX_TRANSPARENT, which is what makes the desktop underneath usable.
  * Never activated. An overlay that took the foreground on show would take
    the keyboard from the Capture Box -- or, for live typing, from the very
    text field the words are being typed into.
  * Excluded from screen capture. The Capture Box frosts its backdrop by
    photographing the screen behind it; without this the photograph would
    include the marker, and a stale copy of it would sit frozen in the
    frost for the rest of the capture.
"""

import ctypes
import math
import sys
import time

from ctypes import wintypes

from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

# How long the opening flash runs, and how far past the outline its halo
# travels before it has faded out. Short: this is a confirmation, not an
# animation to be admired, and anything slower starts to feel like the app
# is doing something rather than reporting something.
FLASH_MS = 460
FLASH_HALO_PX = 14

# The resting pulse on the caret bar, once the flash has finished. A full
# cycle every PULSE_PERIOD seconds, between these two alphas. Slow and shallow
# on purpose -- it has to survive being in the corner of someone's eye for a
# minute without becoming the thing they are looking at.
PULSE_PERIOD = 1.9
PULSE_MIN_ALPHA = 0.55
PULSE_MAX_ALPHA = 1.0

# Repaint rate while the overlay is up. 20 fps is plenty for a sine fade and
# a marker that does not move; the point of the cap is that a capture can run
# for a minute and none of it should be spent on this.
FRAME_MS = 50

# The caret bar: a bar of this width in logical pixels with a cap at each end,
# so it reads as a text insertion point rather than as a stray line. Wider
# than a real caret because it is standing in for one seen at a glance.
CARET_WIDTH = 3.0
CARET_CAP_WIDTH = 9.0
CARET_CAP_HEIGHT = 2.0

# The glow behind the caret bar, drawn as this many strokes of decreasing
# alpha and increasing width. A real blur would mean another offscreen
# surface per frame for something two pixels across.
GLOW_STEPS = 4
GLOW_SPREAD = 7.0

# The field outline, at rest and at the peak of the flash.
FIELD_RESTING_ALPHA = 105
FIELD_RADIUS = 6.0

# How far outside everything the overlay window extends, in physical pixels.
# Only has to be wide enough for the halo and the glow; a transparent window
# costs nothing for being bigger than it needs to be, so this is generous
# rather than derived, which also sidesteps needing the display's scale
# factor before the window that would report it exists.
WINDOW_PAD_PX = 48

# Grey, for a target that has gone. Deliberately not red: the window closing
# is something the user did, not a fault, and the transcript is still going
# to the clipboard either way.
LOST_COLOR = "#8a8f98"

DEFAULT_ACCENT = "#4fa2f0"

# See the module docstring. Windows 10 2004 and later.
WDA_EXCLUDEFROMCAPTURE = 0x11

# SetWindowPos flags, for the topmost nudge in _raise_without_activating.
_SWP_NOACTIVATE = 0x0010
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002


class _MONITORINFOEXW(ctypes.Structure):
    # szDevice is what ties a Windows monitor to a QScreen: Qt reports the
    # same "\\.\DISPLAYn" string from QScreen.name().
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def _monitor_info(physical_point):
    """(device name, native origin) for the monitor under a physical point.

    None if it cannot be determined, which leaves the caller on its identity
    mapping -- correct on any unscaled desktop.
    """
    try:
        user32 = ctypes.windll.user32
        point = wintypes.POINT(int(physical_point[0]), int(physical_point[1]))
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        # MONITOR_DEFAULTTONEAREST: the overlay's extent is padded outwards
        # and can start just off the edge of the screen the target is on.
        monitor = user32.MonitorFromPoint(point, 2)
        if not monitor:
            return None
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_MONITORINFOEXW)]
        if not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            return None
        return info.szDevice, (info.rcMonitor.left, info.rcMonitor.top)
    except Exception:
        return None


def mapping_for(physical_point):
    """How to turn physical screen pixels into Qt's coordinates, near a point.

    caret_target reports physical pixels, because that is what Win32 deals
    in. Qt positions and paints in logical ones. On a display at 100% the two
    are the same number and none of this matters; at 150%, or on a desktop
    mixing a scaled laptop panel with an unscaled monitor, they are different
    numbers with no fixed ratio between them -- each screen has its own
    scale, and Qt lays the screens out in a virtual desktop of its own that
    does not sit where Windows' does.

    The bridge is that Qt names its screens exactly as Windows does.
    GetMonitorInfo gives a monitor's native rectangle and its device name;
    the QScreen with the same name gives the logical rectangle for that same
    monitor, and the ratio between them. Anchoring to the monitor's own two
    origins keeps the conversion exact wherever either desktop puts it.

    Returns (native_x, native_y, logical_x, logical_y, ratio), which is the
    identity mapping when nothing better can be worked out -- and the
    identity is also the right answer on any unscaled desktop.
    """
    screen = None
    native_origin = (0, 0)
    if sys.platform == "win32":
        info = _monitor_info(physical_point)
        if info is not None:
            device, native_origin = info
            for candidate in QGuiApplication.screens():
                if candidate.name() == device:
                    screen = candidate
                    break

    if screen is None:
        # No match: a virtual display, a screen Qt has not noticed yet, or not
        # Windows at all. Fall back to the screen Qt believes the point is on
        # and assume the two desktops share an origin, which is true for the
        # single-monitor case this can realistically happen on.
        screen = (QGuiApplication.screenAt(QPoint(int(physical_point[0]),
                                                  int(physical_point[1])))
                  or QGuiApplication.primaryScreen())
        if screen is None:
            return (0, 0, 0, 0, 1.0)
        native_origin = (int(screen.geometry().left() * screen.devicePixelRatio()),
                         int(screen.geometry().top() * screen.devicePixelRatio()))

    logical_origin = screen.geometry().topLeft()
    return (native_origin[0], native_origin[1],
            logical_origin.x(), logical_origin.y(),
            screen.devicePixelRatio() or 1.0)


def apply_mapping(mapping, rect) -> QRectF:
    """A physical-pixel rectangle in Qt's coordinates, through `mapping`."""
    native_x, native_y, logical_x, logical_y, ratio = mapping
    return QRectF(logical_x + (rect[0] - native_x) / ratio,
                  logical_y + (rect[1] - native_y) / ratio,
                  rect[2] / ratio, rect[3] / ratio)


def anchor_below(rect):
    """The bottom-left of a physical rectangle, as a Qt screen point.

    What the Capture Box hangs from: it opens below and to the right of its
    anchor, so the bottom-left of a thing is "just under that thing". Given
    the caret, the box lands under the line being typed; given the field, it
    lands under the whole control, which is where it goes when ghost text is
    using the space in between. None for a rectangle that was never found.
    """
    if not rect:
        return None
    box = apply_mapping(mapping_for((rect[0], rect[1])), rect)
    return QPoint(int(box.left()), int(box.bottom()))


class TargetOverlay(QWidget):
    """The marker drawn over the app being dictated into."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("WhisperType Target")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # Belt and braces with the window flags above: the flags ask Windows
        # not to activate the window, this asks Qt not to try.
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)

        self._target = None
        self._accent = QColor(DEFAULT_ACCENT)
        # This window's own top-left in Qt's coordinates, and how to get there
        # from the physical screen pixels caret_target reports. Every
        # rectangle is painted through the pair. See _refresh_mapping.
        self._origin = QPoint(0, 0)
        self._map = (0, 0, 0, 0, 1.0)
        self._flash_started = 0.0
        self._shown_at = 0.0
        # Cleared while ghost text is drawing its own words at the caret --
        # see set_caret_visible.
        self._caret_visible = True

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._on_frame)

    # -- public API ---------------------------------------------------------

    def set_accent_color(self, color: str):
        """Use the Capture Box's accent, so the two read as one thing."""
        chosen = QColor(color)
        if chosen.isValid():
            self._accent = chosen
            if self.isVisible():
                self.update()

    def set_caret_visible(self, visible: bool):
        """Draw the caret bar, or leave that spot to something else.

        Turned off when ghost text is showing: it paints the transcript
        starting at the very pixel the bar stands on, so the two overlap and
        the bar ends up struck through the first letter. The words are the
        better marker of the two once they exist, and the field outline still
        says which control they are going into.
        """
        visible = bool(visible)
        if visible == self._caret_visible:
            return
        self._caret_visible = visible
        self._sync_timer()
        if self.isVisible():
            self.update()

    def show_target(self, target):
        """Show the marker for `target`, opening with the flash."""
        if target is None or target.source == "none":
            self.dismiss()
            return
        self._target = target
        self._flash_started = time.monotonic()
        self._shown_at = self._flash_started
        self._apply_geometry()
        self.setWindowOpacity(1.0)
        self.show()
        self._exclude_from_capture()
        self._raise_without_activating()
        self._sync_timer()
        self.update()

    def update_target(self, target):
        """Move or re-state the marker mid-capture, without re-flashing.

        Called from the poll in main.py. The flash belongs to the moment the
        capture opened; replaying it every time a window is nudged would turn
        a confirmation into a flicker.
        """
        if target is None or target.source == "none":
            self.dismiss()
            return
        if not self.isVisible():
            self.show_target(target)
            return
        previous = self._target
        self._target = target
        if previous is None or self._extent(previous) != self._extent(target):
            self._apply_geometry()
        self._sync_timer()
        self.update()

    def dismiss(self):
        """Take the marker down. Safe to call when it is already down."""
        self._timer.stop()
        self._target = None
        if self.isVisible():
            self.hide()

    # -- geometry -----------------------------------------------------------

    def _extent(self, target):
        """The physical-pixel rectangle the marker for `target` occupies.

        The union of whatever it is going to draw, padded. For a target known
        only as a window that is the whole window, which is large but only
        ever outlined -- see _sync_timer for why it does not also animate.
        """
        rects = [rect for rect in (target.caret, target.field) if rect]
        if not rects and target.window:
            rects = [target.window]
        if not rects:
            return None
        left = min(rect[0] for rect in rects)
        top = min(rect[1] for rect in rects)
        right = max(rect[0] + rect[2] for rect in rects)
        bottom = max(rect[1] + rect[3] for rect in rects)
        return (left - WINDOW_PAD_PX, top - WINDOW_PAD_PX,
                right - left + 2 * WINDOW_PAD_PX,
                bottom - top + 2 * WINDOW_PAD_PX)

    def _to_logical(self, rect) -> QRectF:
        """A physical-pixel screen rectangle, in Qt's screen coordinates.

        Through the mapping cached by the last _apply_geometry rather than
        re-derived: this runs for every rectangle of every frame, and the
        display a window is on does not change between two of them.
        """
        return apply_mapping(self._map, rect)

    def _apply_geometry(self):
        """Size and place the window around the target.

        Qt's own setGeometry rather than SetWindowPos: positioning the native
        window behind Qt's back leaves the widget believing it is still its
        default size, and the paint canvas is made from what the widget
        believes -- so the marker gets silently clipped to a 640x480 box in
        the corner of a window that is in the right place.
        """
        extent = self._extent(self._target) if self._target else None
        if extent is None:
            return
        self._map = mapping_for((extent[0], extent[1]))
        logical = apply_mapping(self._map, extent).toAlignedRect()
        self._origin = logical.topLeft()
        self.setGeometry(logical)

    def _raise_without_activating(self):
        """raise_() would take the foreground; this only changes Z order."""
        if sys.platform != "win32":
            self.raise_()
            return
        try:
            HWND_TOPMOST = ctypes.c_void_p(-1)
            ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(int(self.winId())), HWND_TOPMOST,
                0, 0, 0, 0,
                ctypes.c_uint(_SWP_NOACTIVATE | _SWP_NOSIZE | _SWP_NOMOVE))
        except Exception:
            pass

    def _exclude_from_capture(self):
        """Keep the marker out of the Capture Box's frost snapshot."""
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(int(self.winId())),
                ctypes.c_uint(WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            pass

    def _to_local(self, rect) -> QRectF:
        """A physical-pixel screen rectangle, in this widget's own coordinates."""
        return self._to_logical(rect).translated(-self._origin.x(), -self._origin.y())

    # -- animation ----------------------------------------------------------

    def _sync_timer(self):
        """Run the repaint timer only while something is actually moving.

        The flash always animates. After it, only a caret bar keeps going --
        it is the small mark that has to stay noticeable. A window outline is
        the size of the window, and repainting a maximised window's worth of
        transparency twenty times a second to fade a line in and out is not a
        trade worth making.
        """
        if self._target is None or not self.isVisible():
            self._timer.stop()
            return
        flashing = time.monotonic() - self._flash_started < FLASH_MS / 1000.0
        pulsing = (self._target.caret is not None and self._caret_visible
                   and self._target.source != "lost")
        if flashing or pulsing:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _on_frame(self):
        self.update()
        # The flash finishing is what turns the timer off for a target that
        # has nothing left to animate, so the check has to run per frame
        # rather than once when the flash starts.
        self._sync_timer()

    def _emphasis(self) -> float:
        """1.0 at the start of the flash, easing to 0.0 when it is over."""
        elapsed = time.monotonic() - self._flash_started
        span = FLASH_MS / 1000.0
        if elapsed >= span:
            return 0.0
        # Cubic ease-out: most of the movement happens immediately, which is
        # what makes it read as a response to the hotkey rather than as a
        # thing that started animating on its own.
        return (1.0 - elapsed / span) ** 3

    def _pulse(self) -> float:
        """The resting breath on the caret bar, PULSE_MIN_ALPHA..PULSE_MAX_ALPHA."""
        phase = (time.monotonic() - self._shown_at) * 2 * math.pi / PULSE_PERIOD
        eased = (math.sin(phase) + 1.0) / 2.0
        return PULSE_MIN_ALPHA + eased * (PULSE_MAX_ALPHA - PULSE_MIN_ALPHA)

    # -- painting -----------------------------------------------------------

    def _color(self, alpha: float) -> QColor:
        base = QColor(LOST_COLOR) if self._is_lost() else self._accent
        out = QColor(base)
        out.setAlpha(max(0, min(255, int(round(alpha)))))
        return out

    def _is_lost(self) -> bool:
        return self._target is not None and self._target.source == "lost"

    def paintEvent(self, event):
        if self._target is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        emphasis = self._emphasis()

        target = self._target
        if target.field is not None:
            self._paint_field(painter, self._to_local(target.field), emphasis)
        elif target.window is not None and target.caret is None:
            self._paint_window(painter, self._to_local(target.window), emphasis)

        if target.caret is not None and self._caret_visible:
            self._paint_caret(painter, self._to_local(target.caret), emphasis)

    def _paint_field(self, painter: QPainter, rect: QRectF, emphasis: float):
        """The outline around the control the text is going into."""
        # The halo: one rounded rectangle travelling outwards as it fades. It
        # exists only during the flash, and it is what makes the eye land on
        # the target instead of having to find the outline once it is already
        # there.
        if emphasis > 0.01:
            spread = FLASH_HALO_PX * (1.0 - emphasis)
            halo = rect.adjusted(-spread, -spread, spread, spread)
            pen = QPen(self._color(150 * emphasis))
            pen.setWidthF(1.0 + 2.0 * emphasis)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(halo, FIELD_RADIUS + spread,
                                    FIELD_RADIUS + spread)

        alpha = FIELD_RESTING_ALPHA + (255 - FIELD_RESTING_ALPHA) * emphasis
        pen = QPen(self._color(alpha))
        pen.setWidthF(1.5 + 1.5 * emphasis)
        if self._is_lost():
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        # Inset by half the pen so the stroke sits inside the field's own
        # edge rather than straddling it, which on a bordered control reads
        # as the border having thickened rather than as a mark of ours.
        inset = pen.widthF() / 2.0
        painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset),
                                FIELD_RADIUS, FIELD_RADIUS)

    def _paint_window(self, painter: QPainter, rect: QRectF, emphasis: float):
        """The fallback: a whole window, named, when the caret could not be found.

        Dashed rather than solid, and captioned. Both say the same thing --
        this is the window, not the spot -- and saying it is the point:
        the user can see that WhisperType does not know exactly where the
        text will land, and check for themselves before speaking.
        """
        alpha = FIELD_RESTING_ALPHA + (255 - FIELD_RESTING_ALPHA) * emphasis
        pen = QPen(self._color(alpha))
        pen.setWidthF(2.0 + 2.0 * emphasis)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        inset = pen.widthF() / 2.0
        painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset),
                                FIELD_RADIUS, FIELD_RADIUS)
        self._paint_label(painter, rect)

    def _paint_label(self, painter: QPainter, rect: QRectF):
        """A pill naming the target, tucked inside the top-left of the outline."""
        text = self._target.label.strip() if self._target else ""
        if not text:
            text = "Unknown window"
        if len(text) > 64:
            text = text[:61] + "…"

        font = QFont(self.font())
        font.setPixelSize(12)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        pad_x, pad_y = 9.0, 5.0
        width = metrics.horizontalAdvance(text) + 2 * pad_x
        height = metrics.height() + 2 * pad_y
        pill = QRectF(rect.left() + 10, rect.top() + 10, width, height)
        # A window can be smaller than its own caption, and a pill hanging
        # out of the outline it belongs to looks like a second thing.
        if pill.right() > rect.right() - 10:
            pill.setRight(max(rect.left() + 10, rect.right() - 10))

        path = QPainterPath()
        path.addRoundedRect(pill, height / 2.0, height / 2.0)
        painter.fillPath(path, self._color(235))
        painter.setPen(QPen(self._readable_ink()))
        painter.drawText(pill.adjusted(pad_x, 0, -pad_x, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, text)

    def _readable_ink(self) -> QColor:
        """Black or white caption, whichever the pill's fill can carry."""
        base = QColor(LOST_COLOR) if self._is_lost() else self._accent
        return QColor("#101216") if base.lightness() > 145 else QColor("#ffffff")

    def _paint_caret(self, painter: QPainter, rect: QRectF, emphasis: float):
        """The insertion point: a bar with a cap top and bottom, and a glow.

        Drawn at the caret's left edge, which is where the next character
        goes. The caps are what tell it apart from the application's own
        caret blinking in the same place -- without them, in a text field
        that already has a caret, the marker is invisible for being exactly
        what is already there.
        """
        alpha_scale = 1.0 if self._is_lost() else self._pulse()
        # The flash lifts it to full whatever the breath is doing, so the
        # opening beat is never caught at the bottom of a fade.
        alpha_scale = max(alpha_scale, emphasis)

        x = rect.left()
        top = rect.top()
        bottom = rect.bottom()
        half = CARET_WIDTH / 2.0

        # The glow, outward-in: widest and faintest first, so the strokes
        # accumulate towards the centre.
        if not self._is_lost():
            for step in range(GLOW_STEPS, 0, -1):
                fraction = step / GLOW_STEPS
                pen = QPen(self._color(38 * alpha_scale * (1.0 - fraction) + 10))
                pen.setWidthF(CARET_WIDTH + GLOW_SPREAD * fraction
                              * (1.0 + emphasis))
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QPointF(x, top), QPointF(x, bottom))

        body = self._color(255 * alpha_scale)
        painter.setPen(Qt.NoPen)
        painter.setBrush(body)
        painter.drawRoundedRect(
            QRectF(x - half, top, CARET_WIDTH, bottom - top), half, half)

        cap_half = CARET_CAP_WIDTH / 2.0
        for cap_y in (top, bottom - CARET_CAP_HEIGHT):
            painter.drawRoundedRect(
                QRectF(x - cap_half, cap_y, CARET_CAP_WIDTH, CARET_CAP_HEIGHT),
                CARET_CAP_HEIGHT / 2.0, CARET_CAP_HEIGHT / 2.0)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)
