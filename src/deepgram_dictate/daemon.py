"""The background service: owns the hotkey state machine and listens on a Unix socket.

Protocol: the client sends one JSON object per line, e.g. {"cmd": "press", "profile": "en",
"env": {...}}, and gets one JSON line back. {"cmd": "subscribe"} instead keeps the connection
open and streams a JSON line on every state change (used by the status bar indicator).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket

from . import config as config_mod
from . import inject, paths
from .client import FORWARDED_ENV
from .config import Config, ConfigError, Profile
from .controller import Controller
from .credentials import KeyCache, MissingKeyError
from .deepgram import DeepgramError
from .feedback import Feedback
from .keystate import KEY_RIGHTALT, KeyState
from .session import MicMissingError, MicMutedError, MicStartError, NoAudioError, Session

log = logging.getLogger(__name__)

_SD_LISTEN_FDS_START = 3
_EVENT_CUES = {
    "started": "start",
    "locked": "lock",
    "switched": "switch",
    "stopped": "stop",
    "cancelled": "cancel",
}


def _listening_socket() -> tuple[socket.socket, bool]:
    """Use the socket systemd passed in (socket activation), or bind our own.

    Returns the socket and whether it is ours to delete on exit. systemd's socket must
    survive us: it is what starts the next daemon instance.
    """
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        for var in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
            os.environ.pop(var, None)
        return socket.socket(fileno=_SD_LISTEN_FDS_START), False
    path = paths.socket_path()
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old_umask)
    sock.listen(16)
    return sock, True


def _explain(exc: BaseException | None) -> tuple[str, str]:
    if isinstance(exc, MicMutedError):
        return "Microphone is muted", str(exc)
    if isinstance(exc, (NoAudioError, MicStartError, MicMissingError)):
        return "No audio", str(exc)
    if isinstance(exc, MissingKeyError):
        return "Deepgram API key missing", str(exc)
    if isinstance(exc, DeepgramError):
        return "Transcription failed", str(exc)
    if isinstance(exc, inject.InjectError):
        return "Could not type the text", str(exc)
    return "Dictation failed", f"{type(exc).__name__}: {exc}"


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._keys = KeyCache()
        self._feedback = Feedback(sounds=cfg.sounds, volume=cfg.sound_volume)
        self._client_env: dict[str, str] = {}
        self.controller = Controller(
            cfg, self._new_session, self._deliver, self._report, keys=KeyState(KEY_RIGHTALT)
        )

    def _new_session(self, profile: Profile, on_fatal) -> Session:
        return Session(profile, self.cfg, self._keys, on_fatal)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._client_env)
        return env

    async def _deliver(self, text: str) -> None:
        await inject.deliver(text, self.cfg.inject, self._env())

    def _report(self, event: str, exc: BaseException | None) -> None:
        log.info("event: %s%s", event, f" ({exc})" if exc else "")
        if cue := _EVENT_CUES.get(event):
            self._feedback.cue(cue)
        elif exc is not None:
            self._feedback.error(*_explain(exc))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            env = msg.get("env") or {}
            self._client_env.update({k: v for k, v in env.items() if k in FORWARDED_ENV})
            if msg.get("cmd") == "subscribe":
                await self._stream_state(reader, writer)
                return
            reply = await self._command(msg)
        except ConfigError as e:
            # Most likely a keybind naming a language profile that is not configured. The
            # client runs from a keybind, where nobody sees its output, so say it here.
            self._feedback.error(
                "Dictation is not set up for that",
                f"{e}. Add it to {paths.config_path()} (see examples/config.toml).",
            )
            reply = {"ok": False, "error": str(e)}
        except ValueError as e:
            reply = {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("error handling a command")
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with contextlib.suppress(ConnectionError):
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        writer.close()

    async def _command(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        c = self.controller
        if cmd in ("press", "release", "toggle", "switch"):
            if cmd == "switch" and not msg.get("profile"):
                raise ValueError("switch needs a profile, e.g. `dictate-key switch ja`")
            profile = self.cfg.profile(msg.get("profile"))
            result = await getattr(c, cmd)(profile)
        elif cmd == "cancel":
            result = await c.cancel()
        elif cmd == "status":
            return {"ok": True, "result": "ok", **c.state()}
        else:
            raise ValueError(f"unknown command {cmd!r}")
        log.info("%s(%s) -> %s", cmd, msg.get("profile") or "", result)
        return {"ok": True, "result": result, **c.state()}

    async def _stream_state(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        def send(state: dict) -> None:
            if not writer.is_closing():
                writer.write(json.dumps(state).encode() + b"\n")

        unsubscribe = self.controller.subscribe(send)
        try:
            await reader.read()  # returns when the client disconnects
        finally:
            unsubscribe()
            writer.close()


async def run(cfg: Config) -> None:
    daemon = Daemon(cfg)
    sock, owned = _listening_socket()
    server = await asyncio.start_unix_server(daemon.handle, sock=sock, cleanup_socket=owned)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("listening on %s", paths.socket_path())
    async with server:
        await stop.wait()
        log.info("shutting down")
        await daemon.controller.shutdown()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("DICTATE_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = config_mod.load()
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(1) from e
    asyncio.run(run(cfg))
