# Project Specification: WhisperBoard

*A sleek, hotkey-driven dictation utility for Windows that captures speech, transcribes it via a `whisper-live` server, and inserts the text at your cursor.*

---

## 1. User Experience (UX) Philosophy

- **Seamless & Unobtrusive**: The application should feel like a natural extension of the operating system, not a clunky add-on. It should appear when needed and vanish when not, without disrupting workflow.
- **Focused & Effortless**: The primary interaction loop (hotkey -> speak -> insert) must be rapid and require minimal cognitive load.
- **Modern & Integrated**: The UI will adopt modern Windows design principles (e.g., Fluent Design) for a clean, professional aesthetic that feels at home on the platform.

---

## 2. Core UX Flow: "The Capture Loop"

1.  **Invocation**: The user presses a global **Capture Hotkey** (e.g., `Ctrl+`` `). This hotkey is active system-wide.
2.  **Capture Box Appears**: A minimalist **Capture Box** UI element instantly appears near the user's text cursor (caret).
3.  **Live Transcription**: The application immediately begins streaming microphone audio to the `whisper-live` server. As transcription results are received, they appear inside the Capture Box in real-time.
4.  **Confirmation or Cancellation**:
    *   **To Confirm**: The user presses the **Check (✓) button** or hits the **`Enter` key**. The Capture Box disappears, the final transcribed text is copied to the clipboard, and a `Ctrl+V` (paste) command is issued to insert the text at the original cursor location.
    *   **To Cancel**: The user presses the **Cancel (X) button**, hits the **`Esc` key**, or **clicks anywhere outside** the Capture Box. The Capture Box disappears, and the transcribed text is discarded (but still logged, see section 4.2).

---

## 3. User Interface (UI) Components

### 3.1. The Capture Box

This is the primary interaction window.

- **Appearance**:
    - A small, borderless window with rounded corners.
    - Semi-transparent background (e.g., Acrylic/Mica effect) to let the underlying application show through subtly.
    - Displays the live transcribed text.
    - Contains two clean, icon-based buttons on the right or bottom: `✓` and `X`.
- **Behavior**:
    - Appears near the text caret, positioned intelligently to avoid obscuring the text being edited.
    - Fades in and out smoothly.
    - Listens for `Enter` and `Esc` key presses to confirm or cancel.

### 3.2. System Tray Icon

The application's permanent home is the system tray.

- **Icon States**: The icon will change color or design to provide at-a-glance feedback:
    - **Green/Default**: Connected to the server and ready.
    - **Yellow/Spinning**: Attempting to connect to the server.
    - **Blue/Recording**: Actively listening and transcribing.
    - **Red/Error**: Connection to the server is lost or has failed.
- **Context Menu**: Right-clicking the tray icon reveals:
    - `Status: Connected / Disconnected` (A non-interactive label).
    - `Open Settings` (Opens the Settings Window).
    - `View Transcription History` (Opens the backup log file in the default text editor).
    - `---` (Separator)
    - `Exit` (Closes the application).

### 3.3. Settings Window

A clean, simple UI for configuration.

- **Server Address**: Input field for the `ws://` or `wss://` URL of the `whisper-live` Docker container.
- **Capture Hotkey**: A control to let the user define and set their preferred hotkey combination.
- **Launch on Startup**: A checkbox to enable/disable the application from starting with Windows.
- **Theme**: Dropdown or radio buttons for `Light / Dark / Follow System`.

---

## 4. Features & Logic

### 4.1. Client-Server Communication

- The client establishes a persistent WebSocket connection to the `whisper-live` server specified in the settings.
- It will automatically attempt to reconnect with an exponential backoff strategy if the connection is lost.
- The client streams raw microphone audio data in the format expected by the `whisper-live` server.

### 4.2. Transcription History

- All confirmed transcriptions (text that is pasted) are appended to a local log file (e.g., `transcription_history.log`) in a user-accessible directory.
- This provides a backup and allows the user to retrieve previously dictated text. Each entry should be timestamped.

### 4.3. Text Insertion

- To ensure maximum compatibility across all Windows applications, text insertion will be performed by:
    1.  Storing the final text in the system clipboard.
    2.  Programmatically sending a `Ctrl+V` key combination.
    3.  (Optional) Restoring the clipboard to its previous state to be non-destructive.

---

## 5. Proposed Technical Stack

- **Language**: Python
- **UI Framework**: **PySide6**. It provides the necessary capabilities for modern, borderless, and transparent windows, and has excellent support for system tray integration.
- **Client-Server**: `websockets` library for handling WebSocket communication.
- **Audio Input**: `pyaudio` for capturing microphone data.
- **Global Hotkeys**: `pynput` for reliably listening for the global capture hotkey.
- **Packaging**: `PyInstaller` or `Nuitka` for bundling the application and its dependencies into a single distributable `.exe` file.
