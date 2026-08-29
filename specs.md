# Project Specification: WhisperBoard

*A sleek, hotkey-driven dictation utility for Windows that captures speech, transcribes it via a `whisper-live` server, and inserts the text at your cursor.*

Last revised: 2026-08-29

---

## 1. Mandate

### 1.1. What this is

WhisperBoard is a **personal daily-driver dictation tool**, built to a standard that would let it be shared later without a rewrite. It is not a product yet; it is a tool that must be good enough to reach for a dozen times a day without friction.

### 1.2. Who it is for

Primarily the author, on one Windows machine, dictating into arbitrary applications — editors, browsers, chat, notes. A future second audience (someone handed the `.exe`) shapes the architecture but does not yet earn distribution work.

### 1.3. Success criteria

The tool succeeds when:

1. **The loop is invisible.** Hotkey → speak → text lands. No thinking about the tool.
2. **It is fast.** Text appears at the cursor within a moment of confirming. Latency is a feature; a correct-but-slow paste is a worse tool.
3. **Nothing is ever lost.** Every transcription is recoverable from the history log, even if the paste misfires.
4. **Failures are legible.** When the mic is silent, the server is down, or the paste lands nowhere, the user is told — never left staring at an empty box.

### 1.4. Non-goals

- Not a transcription *editor* — long-form editing belongs in the destination app.
- Not a meeting recorder, speaker-diarizer, or file transcriber.
- Not cross-platform. Windows-only; the focus and paste layers are deliberately Win32.
- Not a server. WhisperLive runs separately in Docker and is treated as infrastructure.

### 1.5. Design principles

- **Seamless & unobtrusive** — appears when needed, vanishes when not.
- **Focused & effortless** — the capture loop must cost near-zero cognitive load.
- **Fast over perfect** — prefer an immediate paste with a rare clipped word over a guaranteed-complete paste that makes the user wait.
- **Legible failure** — every silent failure mode is a bug.

---

## 2. Current State (2026-08-29)

Implemented and working: system tray with four states and a context menu, settings window with persisted config, global hotkey listener, WebSocket client with reconnect, audio capture with client-side VAD gating, capture box with fade and caret/mouse positioning, transcription history log, launch-on-startup via the registry, PyInstaller spec.

Recently fixed: **confirm was being routed to cancel.** The click-away hit test compared screen-space geometry against widget-local coordinates, so every click read as "outside"; and because an app-wide event filter sees each press first on the `QWindow` (not a `QWidget`), the window-level press cancelled the capture and ate the click before the Confirm button saw it. Separately, `activateWindow()` is denied to a background process, so the box never held keyboard focus and `Enter`/`Esc` went to the app underneath. Both fixed; click-away is now driven by window activation loss, which is the only signal that can observe a click in *another* application.

Known gaps are tracked in `todo.md`.

---

## 3. Core UX Flow: "The Capture Loop"

1. **Invocation** — the user presses the global **Capture Hotkey**. The foreground window handle is recorded *before* the box appears, so the paste can be routed back to it.
2. **Capture Box appears** near the cursor and takes real keyboard focus (forced via `AttachThreadInput`; a plain `activateWindow()` is denied to a background process).
3. **Live transcription** — audio streams to the server; results appear in the box as they arrive.
4. **Confirmation or cancellation**:
   - **Confirm** — `Enter`, the `✓` button, or the hotkey again. The box hides, the text goes to the clipboard, focus is restored to the recorded window, and `Ctrl+V` is issued.
   - **Cancel** — `Esc`, the `✗` button, or switching to another window. The text is discarded but still logged.

### 3.1. Paste timing (decided)

Paste fires promptly after confirm rather than waiting for the server's final post-EOS segment. This keeps the loop fast at the cost of occasionally clipping a trailing word. **The tail is instead protected upstream**, in the audio layer: the VAD hangover window keeps streaming through short pauses and word tails so speech reaches the server before EOS is sent. If clipping persists, the fix is to lengthen the hangover — *not* to add latency after confirm.

---

## 4. User Interface

### 4.1. Capture Box

The primary interaction surface, and the place worth investing in.

- **Appearance** — borderless, rounded, semi-transparent, fades in and out. Text area sized to ~5 visible lines, scrolling beyond that.
- **Behavior** — appears near the caret where obtainable, otherwise at the mouse; clamped to the screen. Takes real keyboard focus. `Enter` confirms, `Esc` cancels, activation loss cancels.
- **To build** — a live input-level meter (see §6.2), editable text before confirming, and true caret-aware positioning (the current `QInputMethod` query only reports carets inside our own process, so in practice it always falls back to the mouse; real caret tracking needs `GetGUIThreadInfo` or UI Automation).

### 4.2. System Tray Icon

- **Icon states** — Ready (green), Connecting (blue), Recording (red), Error (grey).
- **Left click** — opens a status panel showing client state *and* server state (see §4.3). Currently unbound; this is the gap behind "click it and see the status".
- **Right click** — context menu: status label, Settings, View Transcription History, Exit.
- **Balloon notifications** — used for failures the user would otherwise not see, notably "text copied to clipboard but paste could not be delivered".

### 4.3. Status: client vs. server

"Connected" currently means only that the socket opened. The status surface must distinguish:

- **Client** — hotkey registered, audio device open, input level, capture state.
- **Server** — reachable, which model is loaded, whether it finished loading, and round-trip latency.

### 4.4. Settings Window

Grouped into tabs rather than a single flat form. Full reference in §5.

---

## 5. Settings Reference

Stored at `%APPDATA%\WhisperBoard\config.json`. Settings apply live on save; no restart.

### 5.1. Connection

| Setting | Default | Notes |
|---|---|---|
| Server address | `ws://localhost:9090` | |
| Model | `distil-small.en` | **Dropdown, not free text** — a typo currently fails silently |
| Language | `en` | |
| Connection mode | On-demand | Persistent, or connect only when capture starts |
| Test connection | — | Button; reports reachability, loaded model, latency |

### 5.2. Audio *(highest-value additions)*

| Setting | Default | Notes |
|---|---|---|
| Input device | System default | Currently unselectable; user cannot see which mic was grabbed |
| Input level meter | — | Live meter **with the VAD threshold drawn on it** |
| VAD threshold | `0.012` RMS | Hardcoded today. Below it, *nothing streams* — a silent, invisible failure |
| Hangover window | 8 chunks (~0.5 s) | Protects word tails; the lever for clipped endings (§3.1) |
| Pre-roll | 3 chunks (~0.2 s) | Prevents clipped first syllables |
| Max capture duration | 120 s | Stops a forgotten capture streaming forever |
| Auto-stop on silence | Off | Optional end-of-speech auto-confirm |

### 5.3. Text & Paste

| Setting | Default | Notes |
|---|---|---|
| Paste method | Clipboard + `Ctrl+V` | Alternative: direct keystroke typing, for apps that block clipboard paste |
| Restore clipboard | On | Dictation currently destroys clipboard contents |
| Custom vocabulary | Empty | Replacement rules for names and jargon |
| Hallucination filter | Built-in list | Should be user-editable |
| Trailing space / auto-capitalize | Off | |

### 5.4. Behavior & Appearance

| Setting | Default | Notes |
|---|---|---|
| Capture hotkey | `Ctrl+`` ` | |
| Box position | At caret, else mouse | Alternatives: always at mouse, fixed screen corner |
| Cancel on focus loss | On | Some users will want a capture that survives a stray focus steal |
| Theme | Follow system | Specified since day one, still unbuilt |
| Opacity | 0.7 | |

### 5.5. History & Privacy

| Setting | Default | Notes |
|---|---|---|
| Enable history | On | |
| Log cancelled captures | On | Decided: the log is a safety net, so it records everything |
| Retention | Unlimited | Decided: no rotation. Empty entries are skipped |
| Open folder / Clear history | — | Buttons |

### 5.6. System

| Setting | Default | Notes |
|---|---|---|
| Launch on startup | Off | Registry `Run` key |
| Single instance | On | Two copies means two hotkey listeners fighting |
| Log level / Open log file | INFO | See §6.5 |

---

## 6. Features & Logic

### 6.1. Client–server communication

Persistent or on-demand WebSocket to WhisperLive, reconnecting on drop. The server ignores the sample-rate/format/channel handshake fields (it hardcodes 16 kHz mono float32); the VAD and dedup knobs are what actually reduce hallucination on short utterances.

### 6.2. Audio capture and voice gating

16 kHz mono float32, 1024-sample chunks. Client-side VAD gates silence so it never reaches the server — Whisper hallucinates when fed near-silence, and the server's own VAD leaks. Pre-roll and hangover buffers protect the first syllable and the last word.

**This gate is the single most dangerous silent failure in the app**: a quiet microphone streams nothing at all, and the user sees only an empty box. The level meter and threshold control in §5.2 exist to make it visible.

### 6.3. Model choice

Model choice does more for transcription quality than any client-side filtering. `tiny.en` and `base.en` hallucinate filler ("Okay", "Thank you", "Thanks for watching") on short dictation and **must not be defaults**. `distil-small.en` is the baseline; `large-v3-turbo` on a capable GPU.

### 6.4. Text insertion

Clipboard + simulated `Ctrl+V`, preceded by an explicit focus restore to the window recorded at capture start. `SetForegroundWindow` is denied to a background process, so restoration goes through the `AttachThreadInput` workaround in `win_focus.py`. If restoration fails the text stays on the clipboard and the user is notified — it is never silently dropped.

### 6.5. Diagnostics

Logging must go to a **file** at `%APPDATA%\WhisperBoard\whisperboard.log`, not only stdout: the packaged app is built with `console=False`, so a frozen build currently produces no diagnostics at all.

### 6.6. Transcription history

Appended to `%APPDATA%\WhisperBoard\transcription_history.log`, timestamped, recording both confirmed and cancelled captures. Empty captures are not logged. No rotation — the log is a permanent safety net.

---

## 7. Technical Stack

- **Language** — Python 3.8+
- **UI** — PySide6 6.10.1
- **WebSockets** — websockets 15.0.1 (new asyncio API)
- **Audio** — pyaudio 0.2.14
- **Hotkeys** — pynput 1.8.1
- **Win32** — `ctypes` against `user32`/`kernel32` for focus control
- **Packaging** — PyInstaller (`WhisperBoard.spec`)

Versions are pinned in `requirements.txt`; PySide6 and websockets in particular have made breaking API changes.

---

## 8. Roadmap

**Phase 1 — Trustworthy core.** No silent failures. Level meter and device picker, log file, paste-failure notification, correct default model, tray left-click status.

**Phase 2 — Excellent capture box.** Editable text before confirm, real caret positioning, theme support, refined visuals.

**Phase 3 — Settings depth.** Tabbed settings, custom vocabulary, paste method options, clipboard restore.

**Phase 4 — Shareable.** Single-instance guard, packaged build with icon, first-run setup, README refresh, regression tests around the capture loop.
