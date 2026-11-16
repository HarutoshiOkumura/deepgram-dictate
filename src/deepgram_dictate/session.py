"""One dictation: record the microphone, stream it to Deepgram, and produce the final text.

Audio is streamed live so the transcript is ready almost as soon as you stop talking. Every
byte is also kept in memory: if the live stream fails for a reason other than a bad key, the
whole recording is uploaded once instead, so a network hiccup never loses what you said.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import wave
from collections.abc import Callable
from pathlib import Path

from . import paths
from .audio import BYTES_PER_SECOND, SAMPLE_RATE, Recorder, peak, source_status
from .config import Config, Profile
from .credentials import KeyCache, MissingKeyError
from .deepgram import (
    DeepgramError,
    LiveTranscriber,
    batch_url,
    stream_url,
    transcribe_recording,
)
from .text import assemble

log = logging.getLogger(__name__)

_FINAL_TIMEOUT_S = 10.0
_FALLBACK_TIMEOUT_S = 60.0
_MIC_START_TIMEOUT_S = 3.0


class MicMutedError(Exception):
    def __init__(self) -> None:
        super().__init__("Your microphone is muted. Unmute it (mic-mute key) and try again.")


class MicStartError(Exception):
    def __init__(self) -> None:
        super().__init__("The microphone did not start. Is PipeWire running?")


class MicMissingError(Exception):
    def __init__(self, source: str) -> None:
        super().__init__(
            f"The configured audio_source {source!r} does not exist "
            "(see `pactl list short sources`)."
        )


class NoAudioError(Exception):
    def __init__(self) -> None:
        super().__init__("The microphone recorded pure silence. Is it muted or disconnected?")


class TranscriptionFailedError(Exception):
    def __init__(self, cause: Exception, saved: Path | None) -> None:
        where = f" Your audio was saved to {saved}." if saved else ""
        super().__init__(f"{cause}{where}")
        self.saved = saved


def is_fatal(exc: BaseException) -> bool:
    """Errors that retrying with the same key cannot fix; there is no point to keep recording."""
    return isinstance(exc, MissingKeyError) or (isinstance(exc, DeepgramError) and exc.auth)


class Session:
    def __init__(
        self,
        profile: Profile,
        cfg: Config,
        keys: KeyCache,
        on_fatal: Callable[[BaseException], None] = lambda _e: None,
    ) -> None:
        self.profile = profile
        self._cfg = cfg
        self._keys = keys
        self._on_fatal = on_fatal
        self._recorder = Recorder(cfg.audio_source)
        self._pcm = bytearray()
        self._peak = 0
        self._new_audio = asyncio.Event()
        self._first_audio = asyncio.Event()
        self._capture_done = False
        self._capture_task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[list[str]] | None = None
        self._live: LiveTranscriber | None = None
        self.started_at = 0.0

    @property
    def seconds_recorded(self) -> float:
        return len(self._pcm) / BYTES_PER_SECOND

    async def start(self) -> None:
        """Start recording; returns once audio is actually flowing, so "listening" is true."""
        # The source check runs while the microphone starts, so it adds no delay.
        status = asyncio.create_task(source_status(self._cfg.audio_source))
        await self._recorder.start()
        self.started_at = time.monotonic()
        self._capture_task = asyncio.create_task(self._capture())
        match await status:
            case "muted":
                raise MicMutedError
            case "missing":
                raise MicMissingError(self._cfg.audio_source or "default")
        self._stream_task = asyncio.create_task(self._stream())
        self._stream_task.add_done_callback(self._stream_done)
        try:
            await asyncio.wait_for(self._first_audio.wait(), _MIC_START_TIMEOUT_S)
        except TimeoutError:
            raise MicStartError from None
        log.debug("microphone live after %.0f ms", (time.monotonic() - self.started_at) * 1000)

    async def _capture(self) -> None:
        try:
            async for chunk in self._recorder.chunks():
                self._pcm += chunk
                self._peak = max(self._peak, peak(chunk))
                self._first_audio.set()
                self._new_audio.set()
        finally:
            self._capture_done = True
            self._new_audio.set()

    async def _stream(self) -> list[str]:
        key = await self._keys.get()
        self._live = LiveTranscriber(stream_url(self.profile, self._cfg), key)
        await self._live.connect()
        sent = 0
        while True:
            await self._new_audio.wait()
            self._new_audio.clear()
            end = len(self._pcm) & ~1  # whole 16-bit samples only
            if end > sent:
                await self._live.send(bytes(self._pcm[sent:end]))
                sent = end
            if self._capture_done:
                break
        return await self._live.finish(_FINAL_TIMEOUT_S)

    async def switch_profile(self, profile: Profile) -> None:
        """Transcribe in another language from now on, including what was already said.

        The recording so far is kept in memory, so a fresh stream in the new language
        replays it from the very first sample; nothing is lost by switching late.
        """
        if profile == self.profile:
            return
        self.profile = profile
        if self._stream_task is None:
            return  # not streaming yet; start() will use the new profile
        old_task, old_live = self._stream_task, self._live
        self._stream_task = asyncio.create_task(self._stream())
        self._stream_task.add_done_callback(self._stream_done)
        old_task.cancel()
        with contextlib.suppress(BaseException):
            await old_task
        if old_live is not None:
            with contextlib.suppress(Exception):
                await old_live.abort()

    def _stream_done(self, task: asyncio.Task[list[str]]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and is_fatal(exc):
            self._on_fatal(exc)

    async def finish(self) -> str:
        """Stop recording and return the text to type ("" if nothing was said)."""
        assert self._capture_task and self._stream_task
        await asyncio.sleep(self._cfg.tail_ms / 1000)
        self._recorder.stop()
        await self._capture_task
        log.info("recorded %.1fs of audio", self.seconds_recorded)
        if self._peak == 0:
            await self._discard_stream()
            raise NoAudioError

        try:
            segments = await asyncio.wait_for(self._stream_task, _FINAL_TIMEOUT_S + 10)
        except (MissingKeyError, asyncio.CancelledError):
            raise
        except DeepgramError as e:
            if e.auth:
                self._keys.invalidate()
                raise
            segments = await self._fallback(e)
        except Exception as e:  # any other live-path failure also gets the upload fallback
            segments = await self._fallback(e)
        return assemble(segments, trailing_space=self.profile.trailing_space)

    async def _fallback(self, cause: Exception) -> list[str]:
        log.warning("live stream failed (%s); uploading the recording instead", cause)
        await self._discard_stream()
        try:
            key = await self._keys.get()
            return await transcribe_recording(
                batch_url(self.profile, self._cfg), key, bytes(self._pcm), _FALLBACK_TIMEOUT_S
            )
        except Exception as e:
            raise TranscriptionFailedError(e, self._save_recording()) from e

    def _save_recording(self) -> Path | None:
        try:
            folder = paths.cache_dir() / "failed"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("%Y%m%d-%H%M%S.wav")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(bytes(self._pcm))
            return path
        except OSError:
            log.exception("could not save the failed recording")
            return None

    async def _discard_stream(self) -> None:
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stream_task
        if self._live:
            with contextlib.suppress(Exception):
                await self._live.abort()

    async def cancel(self) -> None:
        self._recorder.kill()
        if self._capture_task:
            self._capture_task.cancel()
            with contextlib.suppress(BaseException):
                await self._capture_task
        await self._discard_stream()
