import copy
import json
import os
from PySide6.QtWidgets import (
    QWidget,
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
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence

from signal_meter import SignalMeter

from capture_box import (
    DEFAULT_FONT_SIZE_PX,
    MIN_FONT_SIZE_PX,
    MAX_FONT_SIZE_PX,
    METER_STYLES,
)

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

DEFAULT_SETTINGS = {
    "server_address": "ws://localhost:9090",
    "capture_hotkey": "ctrl+`",
    "launch_on_startup": False,
    "model": "distil-small.en",
    "connect_on_demand": True,
    "capture_font_size": DEFAULT_FONT_SIZE_PX,
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
        self.capture_font_size_spin = QSpinBox()
        self.capture_font_size_spin.setRange(MIN_FONT_SIZE_PX, MAX_FONT_SIZE_PX)
        self.capture_font_size_spin.setSuffix(" px")
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

        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")

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
        server_start_column = QVBoxLayout()
        server_start_column.setContentsMargins(0, 0, 0, 0)
        server_start_column.addWidget(self.auto_start_server_checkbox)
        server_start_column.addWidget(self.start_docker_desktop_checkbox)
        server_start_column.addWidget(self.server_use_gpu_checkbox)
        server_start_column.addWidget(self.share_one_model_checkbox)
        form_layout.addRow(QLabel("Server Startup:"), server_start_column)
        form_layout.addRow(QLabel("Capture Text Size:"), self.capture_font_size_spin)
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
        button_layout.addStretch()
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
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.close)

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

    def _sync_server_startup_enabled(self):
        """The sub-options only mean anything when auto-start is on."""
        enabled = self.auto_start_server_checkbox.isChecked()
        self.start_docker_desktop_checkbox.setEnabled(enabled)
        self.server_use_gpu_checkbox.setEnabled(enabled)
        # Sharing one model is done by how the container is launched, so it is
        # only ours to arrange when we launch it.
        self.share_one_model_checkbox.setEnabled(enabled)

    def set_preview_level_db(self, db: float):
        """Feed the preview meter, while the app has the mic open for it."""
        self.meter_preview.set_level_db(db)

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

    def load_settings(self):
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
        meter_style = settings.get("capture_meter_style", DEFAULT_SETTINGS["capture_meter_style"])
        if meter_style not in METER_STYLES:
            meter_style = DEFAULT_SETTINGS["capture_meter_style"]
        self.capture_meter_style_combo.setCurrentText(meter_style)

    def save_settings(self):
        sequence = self.capture_hotkey_edit.keySequence()
        hotkey_str = self._normalize_hotkey(sequence)

        modifier_keys = {'ctrl', 'alt', 'shift', 'cmd'}
        keys = set(part.strip('<>').strip().lower() for part in hotkey_str.split('+') if part.strip())
        if not hotkey_str or not keys or keys.issubset(modifier_keys):
            QMessageBox.warning(self, "Invalid Hotkey", "Please press a hotkey that includes at least one non-modifier key (e.g., Ctrl+Shift+` or Alt+F1).")
            return

        # Check if pynput can parse it
        try:
            from pynput import keyboard
            keyboard.HotKey.parse(hotkey_str)
        except Exception as e:
            QMessageBox.warning(self, "Invalid Hotkey", f"The hotkey '{hotkey_str}' could not be parsed. Please check the format.\n\nError: {e}")
            return

        path = self.get_config_path()
        settings = {
            "server_address": self.server_address_edit.text(),
            "capture_hotkey": hotkey_str,
            "launch_on_startup": self.launch_on_startup_checkbox.isChecked(),
            "model": self.model_combo.currentData() or DEFAULT_SETTINGS["model"],
            "connect_on_demand": self.connect_on_demand_checkbox.isChecked(),
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
            "capture_meter_style": self.capture_meter_style_combo.currentText(),
        }

        try:
            with open(path, "w") as f:
                json.dump(settings, f, indent=4)
            QMessageBox.information(self, "Settings Saved", "Your settings have been saved and applied.")
            self.settings_saved.emit(settings)
            self.close()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save settings: {e}")

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
        # Before the close is announced: leaving the mic open behind a closed
        # settings window would be both a leak and a genuine privacy surprise.
        self.stop_preview()
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
