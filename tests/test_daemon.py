"""The client/daemon protocol over a real Unix socket."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deepgram_dictate import client, daemon
from deepgram_dictate.config import Config, Profile


class FakeSession:
    async def start(self) -> None:
        pass

    async def finish(self) -> str:
        return "typed text "

    async def cancel(self) -> None:
        pass


@pytest.fixture
async def running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    d = daemon.Daemon(Config(sounds=False, profiles={"en": Profile("en"), "ja": Profile("ja")}))
    seen: dict = {"typed": [], "profiles": [], "env": [], "errors": []}

    class FakeFeedback:  # no real chimes or desktop notifications from the test suite
        def cue(self, name: str) -> None:
            pass

        def error(self, summary: str, body: str = "") -> None:
            seen["errors"].append(f"{summary}: {body}")

    monkeypatch.setattr(d, "_feedback", FakeFeedback())

    def new_session(profile, on_fatal):
        seen["profiles"].append(profile.name)
        return FakeSession()

    async def deliver(text: str) -> None:
        seen["typed"].append(text)
        seen["env"].append(d._env().get("WAYLAND_DISPLAY"))

    monkeypatch.setattr(d.controller, "_new_session", new_session)
    monkeypatch.setattr(d.controller, "_deliver", deliver)
    # The real key-state watchdog would see Right Alt physically up and cancel the hold.
    monkeypatch.setattr(d.controller, "_keys", None)
    sock, owned = daemon._listening_socket()
    assert owned  # not socket-activated: we bound it, so we clean it up
    server = await asyncio.start_unix_server(d.handle, sock=sock, cleanup_socket=owned)
    async with server:
        yield seen


async def ask(cmd: str, **fields: object) -> dict:
    return await asyncio.to_thread(client.request, cmd, **fields)


async def test_round_trip(running: dict) -> None:
    assert (await ask("status"))["state"] == "idle"
    reply = await ask("press", profile="ja")
    assert reply == {"ok": True, "result": "started", "state": "recording", "mode": "held",
                     "profile": "ja", "label": "JA"}  # fmt: skip
    await asyncio.sleep(0.45)
    assert (await ask("release", profile="ja"))["result"] == "stopped"
    await asyncio.sleep(0.05)
    assert running["typed"] == ["typed text "]
    assert running["profiles"] == ["ja"]
    assert running["env"] == ["wayland-test"]  # forwarded from the client


async def test_errors_are_replies_not_crashes(running: dict) -> None:
    assert (await ask("press", profile="klingon"))["ok"] is False
    assert "unknown command" in (await ask("explode"))["error"]
    assert (await ask("status"))["ok"] is True


async def test_unconfigured_language_is_shown_to_the_user(running: dict) -> None:
    """Keybind clients have no visible output, so the daemon must notify instead."""
    assert (await ask("switch", profile="yue"))["ok"] is False
    assert len(running["errors"]) == 1
    assert "'yue'" in running["errors"][0] and "examples/config.toml" in running["errors"][0]


async def test_subscribe_streams_states(running: dict) -> None:
    states: list[str] = []

    def watch() -> None:
        for s in client.follow():
            states.append(s["state"])
            if len(states) == 3:
                return

    watcher = asyncio.create_task(asyncio.to_thread(watch))
    await asyncio.sleep(0.05)
    await ask("toggle")
    await ask("toggle")
    await asyncio.wait_for(watcher, 2)
    assert states[:2] == ["idle", "recording"]
    assert states[2] in ("transcribing", "idle")
