"""Input-level meters for the Capture Box.

One widget, several looks, all fed by one signal chain:

    chunk dBFS -> median de-spike -> auto-ranging -> ballistics -> style

The chain is where the work is. Raw chunk levels are far too jumpy to look at
directly, and a fixed dBFS scale is wrong for a dictation box: every user's mic
gain, room and speaking distance are different, so a scale calibrated for one
of them reads as either dead or pinned for the next. LevelFollower solves both
by tracking the room's own noise floor and the speaker's own recent peaks and
normalising between them -- silence is 0.0 and your loudest recent speech is
1.0, whoever you are and however your gain is set.

Adding a style means writing a `_paint_<name>` method, listing its name in
STYLES, giving it a size in STYLE_SIZES, and a label in STYLE_LABELS for the
Settings dropdown; everything else is shared.
"""

import math
import time
from collections import deque

from PySide6.QtWidgets import QWidget, QSizePolicy
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QImage,
    QPainterPath,
    QLinearGradient,
)

# Style names, in the order the Settings dropdown lists them. The first is the
# default.
STYLES = ("waveform", "mirror", "bars", "mirror_bars", "arc", "circle")

# What each style is called in the Settings dropdown. The internal name is a
# storage key, saved in every profile, so it can't just be prettied up in
# place the way the model list's labels can -- a display string here is
# derived from it instead, never stored back over it.
STYLE_LABELS = {
    "waveform": "Waveform",
    "mirror": "Mirror waveform",
    "bars": "Bars",
    "mirror_bars": "Mirror bars",
    "arc": "Arc",
    "circle": "Circle",
}

# Each style gets the room it needs rather than a shared box: a scrolling
# waveform is unreadable squeezed into a square, and a ring padded out to
# waveform width would float in a sea of empty space.
STYLE_SIZES = {
    "waveform": (168, 26),
    "mirror": (168, 26),
    "bars": (168, 26),
    "mirror_bars": (168, 26),
    "arc": (52, 28),
    "circle": (26, 26),
}

# How much of each end of the widget fades out, as a fraction of its width.
# Only the scrolling styles: they run edge to edge, and a hard stop makes the
# wave look cropped rather than continuing past the frame. The ring and the
# gauge are closed shapes, and feathering those would just eat their sides.
FEATHER = {"waveform": 0.16, "mirror": 0.14}

# Redraw interval. The audio thread only reports a level about 16 times a
# second (one per 1024-frame chunk), which reads as a stutter on its own; the
# widget redraws faster than that and eases between readings.
FRAME_MS = 16

# The one colour the meter draws itself in. A module default rather than a
# constant: every style below asks the instance for it, so a box can be given
# an accent of its own without four painters having to be told about it.
DEFAULT_ACCENT = "#4fa2f0"
ACCENT = QColor(DEFAULT_ACCENT)


# -- signal chain ---------------------------------------------------------

# Ballistics, as first-order time constants in seconds. Attack is the VU
# standard (99% of full deflection in 300ms, which is 4.6 time constants);
# release is slower, in the spirit of a PPM's slow fall, because a meter that
# falls as fast as it rises flickers between syllables instead of showing the
# shape of a phrase.
ATTACK_TAU = 0.065
RELEASE_TAU = 0.35

# The scrolling styles run a second, quicker release off the same input. A
# 350ms fall is right for a meter you read at a glance, but it is longer than
# a syllable, so a waveform drawn from it smooths speech into one long blob.
# The de-spiking and the attack are shared, so this is a shape with detail in
# it, not a noisier signal.
DETAIL_RELEASE_TAU = 0.11

# Peak-hold fall, in units of full scale per second -- slow enough to read as
# a held mark rather than as a second, laggier meter.
PEAK_FALL_PER_S = 0.55

# Auto-ranging. The floor drops onto quiet quickly but may only climb at a
# crawl, so it settles on the room's noise and never chases speech up; the
# ceiling snaps up to a new peak and then bleeds down, so a stretch of quieter
# talking becomes the new full scale after a few seconds.
FLOOR_FALL_TAU = 0.6
FLOOR_RISE_DB_PER_S = 1.5
CEILING_ATTACK_TAU = 0.05
CEILING_FALL_DB_PER_S = 4.0

# Guard rail on the auto-range. Without a minimum span the ceiling would
# collapse onto the floor during a long silence and the meter would then
# amplify room noise to full scale -- the classic auto-gain failure, and a
# deeply confusing one to look at.
MIN_SPAN_DB = 20.0

# Dead band above the tracked floor. The noise floor itself should read as
# nothing at all, so the scale starts just above it.
NOISE_MARGIN_DB = 4.0

# A level this small is indistinguishable from silence on screen; below it the
# animation timer stops rather than repainting 60 times a second at rest.
REST_LEVEL = 0.004


class LevelFollower:
    """Turns a stream of raw chunk dBFS readings into a smooth 0-1 level.

    Kept out of the widget so the tuning can be tested without a UI, and so
    every style is guaranteed to be showing exactly the same signal.
    """

    def __init__(self):
        self._history = deque(maxlen=3)
        self._floor_db = None
        self._ceiling_db = None
        self._target = 0.0
        self._last_push = None
        self.level = 0.0
        self.detail = 0.0
        self.peak = 0.0

    def reset(self):
        self._history.clear()
        self._floor_db = None
        self._ceiling_db = None
        self._target = 0.0
        self._last_push = None
        self.level = 0.0
        self.detail = 0.0
        self.peak = 0.0

    def push(self, db: float, dt: float = None):
        """Feed one chunk's dBFS reading, at whatever rate they arrive."""
        if dt is None:
            now = time.monotonic()
            # Measured rather than assumed: chunk size and sample rate are
            # both configurable, and a dropped buffer stretches the gap.
            # Clamped so a stall (a slow first chunk, a paused debugger)
            # cannot fast-forward the auto-range through a huge interval.
            dt = 0.064 if self._last_push is None else min(0.25, max(0.005, now - self._last_push))
            self._last_push = now

        # Median of the last three readings: a single-chunk spike -- a key
        # click, a bumped desk -- is rejected outright, where an average would
        # smear it across the next few frames instead.
        self._history.append(db)
        db = sorted(self._history)[len(self._history) // 2]

        if self._floor_db is None:
            self._floor_db = db
            self._ceiling_db = db + MIN_SPAN_DB

        if db < self._floor_db:
            self._floor_db += (db - self._floor_db) * (1.0 - math.exp(-dt / FLOOR_FALL_TAU))
        else:
            self._floor_db = min(db, self._floor_db + FLOOR_RISE_DB_PER_S * dt)

        if db > self._ceiling_db:
            self._ceiling_db += (db - self._ceiling_db) * (1.0 - math.exp(-dt / CEILING_ATTACK_TAU))
        else:
            self._ceiling_db -= CEILING_FALL_DB_PER_S * dt

        self._ceiling_db = max(self._ceiling_db, self._floor_db + MIN_SPAN_DB)

        base = self._floor_db + NOISE_MARGIN_DB
        span = max(1.0, self._ceiling_db - base)
        self._target = max(0.0, min(1.0, (db - base) / span))

    def advance(self, dt: float):
        """Step the ballistics forward by `dt` seconds. Call once per frame."""
        tau = ATTACK_TAU if self._target > self.level else RELEASE_TAU
        self.level += (self._target - self.level) * (1.0 - math.exp(-dt / tau))

        tau = ATTACK_TAU if self._target > self.detail else DETAIL_RELEASE_TAU
        self.detail += (self._target - self.detail) * (1.0 - math.exp(-dt / tau))

        if self._target < REST_LEVEL:
            if self.level < REST_LEVEL:
                self.level = 0.0
            if self.detail < REST_LEVEL:
                self.detail = 0.0
        self.peak = max(self.level, self.peak - PEAK_FALL_PER_S * dt)

    def at_rest(self) -> bool:
        return (self._target < REST_LEVEL and self.level <= 0.0
                and self.detail <= 0.0 and self.peak < REST_LEVEL)


def smooth_path(points) -> QPainterPath:
    """A Catmull-Rom spline through `points`, as cubic Bezier segments.

    Straight segments between level samples show every sample as a corner,
    which is what makes a naive waveform look like a heart monitor.
    """
    path = QPainterPath()
    if not points:
        return path
    path.moveTo(points[0])
    count = len(points)
    for i in range(count - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < count else p2
        c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0, p1.y() + (p2.y() - p0.y()) / 6.0)
        c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0, p2.y() - (p3.y() - p1.y()) / 6.0)
        path.cubicTo(c1, c2, p2)
    return path


# -- widget ---------------------------------------------------------------

# How often a level lands in the scrolling history, and how many points the
# history holds. 45ms is a shade under three frames: fast enough that a
# syllable leaves its own bump, slow enough that about two seconds of speech
# fits across the widget.
HISTORY_MS = 45
HISTORY_POINTS = 44
BAR_COUNT = 24
# Bars per side in Mirror bars -- half of BAR_COUNT, so the two styles read
# as the same density of bars rather than one looking sparser than the other.
MIRROR_BAR_COUNT = BAR_COUNT // 2

# Points per arm of the mirrored style. Half the width, so roughly half the
# history of the scrolling one -- about a second and a quarter either side.
MIRROR_POINTS = 26

# Fraction of the half-height a full-scale level draws to. The auto-range
# pins recent peaks at 1.0, so without headroom the loudest speech would sit
# flat against the widget edge -- the shape reads as clipped even though the
# audio is not.
HEADROOM = 0.86


class SignalMeter(QWidget):
    """A live input-level indicator, drawn in one of several styles.

    Not clickable: a press on it falls through to the window it sits in, which
    for the Capture Box is how the box is dragged. The style is chosen in
    Settings.
    """

    def __init__(self, parent=None, style: str = STYLES[0]):
        super().__init__(parent)
        self._style = style if style in STYLES else STYLES[0]
        self._follower = LevelFollower()
        self._history = deque([0.0] * HISTORY_POINTS, maxlen=HISTORY_POINTS)
        self._since_sample = 0.0
        self._last_frame = None

        self._apply_style_size()
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # The Capture Box sets a window-wide stylesheet (dark fill, rounded
        # corners) that every child inherits. This widget paints itself
        # entirely, so it opts out rather than drawing on top of a stray box.
        self.setStyleSheet("background: transparent; border: none;")
        self._accent = QColor(ACCENT)

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._advance)

    # -- state ------------------------------------------------------------

    def set_level_db(self, db: float):
        """Feed a raw chunk level in dBFS, straight from the audio thread."""
        # A hidden meter drops the sample rather than animating out of sight:
        # the level signal is broadcast to every meter in the app, and the
        # Settings window's preview would otherwise repaint at 60fps behind a
        # closed window for the length of every capture.
        if not self.isVisible():
            return
        try:
            db = float(db)
        except (TypeError, ValueError):
            return
        self._follower.push(db)
        if not self._timer.isActive():
            self._last_frame = None
            self._timer.start()

    def reset(self):
        """Drop to silence immediately, for the start or end of a capture."""
        self._follower.reset()
        self._history = deque([0.0] * HISTORY_POINTS, maxlen=HISTORY_POINTS)
        self._timer.stop()
        self._last_frame = None
        self.update()

    def set_accent_color(self, color: str):
        """The colour every style paints itself in. Invalid input is ignored.

        A bad value here would repaint the meter in Qt's default black on a
        dark box, which is indistinguishable from a meter that has stopped
        working -- so the last good colour is kept instead.
        """
        chosen = QColor(color)
        if not chosen.isValid() or chosen == self._accent:
            return
        self._accent = chosen
        self.update()

    def style_name(self) -> str:
        return self._style

    def set_style(self, style: str):
        if style not in STYLES or style == self._style:
            return
        self._style = style
        self._apply_style_size()
        self.update()

    def _apply_style_size(self):
        self.setFixedSize(*STYLE_SIZES[self._style])

    def _advance(self):
        now = time.monotonic()
        # Wall clock rather than the timer's nominal interval: Qt coalesces
        # timer events under load, and ballistics driven by an assumed 16ms
        # would quietly run slow exactly when the UI is busiest.
        dt = 0.016 if self._last_frame is None else min(0.1, now - self._last_frame)
        self._last_frame = now

        self._follower.advance(dt)

        self._since_sample += dt
        while self._since_sample >= HISTORY_MS / 1000.0:
            self._since_sample -= HISTORY_MS / 1000.0
            self._history.append(self._follower.detail)

        # Keep scrolling until the last bump has left the display, so the wave
        # never freezes mid-shape; only then stop repainting.
        if self._follower.at_rest() and not any(v > REST_LEVEL for v in self._history):
            self._timer.stop()
        self.update()

    # -- events -----------------------------------------------------------

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        feather = FEATHER.get(self._style, 0.0)
        if feather <= 0.0:
            getattr(self, f"_paint_{self._style}")(painter, self._follower.level)
            painter.end()
            return

        # Painted into a layer first so the fade can be applied as an alpha
        # mask over the finished drawing. Fading each element as it is drawn
        # would mean every style having to know about the edges, and would
        # still come out wrong wherever two of them overlap.
        ratio = self.devicePixelRatioF()
        layer = QImage(
            int(self.width() * ratio), int(self.height() * ratio),
            QImage.Format_ARGB32_Premultiplied,
        )
        layer.setDevicePixelRatio(ratio)
        layer.fill(Qt.transparent)

        inner = QPainter(layer)
        inner.setRenderHint(QPainter.Antialiasing, True)
        getattr(self, f"_paint_{self._style}")(inner, self._follower.level)

        # DestinationIn multiplies what is already there by the alpha of what
        # is drawn over it, so a gradient of pure alpha becomes a mask.
        inner.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        mask = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
        for stop, alpha in (
            (0.0, 0.0),
            (feather * 0.45, 0.55),
            (feather, 1.0),
            (1.0 - feather, 1.0),
            (1.0 - feather * 0.45, 0.55),
            (1.0, 0.0),
        ):
            step = QColor(0, 0, 0)
            step.setAlphaF(alpha)
            mask.setColorAt(stop, step)
        inner.fillRect(self.rect(), QBrush(mask))
        inner.end()

        painter.drawImage(0, 0, layer)
        painter.end()

    # -- styles -----------------------------------------------------------

    def _paint_waveform(self, painter: QPainter, level: float):
        """A hairline at rest that swells into a scrolling waveform on speech.

        The envelope is mirrored about the centre line and filled with a
        left-to-right fade, so the wave appears to emerge at the right where
        the sound is happening and dissolve as it ages -- the fade carries the
        direction of time without needing a scale, a scrollbar or a label.
        """
        width = self.width()
        mid = self.height() / 2.0
        amplitude = (mid - 1.5) * HEADROOM
        step = (width - 3.0) / (HISTORY_POINTS - 1)
        values = list(self._history)

        self._draw_rest_line(painter, mid)
        if not any(v > REST_LEVEL for v in values):
            return

        top = [QPointF(1.5 + i * step, mid - v * amplitude) for i, v in enumerate(values)]
        self._draw_envelope(
            painter, top, mid,
            fill=((0.0, 0.0), (0.55, 0.22), (1.0, 0.42)),
            edge=((0.0, 0.10), (1.0, 1.0)),
        )

    def _paint_mirror(self, painter: QPainter, level: float):
        """The same waveform, mirrored about the vertical axis as well.

        Time runs outward from the centre in both directions instead of left
        to right, so the newest sound opens out of the middle and the older
        audio spreads to both edges. It borrows the symmetry of a mirrored
        spectrum analyser while staying a waveform: the shape is still the
        envelope over time, only folded.

        Fewer points than the scrolling version -- each one is drawn twice, so
        the same history at the same spacing would run off both ends.
        """
        width = self.width()
        mid = self.height() / 2.0
        amplitude = (mid - 1.5) * HEADROOM
        centre = width / 2.0
        step = (width - 3.0) / 2.0 / (MIRROR_POINTS - 1)
        # Newest first: index 0 lands on the centre line and the history walks
        # outward from it.
        outward = list(self._history)[-MIRROR_POINTS:][::-1]

        self._draw_rest_line(painter, mid)
        if not any(v > REST_LEVEL for v in outward):
            return

        right = [QPointF(centre + i * step, mid - v * amplitude)
                 for i, v in enumerate(outward)]
        # The centre point is shared, so the left arm skips it rather than
        # doubling a point on top of itself and kinking the spline there.
        left = [QPointF(2 * centre - p.x(), p.y()) for p in right[1:]]
        top = list(reversed(left)) + right

        self._draw_envelope(
            painter, top, mid,
            fill=((0.0, 0.0), (0.5, 0.42), (1.0, 0.0)),
            edge=((0.0, 0.08), (0.5, 1.0), (1.0, 0.08)),
        )

    def _draw_rest_line(self, painter: QPainter, mid: float):
        """The hairline the wave grows out of.

        Drawn under the wave rather than instead of it, so there is no visible
        switch between the resting and speaking states.
        """
        rest = QColor(self._accent)
        rest.setAlphaF(0.35)
        painter.setPen(QPen(rest, 1.0))
        painter.drawLine(QPointF(1.5, mid), QPointF(self.width() - 1.5, mid))

    def _draw_envelope(self, painter: QPainter, top, mid: float, fill, edge):
        """Fill and stroke `top` and its reflection about `mid`.

        Both waveform styles differ only in where their points are and which
        way the fade runs, so the drawing itself lives here once.
        """
        width = float(self.width())
        bottom = [QPointF(p.x(), 2 * mid - p.y()) for p in reversed(top)]

        body = smooth_path(top)
        # lineTo, not a second moveTo: the two curves have to be one closed
        # subpath or the fill leaks across the shape.
        body.lineTo(bottom[0])
        body.connectPath(smooth_path(bottom))
        body.closeSubpath()

        fade = QLinearGradient(0.0, 0.0, width, 0.0)
        for stop, alpha in fill:
            soft = QColor(self._accent)
            soft.setAlphaF(alpha)
            fade.setColorAt(stop, soft)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fade))
        painter.drawPath(body)

        outline = QLinearGradient(0.0, 0.0, width, 0.0)
        for stop, alpha in edge:
            line = QColor(self._accent)
            line.setAlphaF(alpha)
            outline.setColorAt(stop, line)
        pen = QPen(QBrush(outline), 1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(pen)
        painter.drawPath(smooth_path(top))
        painter.drawPath(smooth_path(bottom))

    def _paint_bars(self, painter: QPainter, level: float):
        """The same history as capsules -- the voice-message look.

        Quantising to bars hides the last of the jitter that survives the
        filtering: a bar either grows or it does not, where a line shows every
        wobble in between.
        """
        width = self.width()
        height = self.height()
        mid = height / 2.0
        slot = width / BAR_COUNT
        bar_width = max(2.0, slot * 0.52)
        values = list(self._history)[-BAR_COUNT:]

        painter.setPen(Qt.NoPen)
        for i, value in enumerate(values):
            # Never zero: at rest the bars settle into an even row of dots,
            # which reads as "listening, nothing yet" rather than as a widget
            # that failed to draw.
            bar_height = max(bar_width, value * (height - 2.0))
            x = i * slot + (slot - bar_width) / 2.0
            rect = QRectF(x, mid - bar_height / 2.0, bar_width, bar_height)

            colour = QColor(self._accent)
            # Older bars sit further back. The ramp is squared so the fade is
            # concentrated at the tail rather than dimming the whole row.
            age = (i + 1) / len(values)
            colour.setAlphaF(0.18 + 0.82 * (age ** 2))
            painter.setBrush(QBrush(colour))
            painter.drawRoundedRect(rect, bar_width / 2.0, bar_width / 2.0)

    def _paint_mirror_bars(self, painter: QPainter, level: float):
        """Bars, folded the way Mirror folds the waveform.

        The newest bar sits on the centre line and pushes a twin out to
        either side of it, so the row grows outward from the middle on each
        syllable instead of scrolling past a fixed right edge.
        """
        width = self.width()
        height = self.height()
        mid_y = height / 2.0
        centre = width / 2.0
        slot = centre / MIRROR_BAR_COUNT
        bar_width = max(2.0, slot * 0.52)
        gap = (slot - bar_width) / 2.0
        # Newest first, walking outward -- the same order _paint_mirror reads
        # its history in.
        outward = list(self._history)[-MIRROR_BAR_COUNT:][::-1]

        painter.setPen(Qt.NoPen)
        for i, value in enumerate(outward):
            bar_height = max(bar_width, value * (height - 2.0))
            y = mid_y - bar_height / 2.0
            colour = QColor(self._accent)
            # Distance from the centre, 0 at the newest bar and 1 at the
            # oldest -- the mirror-image of _paint_bars' left-to-right ramp.
            age = (i + 1) / len(outward)
            colour.setAlphaF(0.18 + 0.82 * (1.0 - age) ** 2)
            painter.setBrush(QBrush(colour))
            x_right = centre + i * slot + gap
            painter.drawRoundedRect(
                QRectF(x_right, y, bar_width, bar_height),
                bar_width / 2.0, bar_width / 2.0)
            x_left = centre - (i + 1) * slot + gap
            painter.drawRoundedRect(
                QRectF(x_left, y, bar_width, bar_height),
                bar_width / 2.0, bar_width / 2.0)

    def _paint_arc(self, painter: QPainter, level: float):
        """A sweep gauge with a peak-hold tick: a VU needle's read, modernised.

        The tick is the point of it. A moving arc alone tells you the level
        right now; the mark it leaves behind tells you how loud you actually
        got, which is the question you are asking when you glance at a meter.
        """
        width = self.width()
        height = self.height()
        stroke = max(2.4, height * 0.10)
        radius = min(width / 2.0 - stroke * 1.6, height - stroke * 2.2)
        centre = QPointF(width / 2.0, height - stroke * 1.2)
        box = QRectF(centre.x() - radius, centre.y() - radius, radius * 2.0, radius * 2.0)

        # Qt angles are in sixteenths of a degree, counter-clockwise from
        # three o'clock; the gauge sweeps the top half from left to right.
        start = 180 * 16
        span = -180 * 16

        track = QColor(self._accent)
        track.setAlphaF(0.18)
        pen = QPen(track, stroke)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(box, start, span)

        if level > REST_LEVEL:
            live = QColor(self._accent)
            live.setAlphaF(0.95)
            pen = QPen(live, stroke)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawArc(box, start, int(span * level))

        if self._follower.peak > REST_LEVEL:
            angle = math.pi * (1.0 - self._follower.peak)
            inner = radius + stroke * 0.6
            outer = radius + stroke * 1.7
            mark = QColor("#ffffff")
            mark.setAlphaF(0.8)
            pen = QPen(mark, max(1.4, stroke * 0.5))
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(centre.x() + math.cos(angle) * inner,
                        centre.y() - math.sin(angle) * inner),
                QPointF(centre.x() + math.cos(angle) * outer,
                        centre.y() - math.sin(angle) * outer),
            )

    def _paint_circle(self, painter: QPainter, level: float):
        """A hollow ring that fills in as the input gets louder.

        The ring itself stays at a constant weight so the meter is visible at
        silence -- an outline that faded with the signal would read as a
        rendering glitch rather than as "no input".
        """
        # Proportional so the ring keeps its weight if the box's type size
        # ever drives the meter's size too.
        stroke = max(1.4, self.width() * 0.07)
        rect = QRectF(self.rect()).adjusted(stroke, stroke, -stroke, -stroke)

        fill = QColor(self._accent)
        # A floor under the alpha keeps the ring from reading as empty glass
        # at rest. No curve on top of it: the level arriving here has already
        # been through a dB scale and the auto-range, so it is perceptual
        # already, and a second gamma would flatten the quiet end twice.
        fill.setAlphaF(0.06 + 0.88 * level)
        painter.setBrush(QBrush(fill))

        ring = QColor(self._accent)
        ring.setAlphaF(0.75)
        painter.setPen(QPen(ring, stroke))
        painter.drawEllipse(rect)
