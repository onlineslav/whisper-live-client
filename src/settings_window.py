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
        self.model_edit = QLineEdit()
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

        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")

        # Layout
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        form_layout.addRow(QLabel("Server Address:"), self.server_address_edit)
        form_layout.addRow(QLabel("Capture Hotkey:"), self.capture_hotkey_edit)
        form_layout.addRow(QLabel("Model (e.g., distil-small.en):"), self.model_edit)
        form_layout.addRow(QLabel("Connection Mode:"), self.connect_on_demand_checkbox)
        form_layout.addRow(QLabel("Capture Text Size:"), self.capture_font_size_spin)
        meter_row = QHBoxLayout()
        meter_row.setContentsMargins(0, 0, 0, 0)
        meter_row.addWidget(self.capture_meter_style_combo)
        meter_row.addWidget(self.meter_preview_panel)
        meter_row.addWidget(self.meter_test_button)
        meter_row.addStretch()
        form_layout.addRow(QLabel("Level Meter:"), meter_row)

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
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.close)

        self.load_settings()

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
        self.model_edit.setText(settings.get("model", DEFAULT_SETTINGS["model"]))
        self.launch_on_startup_checkbox.setChecked(settings.get("launch_on_startup", DEFAULT_SETTINGS["launch_on_startup"]))
        self.connect_on_demand_checkbox.setChecked(settings.get("connect_on_demand", DEFAULT_SETTINGS["connect_on_demand"]))
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
            "model": self.model_edit.text() or DEFAULT_SETTINGS["model"],
            "connect_on_demand": self.connect_on_demand_checkbox.isChecked(),
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
