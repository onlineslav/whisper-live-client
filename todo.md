# WhisperType - To-Do List

Reconciled 2026-08-29 against the actual state of the code. See `specs.md` for the mandate and the settings reference.

---

## Done

- [x] **Project setup** — `src/` layout, `requirements.txt`, `main.py` entry point
- [x] **System tray** — `QSystemTrayIcon`, four programmatically drawn state icons, context menu (Status / Settings / History / Exit)
- [x] **Settings window** — server address, hotkey, model, connection mode, launch on startup; persisted to `%APPDATA%\WhisperType\config.json`; applied live on save
- [x] **Global hotkey listener** — `pynput`, configurable, reloads after settings save, validates against modifier-only combos
- [x] **WebSocket client** — connect/disconnect/reconnect, EOS handling, status and message signals, clean thread shutdown via `asyncio.Event`
- [x] **Audio capture** — 16 kHz mono float32, worker thread, client-side VAD gate with pre-roll and hangover
- [x] **Capture box** — frameless translucent overlay, fade in/out, `Enter`/`Esc`, confirm/cancel buttons, screen clamping
- [x] **Integration** — hotkey drives capture, transcripts feed the box, confirm pastes, tray reflects state
- [x] **Transcription history** — timestamped append log, opened from the tray menu
- [x] **Launch on startup** — registry `Run` key wiring
- [x] **Hallucination filtering** — phrase list applied per segment
- [x] **Multiple message schemas** — `segments` / `text` / `segment` handled, unexpected payloads ignored
- [x] **Focus routing for paste** — foreground window recorded at capture start, restored via `AttachThreadInput` before `Ctrl+V`
- [x] **Fix: confirm was routed to cancel** — screen-vs-local coordinate mismatch in the click-away hit test, plus the app filter seeing the `QWindow` press before the widget's and eating the click
- [x] **Fix: capture box never held keyboard focus** — `activateWindow()` is denied to a background process, so `Enter`/`Esc` went to the app underneath
- [x] **Single-instance guard** — named mutex (`Local\WhisperType-SingleInstance`); a second launch explains itself and exits. This was not hypothetical: two copies were running, so both opened a box on the same hotkey and the paste landed in the *other* instance's read-only text area, which swallowed it silently
- [x] **Fix: paste did not reach the target app** — rewritten as a paced sequence in `win_input.py`: native `CF_UNICODETEXT` clipboard (Qt's delayed-rendering data object made the paste depend on our event loop), foreground restore verified by polling rather than assumed, held modifiers released so a stray Shift/Alt cannot turn the injected `Ctrl+V` into another shortcut, and `SendInput` return values checked so a refused injection is visible
- [x] **Notify on paste failure** — tray balloon naming the reason, with the transcript left on the clipboard so `Ctrl+V` still recovers it
- [x] **Input level meter in the capture box** — `signal_meter.py`: one auto-ranging, VU/PPM-ballistic signal chain (median de-spike, tracked noise floor and decaying peak ceiling, dB-domain normalisation) behind four interchangeable styles, in the space left of the Confirm button. Click the meter to cycle styles; the choice persists
- [x] **Start the server, and report what it is doing** — `server_manager.py` starts Docker Desktop, pulls the image (with a download percentage), and runs the container; the tray shows a distinct state for each step with elapsed time, and the capture box says what it is waiting for instead of a permanent "Connecting...". WhisperType launched at login while the server did not, so the app spent its time looking ready with nothing listening on 9090
- [x] **Report server status, not just socket status** — `SERVER_READY` and `WAIT` are now read, so "Ready" means the model is loaded rather than that the socket opened; audio recorded during a cold model load is held and flushed instead of being streamed at a server that is not reading yet. Latency is still not measured
- [x] **Fix: a saved hotkey with a named key would not load** — `_normalize_hotkey_string` stripped the angle brackets off every non-modifier key, so `<ctrl>+<alt>+<f9>` came back as `f9` and pynput refused it. Single-character hotkeys like the default `` <ctrl>+` `` hid this
- [x] **Share one model across connections** — WhisperLive loads a model per *connection* and hangs up after each `END_OF_AUDIO`, so every dictation left another copy behind: 7.1 GB resident on an 8 GB card after ordinary use, after which the GPU is full, clocks drop and every model crawls (`tiny.en` 1.27s → 5.03s to first word, transcribing three words of a ten-second utterance). Its single-model mode is gated behind a custom model *path*, so the manager resolves the chosen model to its snapshot directory in the cache volume and passes `-fw`. Per-connection load went 0.4–3.3s → 0.0s and VRAM is flat across connections; `distil-large-v3` now matches `tiny.en` for latency
- [x] **Release the GPU for other programs** — a configurable list of executables; while any is running the container is stopped and restarted when they exit, with an explicit tray start overriding until that program closes
- [x] **Option to stop reconnecting after each capture** — the reconnect kept a warm connection but cost a model load per capture on a server that does not share one
- [x] **Fix: a running container was never reconfigured** — `ensure_running` returned early on "something is listening", so a settings change that needs a different container silently never took effect
- [x] **Fix: the readiness check passed too early** — a bare TCP connect succeeds as soon as the port is bound, before the server can serve, so the first connection after a container rebuild failed. It now waits for an actual reply
- [x] **Model as a dropdown** — nine curated models with one-line descriptions, split into CPU- and GPU-sized groups; the stored value is the bare model id, and a custom name set by hand in `config.json` is preserved and labelled rather than silently replaced. Free text failed silently on a typo: the server rejects the name and the capture box just never fills in
- [x] **Persist the server's model cache** — the container had no volume for `/root/.cache/huggingface`, so every recreate re-downloaded every model (2.4 GB, measured). Now a named volume, so switching models in Settings costs one download ever
- [x] **Fix: the health probe was flooding the server log** — a bare TCP connect makes WhisperLive log a 27-line traceback, once every probe. The idle poll now asks Docker for containers it manages (zero server-side noise) and only falls back to the network for servers it does not own, where it sends a request line rather than hanging up
- [x] **Click-away to cancel, properly** — driven by window activation loss; a Qt event filter cannot observe clicks in other applications, so the previous implementation never actually worked
- [x] **Show where the text is going to land** — `caret_target.py` finds the paste target through a three-rung ladder (`GetGUIThreadInfo`'s classic caret, then UI Automation's `TextPattern` selection, then the window itself) and `target_overlay.py` draws on it: a flash when the capture opens, then a caret bar that stays for the length of it. Nothing on screen had ever said where the transcript would go, so a capture aimed at the wrong window looked exactly like one aimed at the right one right up until the paste. The rung that answered is deliberately visible — an exact caret gets a bar, a control gets its own outline, a window and nothing more gets a dashed outline and its name — and a focused element that fills its own window is demoted to the window rung rather than drawn as if it were precise. UI Automation is a cross-process call that can block, so it runs once per capture inside a time budget and disables itself for the session if it overruns; everything after that is pure user32 and free

---

## Phase 1 — Trustworthy core (no silent failures)

- [ ] **Set the default model to `distil-small.en` and fix the live config** — currently running `tiny.en`, which the README explicitly warns against; it is the source of most observed hallucination
- [ ] **Draw the VAD threshold on the level meter** — the meter itself is built (below), but the gate's cut-off is not marked on it, so a mic quiet enough to stream nothing still looks the same as one that is working
- [ ] **Microphone device picker** in settings — the user cannot currently see or choose which mic was opened
- [ ] **Expose the VAD threshold** as a setting instead of the hardcoded `0.012`
- [ ] **Log to a file** at `%APPDATA%\WhisperType\whispertype.log` — the packaged build is `console=False` and currently produces no diagnostics at all
- [ ] **Tray left-click opens a status panel** — left-click now shows the current status as a notification balloon, which covers the "is this thing working" question; a real panel is still unbuilt
- [ ] **Skip empty history entries** — cancelled captures with no text currently write blank lines
- [ ] **Feedback when the history file is missing** — `open_history` currently `pass`es silently
- [ ] **Bound the audio thread join** — `stop_streaming()` joins with no timeout on the GUI thread; a blocked PyAudio read freezes the UI
- [ ] **Delete the dead repo-root `config.json`** — nothing reads it (real config lives in `%APPDATA%`), and it disagrees with the live one
- [ ] **Tune the hangover window** against real dictation to protect trailing words — the chosen alternative to delaying the paste

## Phase 2 — Excellent capture box

- [ ] **Make the text editable before confirming** so transcription errors can be fixed in place
- [x] **Real caret-aware positioning** — done as a side effect of the target marker above: `caret_target.locate()` finds the caret in the *other* application, and the box now opens beside it rather than beside the mouse. Off with "Open the box beside the caret, not the mouse", and it still falls back to the pointer wherever no caret is found
- [ ] **Theme support** (light / dark / follow system) — specified since day one, never built
- [ ] **Max capture duration** so a forgotten capture cannot stream forever
- [ ] Visual refinement: icon buttons (`✓` / `✗`) instead of text, opacity setting

## Phase 3 — Settings depth

- [ ] **Tabbed settings window** — Connection / Audio / Text & Paste / Behavior / History / System
- [ ] **Restore the clipboard after pasting** — dictation currently destroys clipboard contents
- [ ] **Paste method option** — clipboard+`Ctrl+V` or direct keystroke typing for apps that block clipboard paste. `win_input.type_text()` already implements the typing path as an automatic fallback when the clipboard is unwritable; this item is now just exposing it as a choice
- [ ] **Custom vocabulary / replacement rules** for names and jargon
- [ ] **User-editable hallucination phrase list**
- [ ] **"Test connection" button** reporting reachability, loaded model, and latency
- [ ] **Cancel-on-focus-loss toggle**

## Phase 4 — Shareable

- [ ] **Regression tests for the capture loop** — confirm/cancel routing has broken three times; a harness driving the box directly catches it
- [ ] **Pin `requirements.txt`** — PySide6 and websockets have both made breaking API changes
- [ ] **Refresh the README** — the "restart for settings to take effect" instruction is stale, and the documented default model does not match the shipped one
- [ ] **Package properly** — application icon, first-run setup, verified `.exe` build
