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
2.  **A running `whisper-live` server instance.** This project acts as a client. You must have an instance of the [collabora/WhisperLive](https://github.com/collabora/WhisperLive) server running. The easiest way is via Docker Desktop (see instructions below).

## Run the WhisperLive server (Docker)

**WhisperBoard can do this for you.** By default it starts the server itself at
launch: it checks whether anything is already listening, starts Docker Desktop
if the daemon is down, pulls the image if it is missing, and runs the container
as `whisperboard-server` with `--restart unless-stopped`. The tray icon reports
each step, so a first run that spends ten minutes downloading a 3 GB image
looks like progress rather than a hang.

Turn it off in **Settings → Server Startup** if you would rather manage the
server yourself; **Start Server** and **Stop Server** stay in the tray menu
either way. It is skipped entirely when the server address points at another
machine.

To run the server by hand instead:

1. Install **Docker Desktop** if you do not have it already.
2. Run **ONE** of these commands in a terminal and leave it running:

   **CPU:**
   ```bash
   docker run -it -p 9090:9090 ghcr.io/collabora/whisperlive-cpu:latest
   ```

   **NVIDIA GPU:**
   ```bash
   docker run -it --gpus all -p 9090:9090 ghcr.io/collabora/whisperlive-gpu:latest
   ```

   The `ghcr.io/collabora/whisperlive-*` images load the Whisper model once and share it across all connections by default. (Pass `--no_single_model` to `run_server.py` only if you specifically want per-client model loading — strongly not recommended for desktop dictation.)

## Model selection

WhisperBoard defaults to `distil-small.en`, which gives the best balance of accuracy, speed, and low hallucination rate for English dictation on a typical desktop CPU.

Pick one from the **Settings → Model** dropdown — it is a list rather than a
text field, because a mistyped model name fails silently: the server rejects it
and the capture box simply never fills in. A custom model set by hand in
`config.json` is kept and shown in the list as "custom".

Measured on an RTX 3060 Ti, streaming 10s of speech at 1x realtime: every model
below finished transcribing *before the speaker stopped*, and time-to-first-word
varied by under 0.15s across the whole range — that latency is set by the
server's chunk cadence, not by the model. What model size actually costs is the
per-connection load (0.4s for `tiny.en` up to 3.3s for `large-v3-turbo`), which
WhisperLive pays on every connection. On a GPU there is little reason to choose
a small model.

Other good options:

| Model | Hardware | Notes |
|---|---|---|
| `distil-small.en` | CPU (default) | Trained to suppress hallucinations; good accuracy for short dictation |
| `small.en` | CPU | Slightly more accurate than distil, slightly more prone to filler hallucinations |
| `distil-medium.en` | CPU (8GB+ RAM) or GPU | Near-medium accuracy, lower hallucination rate |
| `large-v3-turbo` | GPU | Best accuracy and lowest latency on modern NVIDIA GPUs |
| `tiny.en` / `base.en` | Low-end | Avoid for dictation — hallucinate "Okay", "Thanks for watching", etc. on short utterances |

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
2.  **Check the Status**: The tray icon's colour is the quickest read —

    | Colour | Meaning |
    |---|---|
    | Amber | Starting the server: Docker Desktop, image download, or container start |
    | Blue | Connecting, or waiting for the server to finish loading its model |
    | Green | Ready to dictate |
    | Red (bright) | Recording |
    | Dark red | Something is wrong; the tooltip says what |
    | Grey | Not connected, and nothing is starting the server |

    Hover for the detail and elapsed time, left-click for the same as a
    notification, or right-click for the full status and the server controls.
    "Ready" means the server has confirmed its model is loaded — not merely
    that the socket opened, which is why a cold start shows "Loading model"
    for a while first.
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
