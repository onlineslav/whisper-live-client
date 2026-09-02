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
    QMessageBox,
    QKeySequenceEdit,
    QSpinBox,
    QComboBox,
    QColorDialog,
    QFontComboBox,
)
from PySide6.QtCore import Qt, Signal, QPoint, QTimer
from PySide6.QtGui import QKeySequence, QColor, QFont

from signal_meter import SignalMeter

from capture_box import (
    CaptureBox,
    DEFAULT_FONT_SIZE_PX,
    MIN_FONT_SIZE_PX,
    MAX_FONT_SIZE_PX,
    DEFAULT_FONT_FAMILY,
    METER_STYLES,
    DEFAULT_OPACITY,
    MIN_OPACITY,
    MAX_OPACITY,
    DEFAULT_BG_COLOR,
    DEFAULT_FIELD_OPACITY,
    DEFAULT_FIELD_COLOR,
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

APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WhisperBoard")
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


DEFAULT_SETTINGS = {
    "server_address": "ws://localhost:9090",
    "capture_hotkey": "ctrl+`",
    "launch_on_startup": False,
    "model": "distil-small.en",
    "connect_on_demand": True,
    # Re-transcribe the whole capture in one piece when it is confirmed, and
    # paste that instead of the text assembled live. See final_pass.py.
    "final_pass": True,
    "capture_font_size": DEFAULT_FONT_SIZE_PX,
    # Empty means the system UI font.
    "capture_font_family": DEFAULT_FONT_FAMILY,
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
    "capture_meter_style": METER_STYLES[0],
    # Server startup. WhisperBoard launches itself at login but the WhisperLive
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
        self.setWindowTitle("WhisperBoard Settings")

        # UI Elements
        self.server_address_edit = QLineEdit()
        self.capture_hotkey_edit = QKeySequenceEdit()
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
            "The transcription model the server loads. The large models need an "
            "NVIDIA GPU; on a CPU, prefer the distil ones.")
        self.capture_font_family_combo = QFontComboBox()
        self.capture_font_family_combo.setToolTip(
            "The transcript typeface. The box height follows the line height, "
            "so a tall face can make the box taller.")
        self.capture_font_family_combo.setEditable(False)
        self.capture_font_family_combo.currentFontChanged.connect(self._on_font_picked)
        self.capture_font_size_spin = QSpinBox()
        self.capture_font_size_spin.setRange(MIN_FONT_SIZE_PX, MAX_FONT_SIZE_PX)
        self.capture_font_size_spin.setSuffix(" px")
        self.capture_opacity_spin = QSpinBox()
        self.capture_opacity_spin.setRange(int(MIN_OPACITY * 100), int(MAX_OPACITY * 100))
        self.capture_opacity_spin.setSuffix(" %")
        self.capture_opacity_spin.setToolTip(
            "How solid the Capture Box is over whatever is behind it. Lower "
            "lets the window underneath show through; higher makes the "
            "transcript easier to read over a page of text.")
        self.capture_bg_color_button = QPushButton()
        self.capture_bg_color_button.setFixedWidth(90)
        self.capture_bg_color_button.setToolTip("The Capture Box's background colour.")
        self.capture_bg_color_button.clicked.connect(self._pick_bg_color)
        self._bg_color = DEFAULT_BG_COLOR
        self.capture_panel_frost_checkbox = QCheckBox("Frosted")
        self.capture_panel_frost_checkbox.setToolTip(
            "Blur whatever is behind the panel, so a page of text underneath "
            "reads as texture rather than competing with the transcript. The "
            "blur is of the screen as it was when the box opened, so it does "
            "not follow anything moving behind it.")
        self.capture_field_frost_checkbox = QCheckBox("Frosted")
        self.capture_field_frost_checkbox.setToolTip(
            "Frost the transcript inset as well, as a second sheet of glass "
            "over the panel. Drawn over the panel tint rather than under it, "
            "so the field reads as its own surface.")
        self.capture_blur_spin = self._frost_spin(
            "Blur ", " px", MIN_BLUR_RADIUS, MAX_BLUR_RADIUS,
            "Gaussian blur radius. Higher is blurrier, and costs a little "
            "more each time the box opens.")
        self.capture_saturation_spin = self._frost_spin(
            "Sat ", " %", 0, 400,
            "Colour in the blurred snapshot. 100% is what was actually on "
            "screen; above that is what makes frost read as glass rather "
            "than as a grey smear on a dark desktop.")
        self.capture_brightness_spin = self._frost_spin(
            "Bright ", "", 0, 255,
            "The brightness the frost is pulled towards, 0 black to 255 "
            "white. Only has an effect to the extent Level is above zero.")
        self.capture_levelling_spin = self._frost_spin(
            "Level ", " %", 0, 100,
            "How hard the frost is pulled towards Bright. This is why a "
            "panel at 0% opacity still looks grey rather than clear: at 0% "
            "there is no tint left, so what shows is the levelled snapshot. "
            "Set this to 0 and the frost keeps the real colours of whatever "
            "is behind it.")
        self.capture_field_opacity_spin = QSpinBox()
        self.capture_field_opacity_spin.setRange(int(MIN_OPACITY * 100), int(MAX_OPACITY * 100))
        self.capture_field_opacity_spin.setSuffix(" %")
        self.capture_field_opacity_spin.setToolTip(
            "How strongly the transcript's inset is filled over the panel "
            "behind it. This is what separates the text from the buttons and "
            "the meter; a few per cent is usually enough.")
        self.capture_field_color_button = QPushButton()
        self.capture_field_color_button.setFixedWidth(90)
        self.capture_field_color_button.setToolTip("The transcript inset's colour.")
        self.capture_field_color_button.clicked.connect(self._pick_field_color)
        self._field_color = DEFAULT_FIELD_COLOR
        # What is currently on disk and in the app, to compare the form
        # against on the way out. None until load_settings has run.
        self._applied_settings = None
        # Whether the font combo is showing the system default rather than a
        # face the user picked. See _font_family.
        self._font_is_default = True
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip(
            "A whole saved settings file, by name. Choosing one fills this "
            "window in from it -- nothing reaches the app until Apply.")
        self.profile_combo.setMinimumWidth(150)
        self.profile_save_button = QPushButton("Save as...")
        self.profile_save_button.setToolTip(
            "Save everything in this window as a named profile.")
        self.profile_delete_button = QPushButton("Delete")
        self.profile_delete_button.setToolTip("Delete the selected profile.")
        self.capture_preview_button = QPushButton("Preview")
        self.capture_preview_button.setCheckable(True)
        self.capture_preview_button.setToolTip(
            "Open a sample Capture Box beside this window and keep it there "
            "while you adjust it. It is the real box, so what it looks like "
            "is what dictation will look like -- it just does not listen, "
            "paste, or close itself when you click away.")
        self.capture_preview_button.toggled.connect(self._on_box_preview_toggled)
        # A box of its own rather than the app's: a preview must not be able
        # to interfere with a capture that is genuinely in progress.
        self._preview_box = None
        self.capture_meter_style_combo = QComboBox()
        self.capture_meter_style_combo.addItems(METER_STYLES)
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
        self.launch_on_startup_checkbox = QCheckBox("Launch WhisperBoard on system startup")
        self.connect_on_demand_checkbox = QCheckBox("Connect to server only when capture starts (on-demand)")
        self.final_pass_checkbox = QCheckBox(
            "Re-transcribe the whole capture before pasting")
        self.final_pass_checkbox.setToolTip(
            "Live transcription works on a rolling buffer and discards audio as "
            "it goes, so a pause mid-sentence can come out as two sentences. "
            "This sends the capture again in one piece when you confirm it, "
            "which reads the whole thing in context. Costs up to a second "
            "before the paste appears.")
        self.auto_start_server_checkbox = QCheckBox(
            "Start the WhisperLive server automatically (Docker)")
        self.auto_start_server_checkbox.setToolTip(
            "Runs the WhisperLive container on this machine when WhisperBoard "
            "starts. Ignored when the server address points at another machine.")
        self.start_docker_desktop_checkbox = QCheckBox(
            "Launch Docker Desktop if it is not already running")
        self.server_use_gpu_checkbox = QCheckBox(
            "Use the NVIDIA GPU server image")
        self.server_use_gpu_checkbox.setToolTip(
            "Requires an NVIDIA GPU with Docker's container toolkit installed.")
        self.share_one_model_checkbox = QCheckBox(
            "Load the model once and share it between connections")
        self.share_one_model_checkbox.setToolTip(
            "Without this the server loads a separate copy of the model for every "
            "connection, which accumulates in GPU memory and slows transcription "
            "down. Changing the model rebuilds the server container.")
        self.reconnect_after_capture_checkbox = QCheckBox(
            "Reconnect immediately after each capture")
        self.reconnect_after_capture_checkbox.setToolTip(
            "Keeps a connection warm so the next dictation starts instantly. Turn "
            "off to connect only when you dictate, which avoids loading a model "
            "per capture on a server that does not share one.")

        self.vram_yield_apps_edit = QLineEdit()
        self.vram_yield_apps_edit.setPlaceholderText(
            "e.g. eldenring.exe, cs2.exe  —  leave empty to never stand down")
        self.vram_yield_apps_edit.setToolTip(
            "While any of these are running, WhisperBoard stops the server so it "
            "is not holding GPU memory, and starts it again when they exit.\n"
            "The .exe is optional. Starting the server from the tray overrides "
            "this until the app closes.")

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
        connection_column.addWidget(self.final_pass_checkbox)
        form_layout.addRow(QLabel("Connection Mode:"), connection_column)
        server_start_column = QVBoxLayout()
        server_start_column.setContentsMargins(0, 0, 0, 0)
        server_start_column.addWidget(self.auto_start_server_checkbox)
        server_start_column.addWidget(self.start_docker_desktop_checkbox)
        server_start_column.addWidget(self.server_use_gpu_checkbox)
        server_start_column.addWidget(self.share_one_model_checkbox)
        form_layout.addRow(QLabel("Server Startup:"), server_start_column)
        type_row = QHBoxLayout()
        type_row.setContentsMargins(0, 0, 0, 0)
        type_row.addWidget(self.capture_font_family_combo, 1)
        type_row.addSpacing(8)
        type_row.addWidget(self.capture_font_size_spin)
        form_layout.addRow(QLabel("Capture Text:"), type_row)
        appearance_row = QHBoxLayout()
        appearance_row.setContentsMargins(0, 0, 0, 0)
        appearance_row.addWidget(self.capture_opacity_spin)
        appearance_row.addSpacing(8)
        appearance_row.addWidget(self.capture_bg_color_button)
        appearance_row.addSpacing(8)
        appearance_row.addWidget(self.capture_panel_frost_checkbox)
        appearance_row.addStretch()
        appearance_row.addWidget(self.capture_preview_button)
        form_layout.addRow(QLabel("Panel:"), appearance_row)
        field_row = QHBoxLayout()
        field_row.setContentsMargins(0, 0, 0, 0)
        field_row.addWidget(self.capture_field_opacity_spin)
        field_row.addSpacing(8)
        field_row.addWidget(self.capture_field_color_button)
        field_row.addSpacing(8)
        field_row.addWidget(self.capture_field_frost_checkbox)
        field_row.addStretch()
        form_layout.addRow(QLabel("Transcript Field:"), field_row)
        frost_row = QHBoxLayout()
        frost_row.setContentsMargins(0, 0, 0, 0)
        for spin in (self.capture_blur_spin, self.capture_saturation_spin,
                     self.capture_brightness_spin, self.capture_levelling_spin):
            frost_row.addWidget(spin)
            frost_row.addSpacing(4)
        frost_row.addStretch()
        form_layout.addRow(QLabel("Frost:"), frost_row)
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.addWidget(self.profile_combo)
        profile_row.addSpacing(8)
        profile_row.addWidget(self.profile_save_button)
        profile_row.addWidget(self.profile_delete_button)
        profile_row.addStretch()
        form_layout.addRow(QLabel("Profile:"), profile_row)
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
                       self.capture_font_family_combo.currentFontChanged,
                       self.capture_meter_style_combo.currentIndexChanged):
            signal.connect(lambda *_: self._sync_preview())
        # These change the snapshot itself rather than what is painted over
        # it, so they need it taken again.
        for signal in (self.capture_panel_frost_checkbox.toggled,
                       self.capture_field_frost_checkbox.toggled,
                       self.capture_blur_spin.valueChanged,
                       self.capture_saturation_spin.valueChanged,
                       self.capture_brightness_spin.valueChanged,
                       self.capture_levelling_spin.valueChanged):
            signal.connect(lambda *_: self._sync_preview(regrab=True))
        meter_row = QHBoxLayout()
        meter_row.setContentsMargins(0, 0, 0, 0)
        meter_row.addWidget(self.capture_meter_style_combo)
        meter_row.addWidget(self.meter_preview_panel)
        meter_row.addWidget(self.meter_test_button)
        meter_row.addStretch()
        form_layout.addRow(QLabel("Level Meter:"), meter_row)

        form_layout.addRow(QLabel("Release GPU for:"), self.vram_yield_apps_edit)

        layout.addLayout(form_layout)
        layout.addWidget(self.launch_on_startup_checkbox)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.status_label)
        button_layout.addStretch()
        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        # Connections
        self.capture_meter_style_combo.currentTextChanged.connect(self.meter_preview.set_style)
        # The preview is clickable like the real one; keep the dropdown in step
        # so the two never disagree about what is selected.
        self.meter_preview.style_changed.connect(self.capture_meter_style_combo.setCurrentText)
        self.meter_test_button.toggled.connect(self._on_preview_toggled)
        self.auto_start_server_checkbox.toggled.connect(self._sync_server_startup_enabled)
        self.profile_combo.activated.connect(self._on_profile_chosen)
        self.profile_save_button.clicked.connect(self._save_profile)
        self.profile_delete_button.clicked.connect(self._delete_profile)
        self.apply_button.clicked.connect(self.apply_settings)
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.close)

        self._refresh_profiles()
        self.load_settings()

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
        """Fill the form in from the chosen profile.

        Loaded into the window and no further: a profile that took effect the
        instant it was picked would give no way to look at one without
        adopting it. Apply is still what commits.
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
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        merged.update(settings)
        self.load_settings(merged)
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
        settings = self._collect_settings()
        if settings is None:
            return
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

    def _set_bg_color(self, color: str):
        self._bg_color = self._paint_swatch(
            self.capture_bg_color_button, color, DEFAULT_BG_COLOR)
        self._sync_preview()

    def _set_field_color(self, color: str):
        self._field_color = self._paint_swatch(
            self.capture_field_color_button, color, DEFAULT_FIELD_COLOR)
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
        box.set_meter_style(self.capture_meter_style_combo.currentText())
        box.set_surface(self._bg_color,
                        self.capture_opacity_spin.value() / 100.0,
                        self.capture_panel_frost_checkbox.isChecked())
        box.set_field(self._field_color,
                      self.capture_field_opacity_spin.value() / 100.0,
                      self.capture_field_frost_checkbox.isChecked())
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
        self.final_pass_checkbox.setChecked(settings.get("final_pass", DEFAULT_SETTINGS["final_pass"]))
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
        self.capture_font_size_spin.setValue(
            int(settings.get("capture_font_size", DEFAULT_SETTINGS["capture_font_size"])))
        self._set_font_family(settings.get(
            "capture_font_family", DEFAULT_SETTINGS["capture_font_family"]))
        self.capture_opacity_spin.setValue(int(round(float(
            settings.get("capture_opacity", DEFAULT_SETTINGS["capture_opacity"])) * 100)))
        self._set_bg_color(settings.get("capture_bg_color", DEFAULT_SETTINGS["capture_bg_color"]))
        self.capture_field_opacity_spin.setValue(int(round(float(
            settings.get("capture_field_opacity",
                         DEFAULT_SETTINGS["capture_field_opacity"])) * 100)))
        self._set_field_color(
            settings.get("capture_field_color", DEFAULT_SETTINGS["capture_field_color"]))
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
        self.capture_meter_style_combo.setCurrentText(meter_style)

        # The clean state to measure edits against. Read back off the form
        # rather than kept from the file: a config missing a key, or holding
        # one this window does not edit, would otherwise read as dirty the
        # moment it opened.
        if from_disk:
            self._applied_settings = self._collect_settings(validate=False)

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
            "final_pass": self.final_pass_checkbox.isChecked(),
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
            "capture_meter_style": self.capture_meter_style_combo.currentText(),
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
