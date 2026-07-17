#!/usr/bin/env python3
"""Keybow 2040 live looper.

Four loop tracks on keys 0-3, audio through the default device (Focusrite).
Use the interface's Direct Monitor to hear yourself; this program only plays
the loops.

Per track:
  tap   empty      -> record   (LED pulsing red)
  tap   recording  -> play     (LED green; first loop sets the master length)
  tap   playing    -> overdub  (LED pulsing orange)
  tap   overdubbing-> play
  hold             -> clear    (a hold's tap fires first; clear discards it)
  hold  key 15     -> clear everything

Keys 4-7 mute/unmute tracks 0-3 (the neighbouring key). Muting keeps the
loop and its phase, so unmuting drops back in exactly in time; the track
key blinks dim green while muted and its mute key lights solid blue.
Tapping a muted track's own key also unmutes it.

Later loops don't need to match the master length exactly: the recorded
length is rounded to the nearest multiple of the master loop, and overdubs
are latency-compensated so layers land where you played them.

Run:  .venv/bin/python looper.py   (stop keybowd first; they share the serial port)
"""

import glob
import queue
import re
import sys
import time

import numpy as np
import serial
import sounddevice as sd

SAMPLE_RATE = 44100
BLOCK = 256
MAX_SECONDS = 120          # per-track buffer
N_TRACKS = 4
MUTE_KEY_OFFSET = 4        # key n+4 mutes track n
CLEAR_ALL_KEY = 15

MODE_SOLID, MODE_PULSE, MODE_BLINK = 0, 1, 2

EMPTY, REC, PLAY, DUB = "empty", "rec", "play", "dub"

LED_FOR_STATE = {
    EMPTY: (2, 2, 2, MODE_SOLID),          # faint white: "this key is a track"
    REC: (255, 0, 0, MODE_PULSE),
    PLAY: (0, 220, 30, MODE_SOLID),
    DUB: (255, 120, 0, MODE_PULSE),
}
LED_MUTED_TRACK = (0, 70, 10, MODE_BLINK)      # dim green blink
LED_MUTE_ON = (0, 80, 255, MODE_SOLID)         # blue: press to bring it back
LED_MUTE_AVAIL = (0, 6, 20, MODE_SOLID)        # faint blue: track is muteable
LED_OFF = (0, 0, 0, MODE_SOLID)


class Track:
    def __init__(self):
        self.buf = np.zeros(MAX_SECONDS * SAMPLE_RATE, dtype=np.float32)
        self.state = EMPTY
        self.length = 0
        self.rec_len = 0
        self.start_phase = 0
        self.muted = False
        self.gain = 1.0            # applied gain, ramped toward 0/1 on (un)mute


class Looper:
    """All state transitions and audio run on the PortAudio callback thread;
    the control thread only posts commands and reads track states."""

    def __init__(self):
        self.tracks = [Track() for _ in range(N_TRACKS)]
        self.commands = queue.SimpleQueue()
        self.master_len = 0
        self.pos = 0               # global clock in samples, wraps at master_len
        self.lat_samples = 0       # set once the stream reports its latency
        self.xruns = 0
        self._mix = np.zeros(BLOCK, dtype=np.float32)
        self._scratch = np.zeros(BLOCK, dtype=np.float32)
        self.stream = sd.Stream(
            samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=(2, 2),
            dtype="float32", latency="low", callback=self._callback,
        )

    def start(self):
        self.stream.start()
        in_lat, out_lat = self.stream.latency
        self.lat_samples = int((in_lat + out_lat) * SAMPLE_RATE)
        return in_lat, out_lat

    # -- control-thread API ----------------------------------------------

    def tap(self, n):
        self.commands.put(("tap", n))

    def clear(self, n):
        self.commands.put(("clear", n))

    def clear_all(self):
        for n in range(N_TRACKS):
            self.commands.put(("clear", n))

    def toggle_mute(self, n):
        self.commands.put(("mute", n))

    def states(self):
        return [(t.state, t.muted) for t in self.tracks]

    # -- callback-thread side --------------------------------------------

    def _do_command(self, cmd, n):
        t = self.tracks[n]
        if cmd == "clear":
            t.state = EMPTY
            t.length = t.rec_len = 0
            t.muted = False
            if all(tr.state == EMPTY for tr in self.tracks):
                self.master_len = 0
                self.pos = 0
        elif cmd == "mute":
            if t.state == DUB:
                t.state = PLAY
                t.muted = True
            elif t.state == PLAY:
                t.muted = not t.muted
        elif cmd == "tap":
            if t.state == PLAY and t.muted:
                t.muted = False
            elif t.state == EMPTY:
                t.rec_len = 0
                # Musical time of the input we're about to receive: the player
                # is following output we already emitted, so shift earlier by
                # the round-trip latency.
                if self.master_len:
                    t.start_phase = (self.pos - self.lat_samples) % self.master_len
                else:
                    t.start_phase = 0
                t.state = REC
            elif t.state == REC:
                self._finish_recording(t)
            elif t.state == PLAY:
                t.state = DUB
            elif t.state == DUB:
                t.state = PLAY

    def _finish_recording(self, t):
        if t.rec_len < BLOCK:               # accidental double-tap: nothing there
            t.state = EMPTY
            return
        if not self.master_len:
            self.master_len = t.length = t.rec_len
            self.pos = 0                    # the loop starts *now*
        else:
            mult = max(1, round(t.rec_len / self.master_len))
            t.length = mult * self.master_len
            if t.rec_len < t.length:        # stopped early: pad with silence
                t.buf[t.rec_len:t.length] = 0
        t.state = PLAY

    def _add_wrapped(self, out, buf, idx, length, gain=1.0, write=False, src=None):
        """Read (or overdub-write) BLOCK samples at idx modulo length."""
        n = len(out) if not write else len(src)
        first = min(n, length - idx)
        if write:
            buf[idx:idx + first] += src[:first]
            if first < n:
                buf[:n - first] += src[first:]
        else:
            out[:first] += buf[idx:idx + first] * gain
            if first < n:
                out[first:] += buf[:n - first] * gain

    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            self.xruns += 1
        while True:
            try:
                cmd, n = self.commands.get_nowait()
            except queue.Empty:
                break
            self._do_command(cmd, n)

        mono_in = indata[:, 0] + indata[:, 1]
        mix = self._mix
        mix[:] = 0.0

        for t in self.tracks:
            if t.state == REC:
                end = t.rec_len + frames
                if end > len(t.buf):        # out of room: auto-finish
                    self._finish_recording(t)
                else:
                    t.buf[t.rec_len:end] = mono_in
                    t.rec_len = end
            if t.state in (PLAY, DUB) and t.length:
                target = 0.0 if t.muted else 1.0
                if t.gain == 0.0 and target == 0.0:
                    continue                    # muted and fully faded: skip
                idx = (self.pos - t.start_phase) % t.length
                if t.gain != target:            # (un)muting: one-block fade
                    scratch = self._scratch
                    scratch[:] = 0.0
                    self._add_wrapped(scratch, t.buf, idx, t.length)
                    scratch *= np.linspace(t.gain, target, frames,
                                           dtype=np.float32)
                    mix += scratch
                    t.gain = target
                else:
                    self._add_wrapped(mix, t.buf, idx, t.length)
                if t.state == DUB:
                    widx = (self.pos - self.lat_samples - t.start_phase) % t.length
                    self._add_wrapped(None, t.buf, widx, t.length,
                                      write=True, src=mono_in)

        if self.master_len:
            self.pos = (self.pos + frames) % self.master_len

        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:, 0] = mix
        outdata[:, 1] = mix


# -- keybow serial ---------------------------------------------------------


def find_port():
    ports = glob.glob("/dev/cu.usbmodem*")
    return ports[0] if ports else None


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    port = find_port()
    if not port:
        sys.exit("no keybow serial port found")
    ser = serial.Serial(port, 115200, timeout=0.02)

    looper = Looper()
    in_lat, out_lat = looper.start()
    log(f"audio running: {SAMPLE_RATE}Hz block={BLOCK} "
        f"latency in={in_lat * 1000:.1f}ms out={out_lat * 1000:.1f}ms "
        f"(compensating {looper.lat_samples} samples)")
    log(f"keybow on {port}; tracks on keys 0-{N_TRACKS - 1}, "
        f"hold key {CLEAR_ALL_KEY} to clear all")

    ser.write(b"CLR\n")
    sent = {}
    shown = None
    rxbuf = b""
    last_ping = 0.0

    while True:
        now = time.time()
        if now - last_ping > 1.0:
            last_ping = now
            ser.write(b"PING\n")
            if looper.xruns:
                log(f"note: {looper.xruns} audio over/underruns so far")

        # LEDs follow track state.
        states = looper.states()
        if states != shown:
            log("tracks: " + "  ".join(
                f"{i}:{s}{'(muted)' if m else ''}" for i, (s, m) in enumerate(states)))
            shown = list(states)
        for i, (s, muted) in enumerate(states):
            led = LED_MUTED_TRACK if (s == PLAY and muted) else LED_FOR_STATE[s]
            if s in (PLAY, DUB):
                mute_led = LED_MUTE_ON if muted else LED_MUTE_AVAIL
            else:
                mute_led = LED_OFF
            for key, want in ((i, led), (i + MUTE_KEY_OFFSET, mute_led)):
                if sent.get(key) != want:
                    ser.write(("L %d %d %d %d %d\n" % (key, *want)).encode())
                    sent[key] = want

        rxbuf += ser.read(256)
        while b"\n" in rxbuf:
            line, rxbuf = rxbuf.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if not text or text == "PONG":
                continue
            m = re.match(r"([PH]) (\d+)$", text)
            if not m:
                log(f"keybow: {text}")
                continue
            ev, n = m.group(1), int(m.group(2))
            if n < N_TRACKS:
                looper.tap(n) if ev == "P" else looper.clear(n)
            elif MUTE_KEY_OFFSET <= n < MUTE_KEY_OFFSET + N_TRACKS and ev == "P":
                looper.toggle_mute(n - MUTE_KEY_OFFSET)
            elif n == CLEAR_ALL_KEY and ev == "H":
                log("clear all")
                looper.clear_all()

        time.sleep(0.02)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
