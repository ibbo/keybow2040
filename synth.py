"""Small polyphonic synth rendered inside the looper's audio callback.

Two detuned saws per voice with an exponential-segment envelope. All methods
are called from the audio callback thread; MIDI messages are handed in via
Synth.handle() (already dequeued by the caller).
"""

import numpy as np

MAX_VOICES = 10
DETUNE = 1.004
AMP = 0.18                  # per-voice amplitude at full velocity
BEND_RANGE = 2.0            # semitones at full pitch-bend

ATTACK_TAU = 0.002
DECAY_TAU = 0.040
SUSTAIN = 0.70
RELEASE_TAU = 0.080

A, D, S, R = range(4)       # envelope stages


def midi_to_freq(note):
    return 440.0 * 2.0 ** ((note - 69) / 12.0)


class Voice:
    def __init__(self, sr, note, velocity, serial):
        self.sr = sr
        self.note = note
        self.serial = serial
        self.freq = midi_to_freq(note)
        self.amp = AMP * (velocity / 127.0) ** 1.5
        self.phase1 = 0.0
        self.phase2 = 0.0
        self.env = 0.0
        self.stage = A
        self.done = False

    def release(self):
        self.stage = R

    def _saw(self, freq, frames, phase_attr):
        inc = freq / self.sr
        t = getattr(self, phase_attr) + inc * np.arange(1, frames + 1)
        setattr(self, phase_attr, float(t[-1] % 1.0))
        return (2.0 * (t % 1.0) - 1.0).astype(np.float32)

    def _envelope(self, frames):
        target, tau = {
            A: (1.02, ATTACK_TAU),      # overshoot target so we actually reach 1
            D: (SUSTAIN, DECAY_TAU),
            S: (SUSTAIN, DECAY_TAU),
            R: (0.0, RELEASE_TAU),
        }[self.stage]
        n = np.arange(frames, dtype=np.float32)
        env = target + (self.env - target) * np.exp(-n / (tau * self.sr))
        self.env = float(env[-1])
        if self.stage == A and self.env >= 1.0:
            self.stage = D
        elif self.stage == R and self.env < 1e-3:
            self.done = True
        return np.clip(env, 0.0, 1.0)

    def render(self, frames, bend_factor):
        f = self.freq * bend_factor
        sig = self._saw(f * DETUNE, frames, "phase1")
        sig += self._saw(f / DETUNE, frames, "phase2")
        return sig * (self._envelope(frames) * self.amp)


class Synth:
    def __init__(self, samplerate):
        self.sr = samplerate
        self.voices = []
        self.bend = 0.0             # -1..1
        self.sustain = False
        self.deferred_off = set()   # notes released while sustain pedal down
        self.serial = 0

    def handle(self, msg):
        if msg.type == "note_on" and msg.velocity > 0:
            self.deferred_off.discard(msg.note)
            if len(self.voices) >= MAX_VOICES:
                releasing = [v for v in self.voices if v.stage == R]
                victim = (min(releasing, key=lambda v: v.env) if releasing
                          else min(self.voices, key=lambda v: v.serial))
                self.voices.remove(victim)
            self.serial += 1
            self.voices.append(Voice(self.sr, msg.note, msg.velocity, self.serial))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            if self.sustain:
                self.deferred_off.add(msg.note)
            else:
                self._release_note(msg.note)
        elif msg.type == "pitchwheel":
            self.bend = msg.pitch / 8192.0
        elif msg.type == "control_change" and msg.control == 64:
            self.sustain = msg.value >= 64
            if not self.sustain:
                for note in self.deferred_off:
                    self._release_note(note)
                self.deferred_off.clear()

    def _release_note(self, note):
        for v in self.voices:
            if v.note == note and v.stage != R:
                v.release()

    def render(self, out):
        """Add the next len(out) samples into `out` (float32)."""
        if not self.voices:
            return
        bend_factor = 2.0 ** (self.bend * BEND_RANGE / 12.0)
        frames = len(out)
        for v in self.voices:
            out += v.render(frames, bend_factor)
        self.voices = [v for v in self.voices if not v.done]
