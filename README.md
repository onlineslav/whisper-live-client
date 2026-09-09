# WhisperType

WhisperType is a sleek, hotkey-driven dictation utility for Windows. It captures your speech, transcribes it in real-time using a `whisper-live` server, and pastes the text directly at your cursor, integrating dictation into any application.

## Features

- **Hotkey Activated**: Press a global hotkey to instantly start or stop dictating.
- **Live Transcription**: A minimalist "Capture Box" appears, showing your transcribed text as you speak.
- **Paste at Cursor**: Confirmed transcriptions are automatically pasted into whatever application you're using.
- **Shows You the Target**: The place the text will land is marked for the length of the capture, so you can see where you are dictating before you speak.
- **Live Typing**: The words go into the document as you say them, correcting themselves in place as the transcription firms up.
- **System Tray Control**: Runs unobtrusively in the system tray, providing status and access to settings.
- **Configurable**: Easily change the server address and hotkey.
- **Transcription History**: Automatically saves a log of all your transcriptions.

## Prerequisites

1.  **Python 3.8+**
2.  **A running `whisper-live` server instance.** This project acts as a client. You must have an instance of the [collabora/WhisperLive](https://github.com/collabora/WhisperLive) server running. The easiest way is via Docker Desktop (see instructions below).

## Run the WhisperLive server (Docker)

**WhisperType can do this for you.** By default it starts the server itself at
launch: it checks whether anything is already listening, starts Docker Desktop
if the daemon is down, pulls the image if it is missing, and runs the container
as `whispertype-server` with `--restart unless-stopped`. The tray icon reports
each step, so a first run that spends ten minutes downloading a 3 GB image
looks like progress rather than a hang.

Upgrading from the old name: the container it used to create is removed on
the first start, because it would otherwise still be holding the port. The
cache of downloaded model weights is kept and reused where it is, so nothing
is downloaded twice.

By default it also passes the chosen model to the server as a local path,
which switches WhisperLive into **single model mode**: one model, loaded once
and shared by every connection. Left to itself the server loads a *separate*
model for each connection and closes the socket after every dictation, so one
capture is one model instance — measured at 7.1 GB of resident models on an
8 GB card, at which point the GPU fills up and everything slows down (`tiny.en`
went from 1.27s to 5.03s to first word). Sharing one model removes both that
and the per-connection load. The cost is that the model is fixed for the life
of the container, so changing it in Settings rebuilds the container — a few
seconds, with the weights already cached.

**Release GPU for** takes a list of programs — your games, say. While any of
them is running, WhisperType stops the server so it is not holding GPU memory,
and starts it again when they exit. Starting the server from the tray menu
overrides this until that program closes.

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

WhisperType defaults to `distil-small.en`, which gives the best balance of accuracy, speed, and low hallucination rate for English dictation on a typical desktop CPU.

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
    - A **WhisperType** icon will appear in your system tray.
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
4.  **Check where it is going**: The place the text will be pasted is marked
    as the box opens, and stays marked until it lands. How precise the mark is
    tells you how precisely WhisperType knows —

    | What you see | What it means |
    |---|---|
    | A bar at the text caret | The exact insertion point was found |
    | An outline around a field or control | That control has focus; the text goes into it |
    | A dashed outline round a window, with its name | Only the window is known, not the spot in it |
    | Everything turns grey | The target window has gone away |

    A dashed outline is worth a second look before speaking: it usually means
    focus is not in a text field at all. The mark is also why the Capture Box
    now opens beside your caret rather than beside the mouse. Both behaviours
    are under **Settings → Paste Target**.
5.  **Speak**: Where an exact caret was found, the words are typed **straight
    into the document** as you say them, correcting themselves in place as the
    transcription firms up — and the Capture Box slims to just its level meter
    and buttons, so it is not saying the same thing twice over the top of your
    work. The caret stays in your document the whole time; the box never takes
    the keyboard.

    Cancelling removes everything it typed. On confirm, if the finished
    transcript is just what is already on screen plus the trailing space, the
    typed words stay exactly where they are and only the tail is added —
    nothing is deleted, and the text never passes through the clipboard. When
    the final pass has actually re-punctuated the capture, the preview comes
    out and the finished transcript is pasted in its place, as before.

    Two things to know. **Undo works, in one press**: `Ctrl+Z` after a
    dictation puts the document back exactly as it was. Pressing it a second
    time is where it gets untidy — instead of continuing further back, it
    brings the half-finished versions of your sentence back for a few presses
    before returning to where the first press had already got you. And an
    editor with aggressive autocorrect can fight the inserted text.

    Turn it off with **Settings → Paste Target → Type the words into the app as
    you speak**, and the full transcript box comes back.

    Everywhere else — no caret found — the box keeps its transcript area and
    behaves as it always did. Live typing never runs without a confirmed text
    caret, and it stands down by itself in an application that turns out not to
    accept the text.
6.  **Confirm or Cancel**:
    - To **confirm** and paste the text, press the hotkey again, press `Enter`, or click the "✓" button.
    - To **cancel**, press `Esc`, click the "✗" button, or simply click away from the Capture Box.

    While live typing is running the box deliberately does not hold the
    keyboard — your document does — so `Enter` and `Esc` are claimed from the
    desktop for the length of the capture and are swallowed rather than
    reaching the document. Held modifiers hand the key back, so `Shift+Enter`
    is still a newline. Clicking away does not cancel then either: you are
    meant to be clicking around in your own document while it types there.
    `Enter` pressed while the box is still reporting on the connection cancels
    rather than confirms, since there is no transcript in it yet.

    The text in the box is a live preview. On confirm, WhisperType sends the
    whole capture again in one piece and pastes that instead — which is why the
    pasted text is sometimes punctuated a little differently from what you
    watched appear. Live transcription reads a rolling buffer and discards
    audio as it goes, so a pause mid-sentence can come out as two sentences;
    reading the capture whole does not have that problem. It costs up to a
    second before the paste lands. Turn it off with **Settings → Connection
    Mode → Re-transcribe the whole capture before pasting** if you would rather
    have the text immediately.

## Building the Executable (Optional)

You can bundle WhisperType into a single `.exe` file for easy distribution using `PyInstaller`.

1.  **Install PyInstaller:**
    ```bash
    pip install pyinstaller
    ```

2.  **Build the Executable:**
    A `WhisperType.spec` file is included in the repository. Run PyInstaller with this file:
    ```bash
    pyinstaller WhisperType.spec
    ```

3.  **Find the Executable:**
    The final executable, `WhisperType.exe`, will be located in the `dist` directory.
