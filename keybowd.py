#!/usr/bin/env python3
"""Keybow 2040 AI session switcher daemon.

Maps AI assistant sessions to Keybow keys:
  - LED shows session status (off = not running, dim = idle in background,
    pulsing = waiting for your input, bright = focused).
  - Pressing a key focuses that session on the Mac.

Today's providers are macOS apps (Claude Desktop, Codex via ChatGPT), where
"waiting for input" is detected from the app's dock badge. The Session
interface is deliberately small so a tmux/Claude Code provider can be added
later for the work setup.

Run:  .venv/bin/python keybowd.py
"""

import glob
import re
import subprocess
import sys
import time

import serial

# --- configuration -----------------------------------------------------------

MODE_SOLID = 0
MODE_PULSE = 1
MODE_BLINK = 2

IDLE_SCALE = 0.10       # LED brightness for a running-but-backgrounded session
POLL_INTERVAL = 1.0     # seconds between status polls
REFRESH_EVERY = 5.0     # resend all LED states this often even if unchanged

OFF = (0, 0, 0, MODE_SOLID)


class MacAppSession:
    """A session backed by a macOS app. Waiting = app has a dock badge."""

    def __init__(self, name, app_name, key, color):
        self.name = name
        self.app_name = app_name
        self.key = key
        self.color = color

    def _lsappinfo(self, only):
        out = subprocess.run(
            ["lsappinfo", "info", "-only", only, self.app_name],
            capture_output=True, text=True,
        ).stdout
        return out

    def led_state(self, front_app):
        """Return (r, g, b, mode) for the current status of this session."""
        if '"pid"' not in self._lsappinfo("pid"):
            return OFF
        r, g, b = self.color
        if self.app_name == front_app:
            return (r, g, b, MODE_SOLID)
        if '"label"' in self._lsappinfo("StatusLabel"):
            return (r, g, b, MODE_PULSE)
        return (int(r * IDLE_SCALE), int(g * IDLE_SCALE), int(b * IDLE_SCALE),
                MODE_SOLID)

    def activate(self):
        subprocess.run(
            ["osascript", "-e",
             f'tell application "{self.app_name}" to activate'],
            capture_output=True,
        )


SESSIONS = [
    MacAppSession("Claude Desktop", "Claude", key=0, color=(255, 70, 0)),
    MacAppSession("Codex (ChatGPT)", "ChatGPT", key=1, color=(0, 180, 255)),
]

# --- helpers -----------------------------------------------------------------


def frontmost_app():
    asn = subprocess.run(["lsappinfo", "front"],
                         capture_output=True, text=True).stdout.strip()
    if not asn:
        return None
    out = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                         capture_output=True, text=True).stdout
    m = re.search(r'"LSDisplayName"="([^"]+)"', out)
    return m.group(1) if m else None


def find_port():
    ports = glob.glob("/dev/cu.usbmodem*")
    return ports[0] if ports else None


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --- main loop ---------------------------------------------------------------


def run(port):
    ser = serial.Serial(port, 115200, timeout=0.05)
    log(f"connected to {port}")
    sent = {}           # key -> last led state sent
    by_key = {s.key: s for s in SESSIONS}
    last_poll = 0.0
    last_refresh = 0.0
    states = {}
    rxbuf = b""

    def send(line):
        ser.write((line + "\n").encode())

    send("CLR")

    while True:
        now = time.time()

        if now - last_poll >= POLL_INTERVAL:
            last_poll = now
            front = frontmost_app()
            states = {s.key: s.led_state(front) for s in SESSIONS}
            send("PING")  # keepalive even when nothing changes

        refresh = now - last_refresh >= REFRESH_EVERY
        if refresh:
            last_refresh = now
        for key, st in states.items():
            if refresh or sent.get(key) != st:
                send("L %d %d %d %d %d" % (key, *st))
                if sent.get(key) != st:
                    session = by_key[key]
                    log(f"{session.name}: led -> {st}")
                sent[key] = st

        # Read key presses (and anything else the keybow prints).
        rxbuf += ser.read(256)
        while b"\n" in rxbuf:
            line, rxbuf = rxbuf.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if not text or text == "PONG":
                continue
            m = re.match(r"P (\d+)$", text)
            if m and int(m.group(1)) in by_key:
                session = by_key[int(m.group(1))]
                log(f"key {m.group(1)} pressed -> activating {session.name}")
                session.activate()
            elif m:
                log(f"unmapped key {m.group(1)} pressed")
            else:
                log(f"keybow: {text}")  # e.g. tracebacks from code.py


def main():
    while True:
        port = find_port()
        if not port:
            log("no keybow serial port found, retrying in 3s")
            time.sleep(3)
            continue
        try:
            run(port)
        except (serial.SerialException, OSError) as e:
            log(f"serial error: {e}; reconnecting in 3s")
            time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
