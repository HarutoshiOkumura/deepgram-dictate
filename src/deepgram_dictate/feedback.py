"""Audible cues and desktop notifications."""

from __future__ import annotations

import array
import asyncio
import logging
import math
import wave
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

_RATE = 48_000
# Each cue is a list of (frequency Hz, seconds); frequency 0 is a pause.
_CUES: dict[str, list[tuple[float, float]]] = {
    "start": [(587.33, 0.055), (880.0, 0.075)],  # rising: listening
    "lock": [(1174.66, 0.045)],  # single high blip: hands-free locked on
    "switch": [(987.77, 0.04), (0, 0.025), (1318.51, 0.06)],  # two blips: language chosen
    "stop": [(880.0, 0.055), (587.33, 0.075)],  # falling: done listening
    "cancel": [(392.0, 0.11)],
    "error": [(311.13, 0.09), (0, 0.05), (311.13, 0.12)],
}
_CUE_VERSION = "1"


def _render(notes: list[tuple[float, float]], path: Path) -> None:
    frames = array.array("h")
    for freq, secs in notes:
        n = int(_RATE * secs)
        attack, release = int(0.004 * _RATE), int(0.03 * _RATE)
        for i in range(n):
            if freq == 0:
                frames.append(0)
                continue
            env = min(1.0, i / attack, (n - i) / release)
            t = i / _RATE
            # A soft sine with a little second harmonic sounds less harsh than a pure tone.
            s = math.sin(2 * math.pi * freq * t) + 0.2 * math.sin(4 * math.pi * freq * t)
            frames.append(int(0.5 * 32767 * env * s / 1.2))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(frames.tobytes())
    tmp.replace(path)


def _cue_path(name: str) -> Path:
    return paths.cache_dir() / "sounds" / f"{name}-v{_CUE_VERSION}.wav"


def prepare_sounds() -> None:
    for name, notes in _CUES.items():
        path = _cue_path(name)
        if not path.exists():
            _render(notes, path)


class Feedback:
    def __init__(self, *, sounds: bool, volume: float) -> None:
        self._sounds = sounds
        self._volume = volume
        self._background: set[asyncio.Task[None]] = set()
        if sounds:
            prepare_sounds()

    def _spawn(self, *args: str) -> None:
        async def run() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except OSError as e:
                log.warning("could not run %s: %s", args[0], e)

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def cue(self, name: str) -> None:
        if self._sounds:
            self._spawn(
                "pw-play", "--media-role", "Notification", "--volume", str(self._volume),
                str(_cue_path(name)),
            )  # fmt: skip

    def notify(self, summary: str, body: str = "", *, urgent: bool = False) -> None:
        self._spawn(
            "notify-send",
            "--app-name=Dictation",
            "--icon=audio-input-microphone",
            f"--urgency={'critical' if urgent else 'normal'}",
            summary,
            body,
        )

    def error(self, summary: str, body: str = "") -> None:
        log.error("%s: %s", summary, body)
        self.cue("error")
        self.notify(summary, body, urgent=True)
