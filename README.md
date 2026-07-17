# Keybow 2040 AI session switcher (and live looper)

Turns a [Pimoroni Keybow 2040](https://shop.pimoroni.com/products/keybow-2040) into a
status display and switcher for AI assistant sessions — plus, sharing the same
firmware, a standalone live audio looper (`looper.py`, see below):

- Each key maps to a session; its LED shows status:
  - **off** — session not running
  - **dim solid** — running in the background
  - **pulsing** — waiting for your input
  - **bright solid** — currently focused
- Pressing a key switches to that session.
- All keys breathe dim amber when the host daemon isn't running.

## Architecture

Two parts talking a tiny line protocol over the Keybow's USB serial console:

- **`firmware/code.py`** (CircuitPython, on the Keybow) — a dumb display/input
  device. Renders LED commands (`L <key> <r> <g> <b> <mode>`, mode
  0=solid / 1=pulse / 2=blink; `CLR`; `PING`→`PONG`) and emits `P <key>` on
  key press. No policy on the device: what a colour/state *means* lives on the host.
- **`keybowd.py`** (Python, on the host) — polls session status once a second,
  pushes LED states, and reacts to key presses by focusing the session.

Session providers implement two methods, `led_state(front_app)` and
`activate()`. Current provider:

- `MacAppSession` — a macOS app. "Waiting for input" is detected via the dock
  badge (`lsappinfo info -only StatusLabel <App>`), focus via `lsappinfo front`,
  switching via AppleScript `activate`. Configured for Claude Desktop (key 0,
  orange) and Codex/ChatGPT (key 1, cyan) in `SESSIONS` at the top of
  `keybowd.py`.

Planned: a tmux provider for Claude Code sessions at work — `led_state` driven
by Claude Code hooks writing state files (distinguishes "busy generating" from
"waiting on a prompt"), `activate` via `tmux select-window` + focusing the
terminal.

## Setup

The Keybow needs CircuitPython (tested with 9.1.4) and the
[`pmk` library](https://github.com/pimoroni/pmk-circuitpython) in `lib/` —
the stock Pimoroni setup. Then:

```sh
# Flash the firmware (device auto-reloads on save)
cp firmware/code.py /Volumes/CIRCUITPY/code.py

# Host daemon
python3 -m venv .venv
.venv/bin/pip install pyserial
.venv/bin/python keybowd.py
```

The daemon finds the Keybow at `/dev/cu.usbmodem*` automatically and
reconnects if it's unplugged.

## Live looper

`looper.py` turns the Keybow into a 4-track live looper for an audio
interface (built against a Focusrite Scarlett Solo; uses the default audio
device). Use the interface's Direct Monitor to hear yourself — the looper
only plays back loops.

```sh
pip install sounddevice numpy mido python-rtmidi   # in the same venv
.venv/bin/python looper.py      # stop keybowd first: they share the serial port
```

Layout: keys 0–3 are loop tracks, keys 4–7 mute/unmute the neighbouring
track, hold key 15 to clear everything.

Per track: tap to **arm** (blinking red) — recording starts, sample-tight,
the moment the input crosses `TRIGGER_LEVEL`, so there's no dead air while
you get back to your instrument (tap an armed track to force an immediate
start). Tap to close the loop and play (green), tap to overdub (pulsing
orange), tap to return to play; hold to clear. The first loop sets the
master length; later recordings are rounded to the nearest whole multiple
of it, so longer phrases work naturally. Muted tracks (blinking dim green,
mute key solid blue) keep spinning silently in phase, so they come back
exactly in time; (un)mutes are ramped over one audio block to avoid clicks.

### Saving

Every looper run tapes the full performance — loops, synth *and* the live
(direct-monitored) input — to `recordings/<timestamp>/tape.wav`, so the take
you didn't know you wanted is already captured. Tap **key 14** (dim purple;
flashes green on save) to bounce the current loops: one WAV stem per track,
rolled onto the master timeline so they line up in a DAW, plus a mixdown of
what's audible (muted tracks get a stem but stay out of the mix). The
tape's WAV header is refreshed as it grows, so even a hard kill leaves a
playable file; disk writes happen on the control thread, never the audio
callback.

### MIDI keyboard (Roland A-series)

If a MIDI input matching `MIDI_PORT_MATCH` is present, it plays a built-in
polyphonic synth (`synth.py`: two detuned saws per voice, exponential
envelope, pitch bend) mixed into the output and into whatever is recording —
loops can hold audio-interface input, keys, or both. A pedal in the HOLD
jack is a hands-free tap on the *focused* track (last track key touched;
brighter white when empty), knob C1 (CC 74) is the focused track's playback
volume, C2 (CC 71) the synth volume; unmapped CCs are logged so other
controllers can be wired up. Set `PEDAL_CONTROLS_LOOPER = False` to give
the pedal back to synth sustain.

Engineering notes: audio runs in a PortAudio duplex callback
(`sounddevice`, 128-sample blocks, `latency="low"`) with all loop state
owned by the callback thread — the control thread and MIDI thread post
commands/messages via queues and read state back for the LEDs. Recorded
material is shifted earlier by the stream's reported round-trip latency
(~24&nbsp;ms measured) so layers land where you played them, not where the
samples arrived; the synth's recording feed is delayed by the same amount
(it needs no mic-path shift), keeping looped keys and looped strings in
the same groove.
