"""The app wordmark, shared by the system-tray icon and every window's icon.

The mark is a bold "W" filled as a vector path over a coloured disc. Kept in
one place so the Settings window's title-bar icon and the tray icon are
literally the same drawing; only the disc colour differs, and only the tray
varies it (one colour per status -- see TRAY_STATE_COLORS in main.py).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap, QTransform,
)

# "W" reads cleanly at the ~16px Windows renders the tray at; "WL" is legible
# from about 24px up and turns to mush below it.
WORDMARK_LETTER = "W"

# Windows asks for the icon at a handful of sizes depending on DPI and taskbar
# settings. Rendering each one rather than downscaling a single large bitmap
# keeps the letterform crisp -- downscaled type blurs badly.
ICON_SIZES = (16, 20, 24, 32, 48, 64)

# The disc colour for anything that is not status-coloured: the window icons.
# Matches the tray's "ready" green so the app reads as one identity.
BRAND_COLOR = "#2ecc71"


def render_wordmark(size: int, disc_color: str) -> QPixmap:
    """The wordmark at one pixel size, on a disc of ``disc_color``."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    inset = max(1, round(size * 0.03))
    diameter = size - 2 * inset
    painter.setBrush(QColor(disc_color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(inset, inset, diameter, diameter)

    # The wordmark is filled as a vector path rather than drawn as text. Qt's
    # Windows font engine applies ClearType subpixel antialiasing whatever the
    # style strategy or paint device, which bakes coloured RGB fringes into the
    # glyph edges -- visible once the icon is composited over the taskbar. Path
    # filling uses the plain antialiasing rasteriser, so the edges stay neutral.
    font = QFont("Segoe UI")
    font.setBold(True)
    font.setPixelSize(100)  # reference size; scaled to fit below
    path = QPainterPath()
    path.addText(0, 0, font, WORDMARK_LETTER)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        painter.end()
        return pixmap

    # Scale to fit the disc, then centre on the glyph's own ink rather than the
    # font's line box -- capitals sit high in the line box and would look
    # bottom-heavy inside a circle.
    scale = min(diameter * 0.74 / bounds.width(),
                diameter * 0.56 / bounds.height())
    transform = QTransform()
    transform.translate(size / 2, size / 2)
    transform.scale(scale, scale)
    transform.translate(-bounds.center().x(), -bounds.center().y())

    painter.fillPath(transform.map(path), QColor("#ffffff"))
    painter.end()
    return pixmap


def wordmark_icon(disc_color: str = BRAND_COLOR) -> QIcon:
    """A multi-size :class:`QIcon` of the wordmark on a disc of ``disc_color``."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(render_wordmark(size, disc_color))
    return icon
