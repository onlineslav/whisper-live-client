# AI Agent Prompt: Build the WhisperType Application

## High-Level Objective

You are an expert Python developer specializing in building sleek, modern Windows desktop applications. Your task is to build **WhisperType**, a hotkey-driven dictation utility. You will write the complete, runnable source code for this application based on the detailed specifications below.

## Core User Story

As a user, I want to press a global hotkey (e.g., `Ctrl+`` `) to make a small transcription box appear by my cursor. As I speak, my words will be transcribed into this box in real-time. When I'm done, I can press `Enter` to have the text instantly pasted into whatever application I was using, or press `Esc` to dismiss it. The application should be lightweight, responsive, and feel like a native part of Windows.

---

## System Architecture & Components

The application consists of a **Python client** that communicates with a separate **`whisper-live` server** (assumed to be running in a Docker container). You will build the client application, which has the following components:

1.  **Main Application (`src/main.py`):** The core of the application, running a `PySide6` event loop. It initializes and orchestrates all other components.
2.  **System Tray Icon:** The application's main interface. It provides status feedback (Connected, Recording, Error) and a context menu to access settings or exit the program.
3.  **Capture Box (`src/capture_box.py`):** A borderless, semi-transparent `PySide6` window that appears on hotkey press. It displays live transcription text and has confirm/cancel buttons. It should be minimalist and visually appealing.
4.  **Settings Window (`src/settings_window.py`):** A simple UI to configure the server address, hotkey, and startup behavior. Settings are persisted in a `config.json` file.
5.  **WebSocket Client (`src/websocket_client.py`):** Manages the WebSocket connection to the `whisper-live` server. It streams audio data and receives transcription events, handling connection state and reconnection logic.
6.  **Audio Capturer (`src/audio_capture.py`):** Uses `pyaudio` to capture microphone input to be streamed by the WebSocket client.
7.  **Hotkey Listener (`src/hotkey_listener.py`):** Uses `pynput` to listen for the global hotkey combination and trigger the capture sequence.

---

## Technical Stack

You **must** use the following libraries:
- **UI:** `PySide6`
- **Global Hotkeys:** `pynput`
- **WebSockets:** `websockets`
- **Audio:** `pyaudio`

---

## Implementation Plan

Follow the detailed checklist in the `todo.md` file. The general order of operations is:
1.  **Set up the project structure** with a `src` directory and `requirements.txt`.
2.  **Build the skeleton:** Implement the main application loop and the system tray icon with its menu.
3.  **Implement the peripheral components:** Code the Settings window, Hotkey listener, WebSocket client, and Audio capturer as separate, modular classes.
4.  **Build the core UI:** Create the Capture Box UI.
5.  **Integrate everything:** Connect all the signals and slots. The hotkey should trigger the capture box, which in turn starts the audio streaming via the WebSocket client. Text received from the server is displayed in the capture box. Confirming pastes the text and logs it to a history file.
6.  **Refine and Finalize:** Add icons, error handling, and documentation.

## UI/UX Design Guidelines

- **Style:** Adhere to modern Windows UI principles. Use `PySide6` to create borderless, semi-transparent windows (e.g., `Qt.FramelessWindowHint`, `setAttribute(Qt.WA_TranslucentBackground)`).
- **Responsiveness:** The application must feel fast. The Capture Box should appear instantly.
- **User Feedback:** Use the tray icon's state to clearly communicate the application's status. Provide clear error messages if the server connection fails.
- **Intuitive Controls:** The `Enter` for confirm and `Esc` for cancel behaviors are critical for a smooth user experience. Clicking away must *not* cancel: the capture keeps recording (the box fades and says so) so the user can glance at another window mid-dictation, and the hotkey finishes it from anywhere.

## Final Deliverable

The output should be a complete set of Python source files in the `src/` directory. The code should be well-commented, follow PEP 8 standards, include type hints, and be organized into logical classes and modules as described above. The application should be runnable by executing `python src/main.py` after installing the dependencies from `requirements.txt`.
