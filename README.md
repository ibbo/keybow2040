# Keybow 2040 AI session switcher

Turns a [Pimoroni Keybow 2040](https://shop.pimoroni.com/products/keybow-2040) into a
status display and switcher for AI assistant sessions:

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
