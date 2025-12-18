# WhisperBoard - To-Do List

This checklist outlines the tasks required to build the WhisperBoard application.

- [x] **1. Project Setup**
    - [x] Create a `src` directory for all Python source code.
    - [x] Create a `requirements.txt` file listing the dependencies: `PySide6`, `pynput`, `websockets`, `pyaudio`.
    - [x] Create the main application entry point: `src/main.py`.

- [x] **2. System Tray Icon & Application Core**
    - [x] In `main.py`, create the main `QApplication` instance.
    - [x] Implement a `QSystemTrayIcon`.
    - [x] Define the icon states (Ready, Connecting, Recording, Error) and have placeholders for the icon images/colors.
    - [x] Build the context menu for the tray icon (`Status`, `Settings`, `View History`, `Exit`).
    - [x] Connect the `Exit` action to quit the application.

- [x] **3. Settings Window**
    - [x] Create a new file `src/settings_window.py`.
    - [x] Design the UI for the settings window (Server Address, Capture Hotkey, Launch on Startup).
    - [x] Implement the logic to save and load settings from a configuration file (e.g., `config.json`).
    - [x] Connect the `Settings` action in the tray menu to open this window.

- [x] **4. Global Hotkey Listener**
    - [x] Create a new file `src/hotkey_listener.py`.
    - [x] Use the `pynput` library to listen for a global hotkey.
    - [x] The hotkey should be configurable via the settings file.
    - [x] When the hotkey is pressed, it should emit a signal to the main application.

- [x] **5. WebSocket Client**
    - [x] Create a new file `src/websocket_client.py`.
    - [x] Implement a class to manage the WebSocket connection to the `whisper-live` server.
    - [x] It should handle connecting, disconnecting, and automatic reconnection attempts.
    - [x] It should have methods to send audio data and receive transcription results.
    - [x] It should emit signals for connection status changes and received messages.

- [x] **6. Audio Capture**
    - [x] Create a new file `src/audio_capture.py`.
    - [x] Use `pyaudio` to capture audio from the default microphone.
    - [x] The audio should be in the format expected by the `whisper-live` server.
    - [x] Implement `start_streaming` and `stop_streaming` methods.

- [x] **7. Capture Box UI**
    - [x] Create a new file `src/capture_box.py`.
    - [x] Design the UI for the Capture Box: a borderless, semi-transparent window.
    - [x] Add a `QLabel` to display transcribed text and two `QPushButton`s for Confirm (✓) and Cancel (X).
    - [x] Implement logic to show the box at the current mouse cursor's position.
    - [x] Connect keyboard shortcuts (`Enter` for Confirm, `Esc` for Cancel) and the away-click-to-cancel behavior.

- [x] **8. Integration**
    - [x] Tie all components together in `main.py`.
    - [x] The hotkey signal should trigger the Capture Box to appear and start audio capture/streaming.
    - [x] The WebSocket client should feed received text into the Capture Box's `QLabel`.
    - [x] The Confirm button should trigger the text-pasting logic (copy to clipboard, simulate `Ctrl+V`).
    - [x] The Cancel button should hide the box and stop the audio stream.
    - [x] Update the tray icon's state based on the WebSocket client's connection status and recording state.

- [x] **9. Transcription History**
    - [x] Implement the logic to append confirmed transcriptions to `transcription_history.log`.
    - [x] Ensure entries are timestamped.
    - [x] Connect the `View History` tray menu action to open this file.

- [ ] **10. Finalization**
    - [x] Create final icons for all states.
    - [x] Write a `README.md` on how to set up and run the application.
    - [ ] Test the application thoroughly.
    - [ ] (Optional) Create a `pyinstaller` spec file to bundle the application into a `.exe`.

## Post-audit follow-ups
- [ ] Fix crash when confirming/cancelling: replace `capture_box.text_label` with `capture_box.text_area` in `src/main.py`.
- [ ] Fix `setWordWrapMode` usage in `src/capture_box.py` (use a proper `QTextOption` wrap mode) so the capture box instantiates cleanly.
- [ ] Add click-away-to-cancel behavior and optional fade-in/out animations for the capture box; prefer caret-aware positioning over mouse-based.
- [ ] Align audio format with WhisperLive expectations (likely 16 kHz mono int16 PCM) and include any required handshake metadata (e.g., sample rate/format).
- [ ] Improve hotkey picker: switch settings hotkey input to `QKeySequenceEdit` and map to the `pynput` string; reload listener after saving.
- [ ] Implement launch-on-startup toggle wiring for Windows (e.g., shortcut in Startup folder or registry entry).
- [ ] Add user feedback when history file is missing; optionally create it on demand.
- [ ] Ensure clean shutdown of WebSocket/audio threads when exiting from tray.
- [ ] Expand message handling to cover alternative WhisperLive schemas (`text`/`segments`/`is_final`) and ignore unexpected payloads gracefully.
- [ ] Add smoke tests/validation runs for connection, capture loop, and paste behavior.
