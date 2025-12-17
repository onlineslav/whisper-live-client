# WhisperBoard

WhisperBoard is a sleek, hotkey-driven dictation utility for Windows. It captures your speech, transcribes it in real-time using a `whisper-live` server, and pastes the text directly at your cursor, integrating dictation into any application.

## Features

- **Hotkey Activated**: Press a global hotkey to instantly start or stop dictating.
- **Live Transcription**: A minimalist "Capture Box" appears, showing your transcribed text as you speak.
- **Paste at Cursor**: Confirmed transcriptions are automatically pasted into whatever application you're using.
- **System Tray Control**: Runs unobtrusively in the system tray, providing status and access to settings.
- **Configurable**: Easily change the server address and hotkey.
- **Transcription History**: Automatically saves a log of all your transcriptions.

## Prerequisites

1.  **Python 3.8+**
2.  **A running `whisper-live` server instance.** This project acts as a client. You must have an instance of the [collabora/WhisperLive](https://github.com/collabora/WhisperLive) server running. The easiest way is via Docker.

## Setup Instructions

1.  **Clone the Repository:**
    ```bash
    git clone <repository_url>
    cd whisper-live-client
    ```

2.  **Install Dependencies:**
    It is highly recommended to use a virtual environment.
    ```bash
    # Create a virtual environment
    python -m venv .venv
    # Activate it (Windows)
    .venv\Scripts\activate
    
    # Install required packages
    pip install -r requirements.txt
    ```

3.  **Run and Configure:**
    Run the application for the first time:
    ```bash
    python src/main.py
    ```
    - A **WhisperBoard** icon will appear in your system tray.
    - Right-click the icon and select **Settings**.
    - In the **Server Address** field, enter the WebSocket URL of your running `whisper-live` server (e.g., `ws://localhost:9090`).
    - Click **Save**.
    - **Restart the application** for the new settings to take effect.

## Usage

1.  **Start the application**:
    ```bash
    python src/main.py
    ```
2.  **Check the Status**: Right-click the tray icon. The status should change from "Connecting" to "Connected".
3.  **Press the Hotkey**: Press the configured hotkey (`Ctrl+\` by default) to make the Capture Box appear.
4.  **Speak**: The box will show "Listening...". As you speak, the transcribed text will appear.
5.  **Confirm or Cancel**:
    - To **confirm** and paste the text, press the hotkey again, press `Enter`, or click the "✓" button.
    - To **cancel**, press `Esc`, click the "✗" button, or simply click away from the Capture Box.

## Building the Executable (Optional)

You can bundle WhisperBoard into a single `.exe` file for easy distribution using `PyInstaller`.

1.  **Install PyInstaller:**
    ```bash
    pip install pyinstaller
    ```

2.  **Build the Executable:**
    A `WhisperBoard.spec` file is included in the repository. Run PyInstaller with this file:
    ```bash
    pyinstaller WhisperBoard.spec
    ```

3.  **Find the Executable:**
    The final executable, `WhisperBoard.exe`, will be located in the `dist` directory.
