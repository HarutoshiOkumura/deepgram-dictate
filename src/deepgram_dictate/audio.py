"""Microphone capture through PipeWire's pw-record: 16 kHz mono signed 16-bit PCM."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from typing import Literal

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
_READ_SIZE = 4096


class Recorder:
    def __init__(self, source: str = "") -> None:
        self._source = source
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        args = [
            "pw-record",
            "--rate", str(SAMPLE_RATE),
            "--channels", "1",
            "--format", "s16",
            "--raw",
            "--latency", "40ms",
            "--media-role", "Communication",
        ]  # fmt: skip
        if self._source:
            args += ["--target", self._source]
        args.append("-")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield raw PCM as it arrives, until the recorder stops."""
        assert self._proc and self._proc.stdout
        while data := await self._proc.stdout.read(_READ_SIZE):
            yield data
        await self._proc.wait()

    def stop(self) -> None:
        """Ask pw-record to exit; audio already captured is still delivered by chunks()."""
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()

    def kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()


def peak(chunk: bytes) -> int:
    """Largest absolute sample value in a chunk of s16le PCM."""
    usable = len(chunk) - len(chunk) % 2
    if not usable:
        return 0
    samples = memoryview(chunk)[:usable].cast("h")
    hi, lo = max(samples), min(samples)
    return max(hi, -lo)


SourceStatus = Literal["muted", "unmuted", "missing", "unknown"]


async def source_status(source: str = "") -> SourceStatus:
    """Mute state of the microphone in PipeWire/PulseAudio, or whether it is missing.

    "missing" matters: pw-record silently falls back to the default microphone when its
    target does not exist, which would record the wrong device without any error.
    """
    proc = await asyncio.create_subprocess_exec(
        "pactl",
        "get-source-mute",
        source or "@DEFAULT_SOURCE@",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        return "missing" if b"No such entity" in err else "unknown"
    answer = out.decode().strip().lower()
    if answer.endswith("yes"):
        return "muted"
    if answer.endswith("no"):
        return "unmuted"
    return "unknown"


assert sys.byteorder == "little", "PCM handling assumes a little-endian machine"
