# Keybow 2040 firmware: AI session status display + switcher.
#
# Acts as a dumb terminal for a host daemon speaking a line protocol over
# the USB serial console:
#
#   host -> keybow:  L <key> <r> <g> <b> <mode>   set LED (mode 0=solid, 1=pulse, 2=blink)
#                    CLR                          all LEDs off
#                    PING                         keepalive (any command counts)
#   keybow -> host:  P <key>                      key was pressed
#                    PONG                         reply to PING
#
# If the host goes quiet for 10s, all keys show a slow dim amber breathe
# so you can tell the daemon is down.

import math
import sys
import time

import supervisor
from pmk import PMK
from pmk.platform.keybow2040 import Keybow2040 as Hardware

keybow = PMK(Hardware())
keys = keybow.keys

MODE_SOLID = 0
MODE_PULSE = 1
MODE_BLINK = 2

# Per-key [r, g, b, mode], set by the host.
led = [[0, 0, 0, MODE_SOLID] for _ in range(16)]

last_host = -1000.0
buf = ""


def make_press_handler(n):
    def handler(key):
        print("P %d" % n)
    return handler


for i, k in enumerate(keys):
    keybow.on_press(k, make_press_handler(i))


def handle_line(line):
    global last_host
    parts = line.split()
    if not parts:
        return
    last_host = time.monotonic()
    cmd = parts[0]
    if cmd == "L" and len(parts) == 6:
        try:
            n, r, g, b, m = (int(p) for p in parts[1:])
        except ValueError:
            return
        if 0 <= n < 16:
            led[n] = [r, g, b, m]
    elif cmd == "CLR":
        for s in led:
            s[0] = s[1] = s[2] = 0
            s[3] = MODE_SOLID
    elif cmd == "PING":
        print("PONG")


while True:
    keybow.update()

    while supervisor.runtime.serial_bytes_available:
        c = sys.stdin.read(1)
        if c == "\n" or c == "\r":
            handle_line(buf)
            buf = ""
        else:
            buf += c
            if len(buf) > 64:  # junk guard
                buf = ""

    now = time.monotonic()
    if now - last_host < 10.0:
        for i in range(16):
            r, g, b, m = led[i]
            if m == MODE_PULSE:
                s = 0.06 + 0.94 * (0.5 + 0.5 * math.sin(now * 2 * math.pi / 1.4))
            elif m == MODE_BLINK:
                s = 1.0 if (now % 0.5) < 0.25 else 0.0
            else:
                s = 1.0
            keys[i].set_led(int(r * s), int(g * s), int(b * s))
    else:
        # Host daemon not talking to us: dim amber breathe everywhere.
        s = 0.04 + 0.10 * (0.5 + 0.5 * math.sin(now * 2 * math.pi / 3.0))
        for i in range(16):
            keys[i].set_led(int(255 * s), int(80 * s), 0)
