import json
import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QCheckBox, QFormLayout, QMessageBox
)

CONFIG_FILE = "config.json"

DEFAULT_SETTINGS = {
    "server_address": "ws://localhost:9090",
    "capture_hotkey": "ctrl+`",
    "launch_on_startup": False,
    "model": "tiny.en"
}

class SettingsWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WhisperBoard Settings")
        
        # UI Elements
        self.server_address_edit = QLineEdit()
        self.capture_hotkey_edit = QLineEdit()
        self.model_edit = QLineEdit()
        self.launch_on_startup_checkbox = QCheckBox("Launch WhisperBoard on system startup")
        
        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")

        # Layout
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        
        form_layout.addRow(QLabel("Server Address:"), self.server_address_edit)
        form_layout.addRow(QLabel("Capture Hotkey:"), self.capture_hotkey_edit)
        form_layout.addRow(QLabel("Model (e.g., tiny.en):"), self.model_edit)
        
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
        # In a real app, this might be in %APPDATA%, but for simplicity we use the local dir
        return CONFIG_FILE

    def load_settings(self):
        path = self.get_config_path()
        if os.path.exists(path):
            with open(path, "r") as f:
                settings = json.load(f)
        else:
            settings = DEFAULT_SETTINGS
        
        self.server_address_edit.setText(settings.get("server_address", DEFAULT_SETTINGS["server_address"]))
        self.capture_hotkey_edit.setText(settings.get("capture_hotkey", DEFAULT_SETTINGS["capture_hotkey"]))
        self.model_edit.setText(settings.get("model", DEFAULT_SETTINGS["model"]))
        self.launch_on_startup_checkbox.setChecked(settings.get("launch_on_startup", DEFAULT_SETTINGS["launch_on_startup"]))

    def save_settings(self):
        hotkey_str = self.capture_hotkey_edit.text()

        # --- Hotkey Validation ---
        keys = set(part.strip().lower() for part in hotkey_str.split('+'))
        modifier_keys = {'ctrl', 'alt', 'shift', 'cmd'}
        
        # Check for empty or modifier-only hotkeys
        if not keys or keys.issubset(modifier_keys):
            QMessageBox.warning(self, "Invalid Hotkey", f"The hotkey '{hotkey_str}' is invalid. It must include at least one non-modifier key (e.g., 'a', 'b', 'f1', '`').")
            return

        # Check if pynput can parse it
        try:
            from pynput import keyboard
            keyboard.HotKey.parse(hotkey_str)
        except Exception as e:
            QMessageBox.warning(self, "Invalid Hotkey", f"The hotkey '{hotkey_str}' could not be parsed. Please check the format.\n\nError: {e}")
            return
        # --- End Validation ---

        path = self.get_config_path()
        settings = {
            "server_address": self.server_address_edit.text(),
            "capture_hotkey": hotkey_str,
            "launch_on_startup": self.launch_on_startup_checkbox.isChecked(),
            "model": self.model_edit.text() or DEFAULT_SETTINGS["model"],
        }
        
        try:
            with open(path, "w") as f:
                json.dump(settings, f, indent=4)
            QMessageBox.information(self, "Settings Saved", "Your settings have been saved.\nPlease restart the application for the new hotkey to take effect.")
            self.close()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save settings: {e}")

if __name__ == '__main__':
    # For testing the window independently
    from PySide6.QtWidgets import QApplication
    import sys

    app = QApplication(sys.argv)
    window = SettingsWindow()
    window.show()
    sys.exit(app.exec())
