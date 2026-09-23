import copy
import json
import os
from PySide6.QtWidgets import (
    QWidget,
    QInputDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QMessageBox,
    QKeySequenceEdit,
    QSpinBox,
    QComboBox,
    QColorDialog,
    QFontComboBox,
)
from PySide6.QtCore import Qt, Signal, QPoint, QTimer
from PySide6.QtGui import QKeySequence, QColor, QFont

from branding import wordmark_icon
from signal_meter import SignalMeter, STYLE_LABELS

from capture_box import (
    CaptureBox,
    DEFAULT_FONT_SIZE_PX,
    MIN_FONT_SIZE_PX,
    MAX_FONT_SIZE_PX,
    DEFAULT_FONT_FAMILY,
    DEFAULT_LINE_SPACING,
    MIN_LINE_SPACING,
    MAX_LINE_SPACING,
    DEFAULT_GROW_TO_FIT,
    METER_STYLES,
    DEFAULT_OPACITY,
    MIN_OPACITY,
    MAX_OPACITY,
    DEFAULT_BG_COLOR,
    DEFAULT_FIELD_OPACITY,
    DEFAULT_FIELD_COLOR,
    DEFAULT_TEXT_COLOR,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_PANEL_FROST,
    DEFAULT_FIELD_FROST,
    DEFAULT_BLUR_RADIUS,
    MIN_BLUR_RADIUS,
    MAX_BLUR_RADIUS,
    DEFAULT_SATURATION,
    DEFAULT_BRIGHTNESS,
    DEFAULT_LEVELLING,
)

# What the preview box says. Long enough to wrap onto a second line, so the
# line spacing and the field's height are part of what is being judged, and
# ordinary enough to be read past while looking at the colours.
PREVIEW_TEXT = ("I was thinking that we could probably move the meeting to "
                "Thursday afternoon instead.")

# The preview sits on a dark panel matching the Capture Box, because that is
# where the meter really lives; the same blue on the settings window's own
# background would misrepresent every style.
METER_PREVIEW_BACKGROUND = "#2b2b2b"
METER_PREVIEW_WIDTH = 196

# Models offered in the dropdown, in the order they are shown. A typed model
# name fails silently -- the server rejects it and the capture box simply never
# fills in -- so the common cases are picked from a list instead.
#
# `None` inserts a separator. The first group is English-only and sized for a
# CPU or a modest GPU; the second needs a real GPU to be worth choosing.
#
# Measured on an RTX 3060 Ti: every one of these transcribes faster than
# speech, and time-to-first-word varied by under 0.15s across the whole range
# (it is set by the server's chunk cadence, not the model). What model size
# actually costs is the per-connection load -- 0.4s for tiny.en up to 3.3s for
# large-v3-turbo -- so on a GPU there is little reason to pick a small one.
MODEL_CHOICES = [
    ("distil-small.en", "fast, trained to suppress hallucination"),
    ("distil-medium.en", "more accurate, still quick"),
    ("small.en", "accurate; a little more filler on short clips"),
    ("medium.en", "accurate, heavier"),
    ("base.en", "light"),
    ("tiny.en", "fastest to load, hallucinates on short utterances"),
    None,
    ("large-v3-turbo", "best accuracy for the latency — needs an NVIDIA GPU"),
    ("distil-large-v3", "near-large accuracy, faster"),
    ("large-v3", "most accurate, slowest"),
]

_APP_DATA_ROOT = os.environ.get("APPDATA", os.path.expanduser("~"))
APP_DATA_DIR = os.path.join(_APP_DATA_ROOT, "WhisperType")

# Carry a pre-rename data directory over on first launch under the new name,
# so config, profiles and history survive the rename. One-shot: once the new
# directory exists this does nothing.
_LEGACY_APP_DATA_DIR = os.path.join(_APP_DATA_ROOT, "WhisperBoard")
if not os.path.exists(APP_DATA_DIR) and os.path.isdir(_LEGACY_APP_DATA_DIR):
    try:
        os.rename(_LEGACY_APP_DATA_DIR, APP_DATA_DIR)
    except OSError:
        pass

os.makedirs(APP_DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(APP_DATA_DIR, "config.json")

# Saved settings, by name. One file each rather than a section inside
# config.json: a profile is then something that can be copied to another
# machine, backed up, or deleted with the file manager, and a config that
# fails to parse cannot take the profiles down with it.
PROFILE_DIR = os.path.join(APP_DATA_DIR, "profiles")

# Characters a profile name may not contain, being a filename as well as a
# label. Replaced rather than rejected: someone naming a profile "dark/light"
# means something by it and should not have to be told about paths.
_PROFILE_UNSAFE = '<>:"/\\|?*'


def profile_path(name: str) -> str:
    safe = "".join("-" if c in _PROFILE_UNSAFE else c for c in name).strip()
    return os.path.join(PROFILE_DIR, f"{safe}.json")


# Everything a profile is made of, and nothing else: the Capture Box's
# appearance, from the transcript typeface through to the level meter. A
# profile is about how the box looks, so it deliberately leaves the server
# address, the hotkey and the model alone -- switching from a dark box to a
# light one should not quietly switch machines with it.
#
# The Settings window groups exactly these controls under the profile picker,
# so what is inside that box is what is inside the file. An appearance setting
# added to the group and not added here would be shown as part of a profile
# and then not saved with it.
APPEARANCE_KEYS = (
    "capture_font_size",
    "capture_font_family",
    "capture_line_spacing",
    "capture_grow_to_fit",
    "capture_opacity",
    "capture_bg_color",
    "capture_panel_frost",
    "capture_field_opacity",
    "capture_field_color",
    "capture_field_frost",
    "capture_text_color",
    "capture_accent_color",
    "capture_blur_radius",
    "capture_frost_saturation",
    "capture_frost_brightness",
    "capture_frost_levelling",
    "capture_meter_style",
)


DEFAULT_SETTINGS = {
    "server_address": "ws://localhost:9090",
    "capture_hotkey": "ctrl+`",
    "launch_on_startup": False,
    "model": "distil-small.en",
    "connect_on_demand": True,
    # Mark the place the transcript is going to be pasted into for the length
    # of the capture. See target_overlay.py. Not a profile setting: it is
    # about whether the marker is shown at all, not about how the box looks,
    # so switching appearance profiles must not turn it on or off.
    "capture_show_target": False,
    # Open the Capture Box beside the caret it found rather than beside the
    # mouse. Only possible now that the caret is located at all, and only
    # takes effect when it was found -- see caret_target.py.
    "capture_follow_caret": True,
    # Type the transcript into the target as it is spoken, revising it in
    # place as the server changes its mind -- see live_type.py. Only ever runs
    # for a capture with a confirmed text caret, because the mechanism is
    # Backspace and Backspace outside a text field is the browser's Back
    # button. Slims the Capture Box to its meter and buttons, and stops it
    # taking the keyboard, for as long as it runs.
    "capture_live_typing": True,
    "capture_font_size": DEFAULT_FONT_SIZE_PX,
    # Empty means the system UI font.
    "capture_font_family": DEFAULT_FONT_FAMILY,
    # Space between transcript lines, as a percentage of the face's own line
    # height, and whether a long transcript makes the box taller instead of
    # scrolling inside it.
    "capture_line_spacing": DEFAULT_LINE_SPACING,
    "capture_grow_to_fit": DEFAULT_GROW_TO_FIT,
    # How solid the Capture Box is, and what colour. Stored as a fraction
    # rather than a percentage so it goes straight into the stylesheet.
    "capture_opacity": DEFAULT_OPACITY,
    "capture_bg_color": DEFAULT_BG_COLOR,
    # Which surfaces are frosted, and how the one shared snapshot behind
    # them is processed. See capture_box.py -- in particular for why a panel
    # at 0% opacity still looks grey, which is the levelling below.
    "capture_panel_frost": DEFAULT_PANEL_FROST,
    "capture_field_frost": DEFAULT_FIELD_FROST,
    # Renamed from capture_blur, which held a downscale percentage. A
    # radius in pixels is not the same number, and there is no telling one
    # scale's 60 from the other's, so an old value is left behind rather than
    # reinterpreted into something nobody asked for.
    "capture_blur_radius": DEFAULT_BLUR_RADIUS,
    "capture_frost_saturation": DEFAULT_SATURATION,
    "capture_frost_brightness": DEFAULT_BRIGHTNESS,
    "capture_frost_levelling": DEFAULT_LEVELLING,
    # The inset the transcript sits in, set separately from the panel around
    # it -- see capture_box.py for why the two are not one control.
    "capture_field_opacity": DEFAULT_FIELD_OPACITY,
    "capture_field_color": DEFAULT_FIELD_COLOR,
    # The transcript's ink, and the colour the level meter and the Confirm
    # button are drawn in. See capture_box.py.
    "capture_text_color": DEFAULT_TEXT_COLOR,
    "capture_accent_color": DEFAULT_ACCENT_COLOR,
    "capture_meter_style": METER_STYLES[0],
    # Which profile the appearance in force came from, so closing the app and
    # reopening it comes back to the same one rather than to "(none)" over
    # settings that plainly are a profile. Empty means none.
    "active_profile": "",
    # Server startup. WhisperType launches itself at login but the WhisperLive
    # server does not, which left the app looking ready with nothing to talk
    # to. These let it bring the server up itself -- only ever for a server
    # address on this machine.
    "auto_start_server": True,
    "start_docker_desktop": True,
    "server_use_gpu": False,
    # Load the model once on the server and share it across connections,
    # instead of a fresh one per connection.
    "share_one_model": True,
    # Dial straight back after each capture, rather than waiting for the next
    # one. Cheap when the server shares one model; costly when it does not.
    "reconnect_after_capture": True,
    # Apps to free the GPU for. Empty means never stand down.
    "vram_yield_apps": "",
    # Advanced, and deliberately not in the UI: overrides the image chosen by
    # the GPU checkbox, for a pinned tag or a locally built one.
    "server_docker_image": "",
}


class SettingsWindow(QWidget):
    settings_saved = Signal(dict)
    window_closed = Signal()
    # Asks the application to open (True) or release (False) the microphone
    # for the meter preview. The window has no audio source of its own -- and
    # should not grow one, since the app already owns the only capture device.
    preview_requested = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("WhisperType Settings")
        # The same wordmark the tray uses, so the window is recognisably the
        # app's in the title bar and Alt-Tab.
        self.setWindowIcon(wordmark_icon())

        # UI Elements
        self.server_address_edit = QLineEdit()
        self.server_address_edit.setToolTip(
            "host:port of the WhisperLive server. Use localhost to run it on "
            "this machine.")
        self.capture_hotkey_edit = QKeySequenceEdit()
        self.capture_hotkey_edit.setToolTip(
            "Press the key combination that starts and stops a capture.")
        self.model_combo = QComboBox()
        for choice in MODEL_CHOICES:
            if choice is None:
                self.model_combo.insertSeparator(self.model_combo.count())
                continue
            name, description = choice
            # The model id is what the server is actually sent, so it leads;
            # the description is the part that makes the list choosable.
            self.model_combo.addItem(f"{name} — {description}", name)
        self.model_combo.setToolTip(
            "Accuracy against speed. The large models need an NVIDIA GPU; "
            "on a CPU, pick a distil one.")
        self.capture_font_family_combo = QFontComboBox()
        self.capture_font_family_combo.setToolTip(
            "Transcript typeface. A tall face makes the box taller.")
        self.capture_font_family_combo.setEditable(False)
        self.capture_font_family_combo.currentFontChanged.connect(self._on_font_picked)
        self.capture_font_size_spin = QSpinBox()
        self.capture_font_size_spin.setRange(MIN_FONT_SIZE_PX, MAX_FONT_SIZE_PX)
        self.capture_font_size_spin.setSuffix(" px")
        self.capture_font_size_spin.setToolTip("Transcript text size.")
        self.capture_line_spacing_spin = QSpinBox()
        self.capture_line_spacing_spin.setRange(MIN_LINE_SPACING, MAX_LINE_SPACING)
        self.capture_line_spacing_spin.setPrefix("Lines ")
        self.capture_line_spacing_spin.setSuffix(" %")
        self.capture_line_spacing_spin.setSingleStep(5)
        self.capture_line_spacing_spin.setToolTip(
            "Gap between lines, as a % of the font's natural spacing. "
            "Higher is easier to skim.")
        self.capture_grow_to_fit_checkbox = QCheckBox("Grow for long transcripts")
        self.capture_grow_to_fit_checkbox.setToolTip(
            "Let the box grow downward for long transcripts instead of "
            "scrolling. Stops at the screen edge, then scrolls.")
        self.capture_opacity_spin = QSpinBox()
        self.capture_opacity_spin.setRange(int(MIN_OPACITY * 100), int(MAX_OPACITY * 100))
        self.capture_opacity_spin.setSuffix(" %")
        self.capture_opacity_spin.setToolTip(
            "How opaque the panel is. Lower shows the window behind; "
            "higher makes the transcript easier to read.")
        self.capture_bg_color_button = QPushButton()
        self.capture_bg_color_button.setFixedWidth(90)
        self.capture_bg_color_button.setToolTip("Panel background colour.")
        self.capture_bg_color_button.clicked.connect(self._pick_bg_color)
        self._bg_color = DEFAULT_BG_COLOR
        self.capture_panel_frost_checkbox = QCheckBox("Frosted")
        self.capture_panel_frost_checkbox.setToolTip(
            "Blur the screen behind the panel. Captured when the box opens, "
            "so it does not track windows moving behind it.")
        self.capture_field_frost_checkbox = QCheckBox("Frosted")
        self.capture_field_frost_checkbox.setToolTip(
            "Blur again behind the transcript area, so it reads as its own "
            "pane over the panel.")
        self.capture_blur_spin = self._frost_spin(
            "Blur ", " px", MIN_BLUR_RADIUS, MAX_BLUR_RADIUS,
            "Blur strength. Higher is blurrier and a little slower to open.")
        self.capture_saturation_spin = self._frost_spin(
            "Sat ", " %", 0, 400,
            "Colour in the blur. Above 100% keeps frost from looking like a "
            "grey smear on a dark desktop.")
        self.capture_brightness_spin = self._frost_spin(
            "Bright ", "", 0, 255,
            "The tint the frost fades toward, 0 black to 255 white. "
            "Needs Level above 0 to show.")
        self.capture_levelling_spin = self._frost_spin(
            "Level ", " %", 0, 100,
            "How far the frost fades toward the Bright tint. Set 0 to keep "
            "the real colours behind the box.")
        self.capture_field_opacity_spin = QSpinBox()
        self.capture_field_opacity_spin.setRange(int(MIN_OPACITY * 100), int(MAX_OPACITY * 100))
        self.capture_field_opacity_spin.setSuffix(" %")
        self.capture_field_opacity_spin.setToolTip(
            "Fill strength of the transcript area over the panel. A few "
            "per cent is usually enough to set it apart.")
        self.capture_field_color_button = QPushButton()
        self.capture_field_color_button.setFixedWidth(90)
        self.capture_field_color_button.setToolTip("Transcript area colour.")
        self.capture_field_color_button.clicked.connect(self._pick_field_color)
        self._field_color = DEFAULT_FIELD_COLOR
        self.capture_text_color_button = QPushButton()
        self.capture_text_color_button.setFixedWidth(90)
        self.capture_text_color_button.setToolTip(
            "Transcript colour. Choose it against the field colour, not "
            "the desktop.")
        self.capture_text_color_button.clicked.connect(self._pick_text_color)
        self._text_color = DEFAULT_TEXT_COLOR
        self.capture_accent_color_button = QPushButton()
        self.capture_accent_color_button.setFixedWidth(90)
        self.capture_accent_color_button.setToolTip(
            "Colour for the level meter and the Confirm button.")
        self.capture_accent_color_button.clicked.connect(self._pick_accent_color)
        self._accent_color = DEFAULT_ACCENT_COLOR
        # What is currently on disk and in the app, to compare the form
        # against on the way out. None until load_settings has run.
        self._applied_settings = None
        # Whether the font combo is showing the system default rather than a
        # face the user picked. See _font_family.
        self._font_is_default = True
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip(
            "Load a saved look into the fields below. Nothing changes until "
            "you Apply.")
        self.profile_combo.setMinimumWidth(150)
        self.profile_save_button = QPushButton("Save as...")
        self.profile_save_button.setToolTip(
            "Save the fields in this box as a named profile.")
        self.profile_delete_button = QPushButton("Delete")
        self.profile_delete_button.setToolTip("Delete the selected profile.")
        self.capture_preview_button = QPushButton("Preview")
        self.capture_preview_button.setCheckable(True)
        self.capture_preview_button.setToolTip(
            "Show a live sample box beside this window while you adjust it. "
            "It is the real box, minus listening and pasting.")
        self.capture_preview_button.toggled.connect(self._on_box_preview_toggled)
        # A box of its own rather than the app's: a preview must not be able
        # to interfere with a capture that is genuinely in progress.
        self._preview_box = None
        self.capture_meter_style_combo = QComboBox()
        for style in METER_STYLES:
            # The label is shown; the style key is what is stored and sent to
            # the box, so renaming a label in STYLE_LABELS never touches a
            # saved profile.
            self.capture_meter_style_combo.addItem(STYLE_LABELS.get(style, style), style)
        # A live preview, on the dark ground it will actually be seen against.
        # A meter can only be judged moving, so the alternative -- save, close,
        # start a capture, decide you dislike it, reopen settings -- is not a
        # way anyone would willingly compare four of them.
        self.meter_preview = SignalMeter()
        self.meter_preview_panel = QWidget()
        self.meter_preview_panel.setObjectName("meterPreview")
        # Fixed width: the styles are different sizes, and without this the
        # Test button would jump sideways every time the dropdown changed.
        self.meter_preview_panel.setFixedWidth(METER_PREVIEW_WIDTH)
        self.meter_preview_panel.setStyleSheet(
            "QWidget#meterPreview { background-color: %s; border-radius: 8px; }"
            % METER_PREVIEW_BACKGROUND
        )
        preview_layout = QHBoxLayout(self.meter_preview_panel)
        preview_layout.setContentsMargins(8, 6, 8, 6)
        preview_layout.addStretch()
        preview_layout.addWidget(self.meter_preview, 0, Qt.AlignCenter)
        preview_layout.addStretch()

        self.meter_test_button = QPushButton("Test mic")
        self.meter_test_button.setCheckable(True)
        self.meter_test_button.setToolTip(
            "Open the microphone to preview the meter. Nothing is sent to the server."
        )
        self.launch_on_startup_checkbox = QCheckBox("Launch WhisperType on system startup")
        self.connect_on_demand_checkbox = QCheckBox("Connect to server only when capture starts (on-demand)")
        self.capture_show_target_checkbox = QCheckBox(
            "Mark where the text will be pasted")
        self.capture_show_target_checkbox.setToolTip(
            "Outline the field you were typing in and mark the caret while "
            "you dictate, so you can see where the transcript is going before "
            "you speak. Falls back to outlining the whole window in apps that "
            "do not report a caret.")
        self.capture_follow_caret_checkbox = QCheckBox(
            "Open the box beside the caret, not the mouse")
        self.capture_follow_caret_checkbox.setToolTip(
            "Put the Capture Box next to the text caret when it can be found. "
            "Falls back to the mouse pointer otherwise.")
        self.capture_live_typing_checkbox = QCheckBox(
            "Type the words into the app as you speak")
        self.capture_live_typing_checkbox.setToolTip(
            "Type the transcript straight into the app as you dictate, "
            "correcting it in place as the server revises it. Cancelling "
            "removes it again. Needs an exact caret, so it only runs in real "
            "text fields; elsewhere the transcript stays in the box. Each "
            "revision is an undo step in the target app.")
        self.auto_start_server_checkbox = QCheckBox(
            "Start the WhisperLive server automatically (Docker)")
        self.auto_start_server_checkbox.setToolTip(
            "Run the WhisperLive container locally at startup. Ignored when "
            "the server address is another machine.")
        self.start_docker_desktop_checkbox = QCheckBox(
            "Launch Docker Desktop if it is not already running")
        self.server_use_gpu_checkbox = QCheckBox(
            "Use the NVIDIA GPU server image")
        self.server_use_gpu_checkbox.setToolTip(
            "Needs an NVIDIA GPU with Docker's container toolkit installed.")
        self.share_one_model_checkbox = QCheckBox(
            "Load the model once and share it between connections")
        self.share_one_model_checkbox.setToolTip(
            "One model copy for all connections. Off makes each connection "
            "load its own, filling GPU memory. Changing the model rebuilds "
            "the container.")
        self.reconnect_after_capture_checkbox = QCheckBox(
            "Reconnect immediately after each capture")
        self.reconnect_after_capture_checkbox.setToolTip(
            "Keep the connection warm so the next dictation starts instantly. "
            "Off connects on demand instead.")

        self.vram_yield_apps_edit = QLineEdit()
        self.vram_yield_apps_edit.setPlaceholderText(
            "e.g. eldenring.exe, cs2.exe  —  leave empty to never stand down")
        self.vram_yield_apps_edit.setToolTip(
            "While any of these run, the server stops to free GPU memory, then "
            "restarts when they close. The '.exe' is optional; starting the "
            "server from the tray overrides this until the app closes.")

        # Apply commits without closing, so a change can be tried against a
        # real capture and then adjusted again; Save and Exit is the same
        # commit plus the close. Both write to disk -- an "apply" that the
        # next launch forgets is a trap, not a convenience.
        self.apply_button = QPushButton("Apply")
        self.apply_button.setToolTip(
            "Save these settings and apply them, leaving this window open.")
        self.save_button = QPushButton("Save and Exit")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip(
            "Close without applying anything changed since the last Apply.")
        # Success is otherwise silent, and silence after pressing Apply reads
        # as nothing having happened. A label rather than a dialog: a modal
        # to dismiss after every Apply is worse than no confirmation at all.
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #7ac47a;")
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status_label.setText(""))

        # Layout
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        form_layout.addRow(QLabel("Server Address:"), self.server_address_edit)
        form_layout.addRow(QLabel("Capture Hotkey:"), self.capture_hotkey_edit)
        form_layout.addRow(QLabel("Model:"), self.model_combo)
        connection_column = QVBoxLayout()
        connection_column.setContentsMargins(0, 0, 0, 0)
        connection_column.addWidget(self.connect_on_demand_checkbox)
        connection_column.addWidget(self.reconnect_after_capture_checkbox)
        form_layout.addRow(QLabel("Connection Mode:"), connection_column)
        # Where the capture happens, as opposed to how it looks: both of these
        # are about the target window, which is why they sit apart from the
        # appearance box below.
        target_column = QVBoxLayout()
        target_column.setContentsMargins(0, 0, 0, 0)
        target_column.addWidget(self.capture_show_target_checkbox)
        target_column.addWidget(self.capture_follow_caret_checkbox)
        target_column.addWidget(self.capture_live_typing_checkbox)
        form_layout.addRow(QLabel("Paste Target:"), target_column)
        server_start_column = QVBoxLayout()
        server_start_column.setContentsMargins(0, 0, 0, 0)
        server_start_column.addWidget(self.auto_start_server_checkbox)
        server_start_column.addWidget(self.start_docker_desktop_checkbox)
        server_start_column.addWidget(self.server_use_gpu_checkbox)
        server_start_column.addWidget(self.share_one_model_checkbox)
        form_layout.addRow(QLabel("Server Startup:"), server_start_column)
        # Every control a profile covers, inside one titled box with the
        # picker at the top of it. The grouping is the explanation: what a
        # profile saves is the thing it is drawn around, so there is no
        # guessing whether the meter style or the server address travels with
        # it. Keep this box and APPEARANCE_KEYS in step.
        appearance_group = QGroupBox("Capture Box appearance — saved as a profile")
        appearance_form = QFormLayout(appearance_group)
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.addWidget(self.profile_combo)
        profile_row.addSpacing(8)
        profile_row.addWidget(self.profile_save_button)
        profile_row.addWidget(self.profile_delete_button)
        profile_row.addStretch()
        profile_row.addWidget(self.capture_preview_button)
        appearance_form.addRow(QLabel("Profile:"), profile_row)
        type_row = QHBoxLayout()
        type_row.setContentsMargins(0, 0, 0, 0)
        type_row.addWidget(self.capture_font_family_combo, 1)
        type_row.addSpacing(8)
        type_row.addWidget(self.capture_font_size_spin)
        type_row.addSpacing(8)
        type_row.addWidget(self.capture_line_spacing_spin)
        type_row.addSpacing(8)
        type_row.addWidget(self.capture_text_color_button)
        appearance_form.addRow(QLabel("Capture Text:"), type_row)
        appearance_form.addRow(QLabel(""), self.capture_grow_to_fit_checkbox)
        appearance_row = QHBoxLayout()
        appearance_row.setContentsMargins(0, 0, 0, 0)
        appearance_row.addWidget(self.capture_opacity_spin)
        appearance_row.addSpacing(8)
        appearance_row.addWidget(self.capture_bg_color_button)
        appearance_row.addSpacing(8)
        appearance_row.addWidget(self.capture_panel_frost_checkbox)
        appearance_row.addStretch()
        appearance_form.addRow(QLabel("Panel:"), appearance_row)
        field_row = QHBoxLayout()
        field_row.setContentsMargins(0, 0, 0, 0)
        field_row.addWidget(self.capture_field_opacity_spin)
        field_row.addSpacing(8)
        field_row.addWidget(self.capture_field_color_button)
        field_row.addSpacing(8)
        field_row.addWidget(self.capture_field_frost_checkbox)
        field_row.addStretch()
        appearance_form.addRow(QLabel("Transcript Field:"), field_row)
        frost_row = QHBoxLayout()
        frost_row.setContentsMargins(0, 0, 0, 0)
        for spin in (self.capture_blur_spin, self.capture_saturation_spin,
                     self.capture_brightness_spin, self.capture_levelling_spin):
            frost_row.addWidget(spin)
            frost_row.addSpacing(4)
        frost_row.addStretch()
        appearance_form.addRow(QLabel("Frost:"), frost_row)
        accent_row = QHBoxLayout()
        accent_row.setContentsMargins(0, 0, 0, 0)
        accent_row.addWidget(self.capture_accent_color_button)
        accent_row.addSpacing(8)
        accent_row.addWidget(QLabel("Level meter and the Confirm button"))
        accent_row.addStretch()
        appearance_form.addRow(QLabel("Accent:"), accent_row)
        meter_row = QHBoxLayout()
        meter_row.setContentsMargins(0, 0, 0, 0)
        meter_row.addWidget(self.capture_meter_style_combo)
        meter_row.addWidget(self.meter_preview_panel)
        meter_row.addWidget(self.meter_test_button)
        meter_row.addStretch()
        appearance_form.addRow(QLabel("Level Meter:"), meter_row)
        # Anything the box's appearance is made of, pushed straight at it.
        # The backdrop is the one that needs a new snapshot rather than a
        # repaint -- see _sync_preview.
        # Lambdas rather than the bound method: every one of these signals
        # carries a value, and it would arrive in `regrab` -- making any
        # non-zero spinbox tick re-snapshot the backdrop, with the hide and
        # show that costs.
        for signal in (self.capture_opacity_spin.valueChanged,
                       self.capture_field_opacity_spin.valueChanged,
                       self.capture_font_size_spin.valueChanged,
                       self.capture_line_spacing_spin.valueChanged,
                       self.capture_font_family_combo.currentFontChanged,
                       self.capture_meter_style_combo.currentIndexChanged):
            signal.connect(lambda *_: self._sync_preview())
        # These change the snapshot itself rather than what is painted over
        # it, so they need it taken again.
        for signal in (self.capture_panel_frost_checkbox.toggled,
                       self.capture_grow_to_fit_checkbox.toggled,
                       self.capture_field_frost_checkbox.toggled,
                       self.capture_blur_spin.valueChanged,
                       self.capture_saturation_spin.valueChanged,
                       self.capture_brightness_spin.valueChanged,
                       self.capture_levelling_spin.valueChanged):
            signal.connect(lambda *_: self._sync_preview(regrab=True))

        form_layout.addRow(QLabel("Release GPU for:"), self.vram_yield_apps_edit)

        layout.addLayout(form_layout)
        layout.addWidget(appearance_group)
        layout.addWidget(self.launch_on_startup_checkbox)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.status_label)
        button_layout.addStretch()
        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        # Connections
        self.capture_meter_style_combo.currentIndexChanged.connect(
            lambda _: self.meter_preview.set_style(self.capture_meter_style_combo.currentData()))
        self.meter_test_button.toggled.connect(self._on_preview_toggled)
        self.auto_start_server_checkbox.toggled.connect(self._sync_server_startup_enabled)
        self.profile_combo.activated.connect(self._on_profile_chosen)
        self.profile_save_button.clicked.connect(self._save_profile)
        self.profile_delete_button.clicked.connect(self._delete_profile)
        self.apply_button.clicked.connect(self.apply_settings)
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.close)

        self.load_settings()

    def _select_meter_style(self, style: str):
        """Select `style` in the meter dropdown by its storage key, not its label."""
        index = self.capture_meter_style_combo.findData(style)
        if index >= 0:
            self.capture_meter_style_combo.setCurrentIndex(index)

    def _select_model(self, model: str):
        """Select `model`, keeping a hand-edited one that is not on the list.

        WhisperLive accepts model names this list does not carry -- a custom or
        locally converted one, set in config.json by hand. Dropping such a
        value on the floor the first time the Settings window was opened would
        silently rewrite the config to something the user did not choose, so it
        is added to the list instead and marked as what it is.
        """
        index = self.model_combo.findData(model)
        if index < 0 and model:
            self.model_combo.insertItem(0, f"{model} — custom", model)
            index = 0
        self.model_combo.setCurrentIndex(max(index, 0))

    def _profile_names(self):
        try:
            names = [f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".json")]
        except OSError:
            return []
        return sorted(names, key=str.lower)

    def _refresh_profiles(self, select: str = None):
        """Reload the profile list, leaving `select` (or nothing) chosen."""
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        # A placeholder rather than auto-selecting the first: opening this
        # window must not look like a profile is in force when none is.
        self.profile_combo.addItem("(none)", None)
        for name in self._profile_names():
            self.profile_combo.addItem(name, name)
        if select:
            index = self.profile_combo.findData(select)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self.profile_delete_button.setEnabled(bool(self.profile_combo.currentData()))

    def _on_profile_chosen(self, _index: int):
        """Fill the appearance group in from the chosen profile.

        Loaded into the window and no further: a profile that took effect the
        instant it was picked would give no way to look at one without
        adopting it. Apply is still what commits.

        Only the appearance keys are read across, so a profile written by an
        older build -- when a profile was the whole settings file -- cannot
        reach in and change the server address or the hotkey on its way past.
        """
        name = self.profile_combo.currentData()
        self.profile_delete_button.setEnabled(bool(name))
        if not name:
            return
        try:
            with open(profile_path(name), "r", encoding="utf-8") as f:
                settings = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Profile", f"Could not read '{name}': {e}")
            self._refresh_profiles()
            return
        if not isinstance(settings, dict):
            QMessageBox.warning(self, "Profile", f"'{name}' is not a settings file.")
            return
        # Over the defaults, so a profile written by an older version is
        # missing keys rather than carrying stale ones.
        appearance = {key: DEFAULT_SETTINGS[key] for key in APPEARANCE_KEYS}
        appearance.update({key: settings[key] for key in APPEARANCE_KEYS
                           if key in settings})
        self._load_appearance(appearance)
        self._sync_preview(regrab=True)
        self.status_label.setText(f"Loaded '{name}' -- not applied yet.")
        self._status_timer.start(4000)

    def _save_profile(self):
        current = self.profile_combo.currentData() or ""
        name, ok = QInputDialog.getText(
            self, "Save profile", "Profile name:", text=current)
        name = (name or "").strip()
        if not ok or not name:
            return
        # validate=False: a profile carries no hotkey, so an invalid one is
        # not this button's business to complain about.
        settings = self._collect_settings(validate=False)
        settings = {key: settings[key] for key in APPEARANCE_KEYS}
        path = profile_path(name)
        if os.path.exists(path) and QMessageBox.question(
                self, "Save profile",
                f"'{name}' already exists. Replace it?") != QMessageBox.Yes:
            return
        try:
            os.makedirs(PROFILE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            QMessageBox.warning(self, "Profile", f"Could not save '{name}': {e}")
            return
        self._refresh_profiles(select=name)
        self.status_label.setText(f"Saved profile '{name}'.")
        self._status_timer.start(2500)

    def _delete_profile(self):
        name = self.profile_combo.currentData()
        if not name:
            return
        if QMessageBox.question(
                self, "Delete profile",
                f"Delete the profile '{name}'? This cannot be undone."
        ) != QMessageBox.Yes:
            return
        try:
            os.remove(profile_path(name))
        except OSError as e:
            QMessageBox.warning(self, "Profile", f"Could not delete '{name}': {e}")
        self._refresh_profiles()
        self.status_label.setText(f"Deleted profile '{name}'.")
        self._status_timer.start(2500)

    def _font_family(self) -> str:
        """The chosen family, or "" for the system default.

        QFontComboBox always has something selected, so "the system font" is
        not a state it can be in. It is stored as the empty string and shown
        as whatever Qt actually resolved that to, which is the honest way to
        display a default nobody named.
        """
        if self._font_is_default:
            return ""
        return self.capture_font_family_combo.currentFont().family()

    def _set_font_family(self, family: str):
        """Show `family` in the combo without it counting as a choice.

        Signals are blocked over the assignment rather than the handler being
        disconnected and reconnected around it: the handler is what tells a
        deliberate pick from a value being loaded, and a connection made in
        one place and undone in another is how it would end up attached twice.
        """
        combo = self.capture_font_family_combo
        self._font_is_default = not (family or "").strip()
        combo.blockSignals(True)
        combo.setCurrentFont(QFont(family) if family else QWidget().font())
        combo.blockSignals(False)

    def _on_font_picked(self, _font):
        self._font_is_default = False

    @staticmethod
    def _frost_spin(prefix: str, suffix: str, low: int, high: int, tip: str) -> QSpinBox:
        """One of the frost numeric controls.

        Prefixed rather than labelled: four of these share a row, and four
        separate labels would not fit beside them.
        """
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setPrefix(prefix)
        spin.setSuffix(suffix)
        spin.setToolTip(tip)
        return spin

    def _pick_bg_color(self):
        chosen = QColorDialog.getColor(QColor(self._bg_color), self, "Panel colour")
        if chosen.isValid():
            self._set_bg_color(chosen.name())

    def _pick_field_color(self):
        chosen = QColorDialog.getColor(
            QColor(self._field_color), self, "Transcript field colour")
        if chosen.isValid():
            self._set_field_color(chosen.name())

    def _pick_text_color(self):
        chosen = QColorDialog.getColor(
            QColor(self._text_color), self, "Transcript colour")
        if chosen.isValid():
            self._set_text_color(chosen.name())

    def _pick_accent_color(self):
        chosen = QColorDialog.getColor(
            QColor(self._accent_color), self, "Accent colour")
        if chosen.isValid():
            self._set_accent_color(chosen.name())

    def _set_bg_color(self, color: str):
        self._bg_color = self._paint_swatch(
            self.capture_bg_color_button, color, DEFAULT_BG_COLOR)
        self._sync_preview()

    def _set_field_color(self, color: str):
        self._field_color = self._paint_swatch(
            self.capture_field_color_button, color, DEFAULT_FIELD_COLOR)
        self._sync_preview()

    def _set_text_color(self, color: str):
        self._text_color = self._paint_swatch(
            self.capture_text_color_button, color, DEFAULT_TEXT_COLOR)
        self._sync_preview()

    def _set_accent_color(self, color: str):
        self._accent_color = self._paint_swatch(
            self.capture_accent_color_button, color, DEFAULT_ACCENT_COLOR)
        # The settings window's own meter is the thing most likely to be
        # looked at while choosing this, so it follows immediately.
        self.meter_preview.set_accent_color(self._accent_color)
        self._sync_preview()

    @staticmethod
    def _paint_swatch(button, color: str, fallback: str) -> str:
        """Show `color` on the button that picks it. Returns what was set.

        The swatch is the button itself rather than a label beside it: the
        colour is the only thing the button has to say, and a hex code means
        nothing next to seeing it.
        """
        value = QColor(color)
        if not value.isValid():
            value = QColor(fallback)
        name = value.name()
        # Readable caption whichever end of the range the colour is from.
        ink = "#000000" if value.lightness() > 127 else "#ffffff"
        button.setText(name)
        button.setStyleSheet(
            f"background-color: {name}; color: {ink};"
            " border: 1px solid #888; padding: 4px;")
        return name

    def _sync_server_startup_enabled(self):
        """The sub-options only mean anything when auto-start is on."""
        enabled = self.auto_start_server_checkbox.isChecked()
        self.start_docker_desktop_checkbox.setEnabled(enabled)
        self.server_use_gpu_checkbox.setEnabled(enabled)
        # Sharing one model is done by how the container is launched, so it is
        # only ours to arrange when we launch it.
        self.share_one_model_checkbox.setEnabled(enabled)

    def _on_box_preview_toggled(self, active: bool):
        self.capture_preview_button.setText("Close preview" if active else "Preview")
        if not active:
            self.close_box_preview()
            return
        if self._preview_box is None:
            self._preview_box = CaptureBox()
            self._preview_box.set_text(PREVIEW_TEXT)
            # Clicking the sample's own Confirm or Cancel closes it, so the
            # button has to come back up with it.
            self._preview_box.confirmed.connect(
                lambda _: self.capture_preview_button.setChecked(False))
            self._preview_box.cancelled.connect(
                lambda: self.capture_preview_button.setChecked(False))
        self._sync_preview()
        # Beside this window rather than over it, so both are readable at
        # once; _move_near flips it to the other side at a screen edge.
        frame = self.frameGeometry()
        self._preview_box.show_preview(QPoint(frame.right(), frame.top()))

    def close_box_preview(self):
        """Take the sample box down. Safe when there never was one."""
        if self._preview_box is not None:
            self._preview_box.hide()

    def _sync_preview(self, regrab: bool = False):
        """Push the controls' current values onto the sample box.

        `regrab` re-takes the frosted snapshot, which only the backdrop and a
        move actually invalidate: a colour or opacity change does not alter
        what is behind the box, so repainting over the snapshot it already
        has is both correct and free of the hide/show flicker regrabbing
        costs.
        """
        box = self._preview_box
        if box is None:
            return
        box.set_font_size(self.capture_font_size_spin.value())
        box.set_font_family(self._font_family())
        box.set_line_spacing(self.capture_line_spacing_spin.value())
        box.set_grow_to_fit(self.capture_grow_to_fit_checkbox.isChecked())
        box.set_meter_style(self.capture_meter_style_combo.currentData())
        box.set_surface(self._bg_color,
                        self.capture_opacity_spin.value() / 100.0,
                        self.capture_panel_frost_checkbox.isChecked())
        box.set_field(self._field_color,
                      self.capture_field_opacity_spin.value() / 100.0,
                      self.capture_field_frost_checkbox.isChecked())
        box.set_text_color(self._text_color)
        box.set_accent_color(self._accent_color)
        box.set_frost(self.capture_blur_spin.value(),
                      self.capture_saturation_spin.value(),
                      self.capture_brightness_spin.value(),
                      self.capture_levelling_spin.value())
        if regrab:
            box.refresh_backdrop()

    def set_preview_level_db(self, db: float):
        """Feed the preview meter, while the app has the mic open for it."""
        self.meter_preview.set_level_db(db)
        # The sample box has a meter of its own, and a dead one in a preview
        # of the box reads as a broken box.
        if self._preview_box is not None and self._preview_box.isVisible():
            self._preview_box.set_level_db(db)

    def _on_preview_toggled(self, active: bool):
        self.meter_test_button.setText("Stop test" if active else "Test mic")
        if not active:
            self.meter_preview.reset()
        self.preview_requested.emit(active)

    def stop_preview(self):
        """Release the microphone. Safe to call when it was never opened."""
        if self.meter_test_button.isChecked():
            self.meter_test_button.setChecked(False)

    def get_config_path(self):
        return CONFIG_FILE

    def load_settings(self, settings=None):
        """Fill the form in, from the config file or from `settings`.

        A caller-supplied dict is a profile being read into the window, which
        is deliberately not the same as the window being opened: the clean
        state is only re-taken for what actually came off disk, so a loaded
        profile counts as an unapplied edit and Cancel says so.
        """
        from_disk = settings is None
        if from_disk:
            path = self.get_config_path()
            if os.path.exists(path):
                with open(path, "r") as f:
                    settings = json.load(f)
            else:
                settings = copy.deepcopy(DEFAULT_SETTINGS)

        self.server_address_edit.setText(settings.get("server_address", DEFAULT_SETTINGS["server_address"]))
        capture_hotkey = settings.get("capture_hotkey", DEFAULT_SETTINGS["capture_hotkey"])
        self.capture_hotkey_edit.setKeySequence(QKeySequence(self._display_hotkey(capture_hotkey)))
        self._select_model(settings.get("model", DEFAULT_SETTINGS["model"]))
        self.launch_on_startup_checkbox.setChecked(settings.get("launch_on_startup", DEFAULT_SETTINGS["launch_on_startup"]))
        self.connect_on_demand_checkbox.setChecked(settings.get("connect_on_demand", DEFAULT_SETTINGS["connect_on_demand"]))
        self.capture_show_target_checkbox.setChecked(bool(settings.get(
            "capture_show_target", DEFAULT_SETTINGS["capture_show_target"])))
        self.capture_follow_caret_checkbox.setChecked(bool(settings.get(
            "capture_follow_caret", DEFAULT_SETTINGS["capture_follow_caret"])))
        self.capture_live_typing_checkbox.setChecked(bool(settings.get(
            "capture_live_typing", DEFAULT_SETTINGS["capture_live_typing"])))
        self.auto_start_server_checkbox.setChecked(
            settings.get("auto_start_server", DEFAULT_SETTINGS["auto_start_server"]))
        self.start_docker_desktop_checkbox.setChecked(
            settings.get("start_docker_desktop", DEFAULT_SETTINGS["start_docker_desktop"]))
        self.server_use_gpu_checkbox.setChecked(
            settings.get("server_use_gpu", DEFAULT_SETTINGS["server_use_gpu"]))
        self.share_one_model_checkbox.setChecked(
            settings.get("share_one_model", DEFAULT_SETTINGS["share_one_model"]))
        self.reconnect_after_capture_checkbox.setChecked(
            settings.get("reconnect_after_capture", DEFAULT_SETTINGS["reconnect_after_capture"]))
        self.vram_yield_apps_edit.setText(
            settings.get("vram_yield_apps", DEFAULT_SETTINGS["vram_yield_apps"]))
        self._server_docker_image = settings.get(
            "server_docker_image", DEFAULT_SETTINGS["server_docker_image"])
        self._sync_server_startup_enabled()
        self._load_appearance(settings)

        # The clean state to measure edits against. Read back off the form
        # rather than kept from the file: a config missing a key, or holding
        # one this window does not edit, would otherwise read as dirty the
        # moment it opened.
        if from_disk:
            # The profile the appearance in force came from, re-selected
            # before the clean state is taken -- otherwise reopening the
            # window would read as an unapplied change. A profile deleted
            # from disk in the meantime simply leaves "(none)" showing.
            self._refresh_profiles(
                select=settings.get("active_profile",
                                    DEFAULT_SETTINGS["active_profile"]))
            self._applied_settings = self._collect_settings(validate=False)

    def _load_appearance(self, settings: dict):
        """Fill in the Capture Box appearance group, and only that group.

        Separate from load_settings because a profile is exactly this much of
        a settings dict -- see APPEARANCE_KEYS -- and choosing one must not
        disturb the server or hotkey controls above it.
        """
        self.capture_font_size_spin.setValue(
            int(settings.get("capture_font_size", DEFAULT_SETTINGS["capture_font_size"])))
        self._set_font_family(settings.get(
            "capture_font_family", DEFAULT_SETTINGS["capture_font_family"]))
        self.capture_line_spacing_spin.setValue(int(settings.get(
            "capture_line_spacing", DEFAULT_SETTINGS["capture_line_spacing"])))
        self.capture_grow_to_fit_checkbox.setChecked(bool(settings.get(
            "capture_grow_to_fit", DEFAULT_SETTINGS["capture_grow_to_fit"])))
        self.capture_opacity_spin.setValue(int(round(float(
            settings.get("capture_opacity", DEFAULT_SETTINGS["capture_opacity"])) * 100)))
        self._set_bg_color(settings.get("capture_bg_color", DEFAULT_SETTINGS["capture_bg_color"]))
        self.capture_field_opacity_spin.setValue(int(round(float(
            settings.get("capture_field_opacity",
                         DEFAULT_SETTINGS["capture_field_opacity"])) * 100)))
        self._set_field_color(
            settings.get("capture_field_color", DEFAULT_SETTINGS["capture_field_color"]))
        self._set_text_color(
            settings.get("capture_text_color", DEFAULT_SETTINGS["capture_text_color"]))
        self._set_accent_color(
            settings.get("capture_accent_color", DEFAULT_SETTINGS["capture_accent_color"]))
        # capture_backdrop was one setting for the whole box, before the two
        # surfaces were frosted separately. Carry it onto the panel, which is
        # what it used to mean.
        legacy = settings.get("capture_backdrop")
        panel_frost = settings.get(
            "capture_panel_frost",
            legacy == "frost" if legacy is not None
            else DEFAULT_SETTINGS["capture_panel_frost"])
        self.capture_panel_frost_checkbox.setChecked(bool(panel_frost))
        self.capture_field_frost_checkbox.setChecked(bool(settings.get(
            "capture_field_frost", DEFAULT_SETTINGS["capture_field_frost"])))
        for key, spin in (("capture_blur_radius", self.capture_blur_spin),
                          ("capture_frost_saturation", self.capture_saturation_spin),
                          ("capture_frost_brightness", self.capture_brightness_spin),
                          ("capture_frost_levelling", self.capture_levelling_spin)):
            spin.setValue(int(settings.get(key, DEFAULT_SETTINGS[key])))
        meter_style = settings.get("capture_meter_style", DEFAULT_SETTINGS["capture_meter_style"])
        if meter_style not in METER_STYLES:
            meter_style = DEFAULT_SETTINGS["capture_meter_style"]
        self._select_meter_style(meter_style)

    def _collect_settings(self, validate: bool = True):
        """Everything the form is currently saying, as a settings dict.

        `validate` off skips the hotkey complaints, for the dirty check --
        which runs on every close and must not put a dialog in front of
        someone who is only trying to leave.
        """
        sequence = self.capture_hotkey_edit.keySequence()
        hotkey_str = self._normalize_hotkey(sequence)

        if validate:
            modifier_keys = {'ctrl', 'alt', 'shift', 'cmd'}
            keys = set(part.strip('<>').strip().lower() for part in hotkey_str.split('+') if part.strip())
            if not hotkey_str or not keys or keys.issubset(modifier_keys):
                QMessageBox.warning(self, "Invalid Hotkey", "Please press a hotkey that includes at least one non-modifier key (e.g., Ctrl+Shift+` or Alt+F1).")
                return None

            # Check if pynput can parse it
            try:
                from pynput import keyboard
                keyboard.HotKey.parse(hotkey_str)
            except Exception as e:
                QMessageBox.warning(self, "Invalid Hotkey", f"The hotkey '{hotkey_str}' could not be parsed. Please check the format.\n\nError: {e}")
                return None

        settings = {
            "server_address": self.server_address_edit.text(),
            "capture_hotkey": hotkey_str,
            "launch_on_startup": self.launch_on_startup_checkbox.isChecked(),
            "model": self.model_combo.currentData() or DEFAULT_SETTINGS["model"],
            "connect_on_demand": self.connect_on_demand_checkbox.isChecked(),
            "capture_show_target": self.capture_show_target_checkbox.isChecked(),
            "capture_follow_caret": self.capture_follow_caret_checkbox.isChecked(),
            "capture_live_typing": self.capture_live_typing_checkbox.isChecked(),
            "auto_start_server": self.auto_start_server_checkbox.isChecked(),
            "start_docker_desktop": self.start_docker_desktop_checkbox.isChecked(),
            "server_use_gpu": self.server_use_gpu_checkbox.isChecked(),
            "share_one_model": self.share_one_model_checkbox.isChecked(),
            "reconnect_after_capture": self.reconnect_after_capture_checkbox.isChecked(),
            "vram_yield_apps": self.vram_yield_apps_edit.text(),
            # Round-tripped rather than edited: it has no UI, but a value set
            # by hand in config.json must survive a visit to this window.
            "server_docker_image": getattr(self, "_server_docker_image", ""),
            "capture_font_size": self.capture_font_size_spin.value(),
            "capture_font_family": self._font_family(),
            "capture_line_spacing": self.capture_line_spacing_spin.value(),
            "capture_grow_to_fit": self.capture_grow_to_fit_checkbox.isChecked(),
            "capture_opacity": self.capture_opacity_spin.value() / 100.0,
            "capture_bg_color": self._bg_color,
            "capture_panel_frost": self.capture_panel_frost_checkbox.isChecked(),
            "capture_field_frost": self.capture_field_frost_checkbox.isChecked(),
            "capture_blur_radius": self.capture_blur_spin.value(),
            "capture_frost_saturation": self.capture_saturation_spin.value(),
            "capture_frost_brightness": self.capture_brightness_spin.value(),
            "capture_frost_levelling": self.capture_levelling_spin.value(),
            "capture_field_opacity": self.capture_field_opacity_spin.value() / 100.0,
            "capture_field_color": self._field_color,
            "capture_text_color": self._text_color,
            "capture_accent_color": self._accent_color,
            "capture_meter_style": (self.capture_meter_style_combo.currentData()
                                    or DEFAULT_SETTINGS["capture_meter_style"]),
            # Not a setting so much as a bookmark: which profile the
            # appearance above came from, so the next launch reopens on it.
            "active_profile": self.profile_combo.currentData() or "",
        }
        return settings

    def _commit(self) -> bool:
        """Write the form to disk and hand it to the app. False if it did not."""
        settings = self._collect_settings()
        if settings is None:
            return False
        try:
            with open(self.get_config_path(), "w") as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save settings: {e}")
            return False
        # Only now: a dict that failed to reach the disk is not what the
        # window should be treating as its clean state.
        self._applied_settings = copy.deepcopy(settings)
        self.settings_saved.emit(settings)
        return True

    def apply_settings(self):
        """Save and apply, without closing."""
        if not self._commit():
            return
        self.status_label.setText("Saved.")
        self._status_timer.start(2500)

    def save_settings(self):
        """Save, apply, and close."""
        if self._commit():
            self.close()

    def _is_dirty(self) -> bool:
        """Whether the form says something other than what was last applied."""
        if self._applied_settings is None:
            return False
        return self._collect_settings(validate=False) != self._applied_settings

    def _normalize_hotkey(self, sequence: QKeySequence) -> str:
        """
        Convert a QKeySequence to a lowercase string usable by pynput (e.g., '<ctrl>+`').
        """
        text = sequence.toString(QKeySequence.NativeText)
        if not text:
            return ""

        normalized = []
        for raw in text.split('+'):
            key = raw.strip()
            if not key:
                continue
            lower = key.lower()
            if lower in ("ctrl", "control"):
                normalized.append("<ctrl>")
            elif lower in ("alt",):
                normalized.append("<alt>")
            elif lower in ("shift",):
                normalized.append("<shift>")
            elif lower in ("meta", "win", "windows", "super", "cmd"):
                normalized.append("<cmd>")
            else:
                key_clean = lower.strip()
                if len(key_clean) == 1:
                    normalized.append(key_clean)
                else:
                    normalized.append(f"<{key_clean}>")
        return "+".join(normalized)

    def _display_hotkey(self, stored: str) -> str:
        """
        Convert a stored pynput-style hotkey string (with angle-bracket modifiers) into
        a user-friendly string for QKeySequence.
        """
        if not stored:
            return ""
        parts = []
        for token in stored.split('+'):
            t = token.strip().lower()
            if t in ("<ctrl>", "ctrl"):
                parts.append("Ctrl")
            elif t in ("<alt>", "alt"):
                parts.append("Alt")
            elif t in ("<shift>", "shift"):
                parts.append("Shift")
            elif t in ("<cmd>", "cmd"):
                parts.append("Meta")
            else:
                t = t.strip("<>")
                parts.append(t)
        return "+".join(parts)

    def closeEvent(self, event):
        if self._is_dirty():
            choice = QMessageBox.warning(
                self, "Unsaved changes",
                "There are changes here that have not been applied.\n\n"
                "Save them before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if choice == QMessageBox.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.Save and not self._commit():
                # Saving failed, or the hotkey was refused. Staying open is
                # the only outcome that does not throw the edits away.
                event.ignore()
                return
        # Before the close is announced: leaving the mic open behind a closed
        # settings window would be both a leak and a genuine privacy surprise.
        self.stop_preview()
        self.capture_preview_button.setChecked(False)
        self.close_box_preview()
        self.window_closed.emit()
        super().closeEvent(event)


if __name__ == '__main__':
    # For testing the window independently
    from PySide6.QtWidgets import QApplication
    import sys

    app = QApplication(sys.argv)
    window = SettingsWindow()
    window.show()
    sys.exit(app.exec())
