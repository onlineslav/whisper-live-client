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
)
from PySide6.QtCore import Signal
from PySide6.QtGui import QKeySequence

CONFIG_FILE = "config.json"

DEFAULT_SETTINGS = {
    "server_address": "ws://localhost:9090",
    "capture_hotkey": "ctrl+`",
    "launch_on_startup": False,
    "model": "tiny.en",
    "connect_on_demand": True,
}


class SettingsWindow(QWidget):
    settings_saved = Signal(dict)
    window_closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("WhisperBoard Settings")

        # UI Elements
        self.server_address_edit = QLineEdit()
        self.capture_hotkey_edit = QKeySequenceEdit()
        self.model_edit = QLineEdit()
        self.launch_on_startup_checkbox = QCheckBox("Launch WhisperBoard on system startup")
        self.connect_on_demand_checkbox = QCheckBox("Connect to server only when capture starts (on-demand)")

        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")

        # Layout
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        form_layout.addRow(QLabel("Server Address:"), self.server_address_edit)
        form_layout.addRow(QLabel("Capture Hotkey:"), self.capture_hotkey_edit)
        form_layout.addRow(QLabel("Model (e.g., tiny.en):"), self.model_edit)
        form_layout.addRow(QLabel("Connection Mode:"), self.connect_on_demand_checkbox)

        layout.addLayout(form_layout)
        layout.addWidget(self.launch_on_startup_checkbox)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        # Connections
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.close)

        self.load_settings()

    def get_config_path(self):
        return CONFIG_FILE

    def load_settings(self):
        path = self.get_config_path()
        if os.path.exists(path):
            with open(path, "r") as f:
                settings = json.load(f)
        else:
            settings = DEFAULT_SETTINGS

        self.server_address_edit.setText(settings.get("server_address", DEFAULT_SETTINGS["server_address"]))
        capture_hotkey = settings.get("capture_hotkey", DEFAULT_SETTINGS["capture_hotkey"])
        self.capture_hotkey_edit.setKeySequence(QKeySequence(self._display_hotkey(capture_hotkey)))
        self.model_edit.setText(settings.get("model", DEFAULT_SETTINGS["model"]))
        self.launch_on_startup_checkbox.setChecked(settings.get("launch_on_startup", DEFAULT_SETTINGS["launch_on_startup"]))
        self.connect_on_demand_checkbox.setChecked(settings.get("connect_on_demand", DEFAULT_SETTINGS["connect_on_demand"]))

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
