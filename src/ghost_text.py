"""Show the words where they are going to be, while they are still provisional.

The target marker says *where* the transcript will land. This says *what* will
land there, in place, as it is being spoken -- the words appear at the caret,
in the document's own font, greyed to say they are not committed yet.

It is a drawing, not an edit. Nothing is typed into the target application:
this is a click-through window standing over it with text painted on. That is
the whole reason it is built this way. The obvious alternative -- actually
typing the interim transcript and backspacing over it as the server revises
its guess -- puts real text in someone's document, one undo step per revision,
and a stray Backspace in a window whose focus is not in a text field is the
browser's Back button. A drawing cannot do any of that. Cancel a capture and
the window hides; crash mid-capture and the document was never touched.

The cost of the illusion is that it is one. Existing text to the right of the
caret is covered rather than pushed along, wrapping is this module's guess at
the target's rather than the target's own, and a document that scrolls while
the capture is running leaves the drawing behind. Those are all survivable for
something that is on screen for a few seconds and then replaced by the real
paste; text silently entering a document is not.

Three things make it read as belonging to the document underneath:

  * The font. UI Automation reports the family and point size at the caret --
    Consolas 11 in Notepad, Segoe UI 11.5 in Obsidian -- so the ghost is set
    in the document's face, not in ours.
  * The line height. The caret rectangle *is* the line box, so its height is
    the exact leading to advance by. This matters more than the font size for
    looking right: text on the wrong baseline grid reads as pasted on top.
  * The background. Sampled from the screen at the caret's own line, so the
    plate behind the ghost is the document's paper colour whatever theme the
    application is wearing.
"""

import ctypes
import sys

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QSize
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
)
from PySide6.QtWidgets import QApplication, QWidget

from target_overlay import apply_mapping, mapping_for, WDA_EXCLUDEFROMCAPTURE

# How solid the provisional text is drawn. Low enough to read as "not yet
# real", high enough to actually read. The words are the point.
GHOST_ALPHA = 0.62

# Fallback glyph size as a fraction of the line box, for a target that reports
# no font size. A line box is typically a little over 1.3x the type size, and
# erring small keeps the ghost inside its line rather than clipping.
GLYPH_TO_LINE = 0.62

# Inset from the field's edges for wrapping, in logical pixels. Text controls
# have internal padding that UI Automation does not report, so wrapping
# exactly at the reported edge runs the ghost under the control's border.
WRAP_INSET = 6

# Where a continuation line starts when there is no field rectangle to align
# to -- the ghost falls back to the caret's own column.
FALLBACK_WRAP_WIDTH = 520
FALLBACK_LINES = 6

# How far into a field the caret may sit and still be read as being at the
# line's start, in logical pixels. See _continuation_x. Roughly one indent:
# far enough to allow for a control's own padding, near enough that a caret
# genuinely mid-sentence is not mistaken for a margin.
MARGIN_PROBE_PX = 48

# The strip of screen sampled for the background colour: from just right of
# the caret, this many logical pixels across. Right of the caret because that
# is where the ghost is about to be drawn, and on an empty line it is clean
# paper -- exactly the colour wanted.
SAMPLE_WIDTH = 160

# Sampling quantises to this many levels per channel before counting, so that
# a subtly dithered or gradient background still resolves to one dominant
# colour instead of a thousand near-identical ones.
SAMPLE_QUANTISE = 8

DEFAULT_BACKGROUND = "#1e1e1e"


class GhostText(QWidget):
    """Provisional transcript, painted at the caret in the document's own style."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("WhisperType Ghost")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)

        self._target = None
        self._text = ""
        self._map = (0, 0, 0, 0, 1.0)
        self._origin = QPoint(0, 0)
        self._font = QFont()
        self._line_height = 0.0
        self._background = QColor(DEFAULT_BACKGROUND)
        self._ink = QColor("#ffffff")
        # Where the first line starts and where later ones wrap to, in this
        # widget's own coordinates. Fixed for the capture, so the ghost does
        # not shuffle sideways as it grows.
        self._start_x = 0.0
        self._wrap_left = 0.0
        self._wrap_right = 0.0

    # -- public API ---------------------------------------------------------

    def can_show(self, target) -> bool:
        """True if `target` is precise enough to draw provisional text into.

        A caret and nothing less. Painting words over the middle of a window
        that merely has focus would be a guess presented as a fact, and the
        dashed window outline the marker already draws is the honest way to
        say that much.
        """
        return target is not None and target.source == "caret" and bool(target.caret)

    def show_for(self, target) -> bool:
        """Open the ghost layer over `target`. False if it could not be set up."""
        if not self.can_show(target):
            self.dismiss()
            return False
        self._target = target
        self._text = ""
        if not self._lay_out():
            self.dismiss()
            return False
        # Before show(), so the sample is the document rather than the ghost's
        # own previous frame.
        self._sample_background()
        self.show()
        self._exclude_from_capture()
        self.update()
        return True

    def set_text(self, text: str):
        """Update the provisional words. Empty hides nothing; it just clears."""
        text = (text or "").strip()
        if text == self._text:
            return
        self._text = text
        if self.isVisible():
            self.update()

    def retarget(self, target):
        """Follow the target if it has moved, keeping the words already shown.

        The marker is refreshed several times a second while a capture runs,
        mostly so a window being dragged takes its marker with it. The ghost
        has to make the same move or the two come apart. The background is not
        re-sampled: it is the document's paper colour, which dragging a window
        does not change, and sampling is the one expensive thing here.
        """
        if not self.isVisible() or self._target is None:
            return
        if not self.can_show(target):
            self.dismiss()
            return
        if target.caret == self._target.caret and target.field == self._target.field:
            return
        self._target = target
        if not self._lay_out():
            self.dismiss()
            return
        self.update()

    def dismiss(self):
        self._target = None
        self._text = ""
        if self.isVisible():
            self.hide()

    # -- setup --------------------------------------------------------------

    def _lay_out(self) -> bool:
        """Size the window to the field and work out the type and the margins."""
        caret = self._target.caret
        field = self._target.field
        window = self._target.window

        self._map = mapping_for((caret[0], caret[1]))
        caret_box = apply_mapping(self._map, caret)
        self._line_height = max(1.0, caret_box.height())

        # The area the ghost may use: the field it is in, or a band beside the
        # caret when the control did not report one.
        if field is not None:
            area = apply_mapping(self._map, field)
        else:
            area = QRectF(caret_box.left(), caret_box.top(),
                          FALLBACK_WRAP_WIDTH,
                          self._line_height * FALLBACK_LINES)
        # Never above the caret's own line: the ghost is what comes after the
        # insertion point, so the lines before it are not its to draw on.
        if area.top() < caret_box.top():
            area.setTop(caret_box.top())
        if area.bottom() <= area.top():
            area.setBottom(area.top() + self._line_height)

        screen = (QGuiApplication.screenAt(area.center().toPoint())
                  or QApplication.primaryScreen())
        if screen is not None:
            area = area.intersected(QRectF(screen.geometry()))
        if area.isEmpty():
            return False

        geometry = area.toAlignedRect()
        self._origin = geometry.topLeft()
        self.setGeometry(geometry)

        self._font = self._document_font(window)
        self._start_x = caret_box.left() - self._origin.x()
        self._wrap_left = self._continuation_x(caret_box, field is not None)
        self._wrap_right = max(self._wrap_left + 1.0,
                               geometry.width() - WRAP_INSET)
        return True

    def _continuation_x(self, caret_box: QRectF, has_field: bool) -> float:
        """Where a wrapped line starts, in widget coordinates.

        The target's real text margin is not reported by anything, and
        wrapping to the field's own edge lands a few pixels left of where the
        application would have wrapped -- close enough to look wrong, being
        visibly out of line with the row above it.

        The caret rescues it in the case that matters. Dictation almost always
        starts at an empty line or the end of one, and a caret sitting near the
        field's left edge *is* the text margin, measured rather than guessed.
        Only when the caret is well into the line -- inserting mid-sentence,
        where there is no way to know the margin -- does this fall back to the
        field's edge.
        """
        start = caret_box.left() - self._origin.x()
        if not has_field:
            return start
        if start <= MARGIN_PROBE_PX:
            return start
        return WRAP_INSET

    def _document_font(self, _window) -> QFont:
        """The target's own face at the caret, or a fallback fitted to the line."""
        family, points = (self._target.style or (None, None))
        font = QFont()
        if family:
            font.setFamily(family)
        if points:
            font.setPointSizeF(float(points))
        else:
            # No reported size: fit the glyphs to the line box instead, which
            # is the one measurement always available.
            font.setPixelSize(max(6, int(round(self._line_height * GLYPH_TO_LINE))))
        return font

    def _sample_background(self):
        """Read the document's paper colour off the screen at the caret's line."""
        self._background = QColor(DEFAULT_BACKGROUND)
        caret = self._target.caret if self._target else None
        if not caret:
            return
        # In physical pixels, since that is what the grab is measured in.
        ratio = self._map[4] or 1.0
        width = int(SAMPLE_WIDTH * ratio)
        strip = QRect(caret[0] + caret[2], caret[1], width, max(1, caret[3]))

        screen = (QGuiApplication.screenAt(self._origin)
                  or QApplication.primaryScreen())
        if screen is None:
            return
        try:
            shot = screen.grabWindow(0, strip.x(), strip.y(),
                                     strip.width(), strip.height())
        except Exception:
            return
        if shot.isNull():
            return
        image = shot.toImage()
        if image.isNull() or image.width() == 0:
            return

        # The most common colour wins. Quantised first so a gradient or a
        # dithered surface still lands on one answer.
        counts = {}
        step = max(1, image.width() // 60)
        for y in range(0, image.height(), max(1, image.height() // 6)):
            for x in range(0, image.width(), step):
                pixel = image.pixelColor(x, y)
                key = (pixel.red() // SAMPLE_QUANTISE,
                       pixel.green() // SAMPLE_QUANTISE,
                       pixel.blue() // SAMPLE_QUANTISE)
                entry = counts.get(key)
                if entry is None:
                    counts[key] = [1, pixel]
                else:
                    entry[0] += 1
        if not counts:
            return
        self._background = max(counts.values(), key=lambda item: item[0])[1]
        # Ink that the paper can carry, rather than the document's real text
        # colour -- which is not reported, and which a wrong guess at would
        # make the ghost unreadable exactly where it matters.
        self._ink = (QColor("#101216") if self._background.lightness() > 140
                     else QColor("#ffffff"))

    def _exclude_from_capture(self):
        """Keep the ghost out of the Capture Box's frost snapshot."""
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(int(self.winId())),
                ctypes.c_uint(WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            pass

    # -- layout of the words themselves -------------------------------------

    def _lines(self, metrics: QFontMetricsF):
        """Wrap the provisional text into (x, y, string) rows.

        The first row begins at the caret and the rest at the field's margin,
        which is what makes the ghost read as a continuation of the line
        already there rather than as a block dropped beside it.
        """
        rows = []
        x = self._start_x
        current = ""
        for word in self._text.split():
            candidate = f"{current} {word}" if current else word
            if x + metrics.horizontalAdvance(candidate) <= self._wrap_right:
                current = candidate
                continue
            if current:
                rows.append((x, current))
            x = self._wrap_left
            current = word
            # A single word too long for a whole line: let it overrun rather
            # than splitting it, since it is about to be replaced anyway.
        if current:
            rows.append((x, current))

        # More rows than the field has room for: keep the tail. While someone
        # is speaking, the words that matter are the ones just said.
        capacity = max(1, int(self.height() // self._line_height))
        if len(rows) > capacity:
            rows = rows[-capacity:]
            # The first surviving row is no longer the caret's row, so it
            # starts at the margin like every other continuation.
            rows[0] = (self._wrap_left, rows[0][1])
        return [(x, index * self._line_height, text)
                for index, (x, text) in enumerate(rows)]

    def paintEvent(self, event):
        if not self._text or self._target is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self._font)
        metrics = QFontMetricsF(self._font)

        ink = QColor(self._ink)
        ink.setAlphaF(GHOST_ALPHA)

        for x, y, text in self._lines(metrics):
            width = metrics.horizontalAdvance(text)
            # The plate: the document's own paper, so whatever the ghost is
            # standing over stops competing with it. Squared off and exactly
            # one line tall, so consecutive rows join into one block.
            painter.fillRect(QRectF(x, y, width, self._line_height),
                             self._background)
            painter.setPen(ink)
            painter.drawText(QRectF(x, y, width, self._line_height),
                             Qt.AlignVCenter | Qt.AlignLeft, text)

    def sizeHint(self) -> QSize:
        return QSize(400, 100)
