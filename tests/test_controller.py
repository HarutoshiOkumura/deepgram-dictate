import asyncio
from collections.abc import Callable

import pytest

from deepgram_dictate.config import Config, Profile
from deepgram_dictate.controller import Controller

EN = Profile(name="en")


class FakeSession:
    def __init__(self, text: str, *, fail_start: Exception | None = None) -> None:
        self.text = text
        self.fail_start = fail_start
        self.started = self.cancelled = False
        self.profiles: list[str] = []
        self.release_finish = asyncio.Event()
        self.release_finish.set()

    async def start(self) -> None:
        if self.fail_start:
            raise self.fail_start
        self.started = True

    async def switch_profile(self, profile: Profile) -> None:
        self.profiles.append(profile.name)

    async def finish(self) -> str:
        await self.release_finish.wait()
        return self.text

    async def cancel(self) -> None:
        self.cancelled = True


class FakeKeys:
    def __init__(self) -> None:
        self.down: bool | None = True
        self.refreshed = 0

    def refresh(self) -> None:
        self.refreshed += 1

    def is_down(self) -> bool | None:
        return self.down


class Harness:
    def __init__(self, texts: list[str], keys: FakeKeys | None = None, **cfg: object) -> None:
        self.now = 0.0
        self.texts = list(texts)
        self.sessions: list[FakeSession] = []
        self.typed: list[str] = []
        self.events: list[str] = []
        self.fatal: list[Callable[[BaseException], None]] = []
        self.c = Controller(
            Config(**cfg),  # type: ignore[arg-type]
            self._new_session,
            self._deliver,
            lambda event, exc: self.events.append(event),
            clock=lambda: self.now,
            keys=keys,
        )

    def _new_session(self, profile, on_fatal):
        s = FakeSession(self.texts.pop(0))
        self.sessions.append(s)
        self.fatal.append(on_fatal)
        return s

    async def _deliver(self, text: str) -> None:
        self.typed.append(text)

    async def settle(self) -> None:
        for _ in range(10):
            await asyncio.sleep(0)


async def test_hold_to_talk() -> None:
    h = Harness(["hello "])
    assert await h.c.press(EN) == "started"
    assert h.c.state() == {"state": "recording", "mode": "held", "profile": "en", "label": "EN"}
    h.now = 2.0
    assert await h.c.release(EN) == "stopped"
    await h.settle()
    assert h.typed == ["hello "]
    assert h.c.state()["state"] == "idle"
    assert h.events == ["started", "stopped", "delivered"]


async def test_tap_locks_then_tap_stops() -> None:
    h = Harness(["long dictation "])
    await h.c.press(EN)
    h.now = 0.15
    assert await h.c.release(EN) == "locked"
    assert h.c.state()["mode"] == "locked"
    h.now = 30.0
    assert await h.c.press(EN) == "stopped"
    # The release belonging to the stopping press must not start or stop anything.
    h.now = 30.1
    assert await h.c.release(EN) == "ignored"
    await h.settle()
    assert h.typed == ["long dictation "]
    assert h.c.state()["state"] == "idle"


async def test_duplicate_commands_are_ignored() -> None:
    """The hotkey is bound for both modifier states, so one key event can arrive twice."""
    h = Harness(["x "])
    await h.c.press(EN)
    h.now = 0.05
    assert await h.c.press(EN) == "ignored"
    assert len(h.sessions) == 1
    h.now = 1.0
    assert await h.c.release(EN) == "stopped"
    h.now = 1.02
    assert await h.c.release(EN) == "ignored"


async def test_press_while_held_replaces_a_stale_dictation() -> None:
    """Held, then pressed again: the release in between was lost, so start over."""
    h = Harness(["stale ", "fresh "])
    await h.c.press(EN)
    h.now = 5.0
    assert await h.c.press(EN) == "started"
    assert h.sessions[0].cancelled
    assert h.c.state()["mode"] == "held"
    h.now = 6.0
    await h.c.release(EN)
    await h.settle()
    assert h.typed == ["fresh "]


async def test_threshold_boundary() -> None:
    h = Harness(["a ", "b "], hold_threshold_ms=400)
    await h.c.press(EN)
    h.now = 0.399
    assert await h.c.release(EN) == "locked"
    await h.c.press(EN)
    await h.c.release(EN)
    h.now = 1.0
    await h.c.press(EN)
    h.now = 1.5
    assert await h.c.release(EN) == "stopped"


async def test_cancel_discards() -> None:
    h = Harness(["never typed ", "next "])
    await h.c.press(EN)
    await h.c.release(EN)  # tap: hands-free
    assert await h.c.cancel() == "cancelled"
    await h.settle()
    assert h.sessions[0].cancelled
    assert h.typed == []
    assert await h.c.cancel() == "ignored"
    # The next press starts fresh rather than being swallowed.
    h.now = 1.0
    assert await h.c.press(EN) == "started"


async def test_toggle() -> None:
    h = Harness(["toggled "])
    assert await h.c.toggle(EN) == "started"
    assert h.c.state()["mode"] == "locked"
    assert await h.c.toggle(EN) == "stopped"
    await h.settle()
    assert h.typed == ["toggled "]


async def test_start_failure_reports_and_returns_to_idle() -> None:
    h = Harness([])
    h.c._new_session = lambda p, f: FakeSession("", fail_start=RuntimeError("muted"))
    assert await h.c.press(EN) == "failed"
    assert h.c.state()["state"] == "idle"
    assert h.events == ["start-failed"]
    assert await h.c.release(EN) == "ignored"


async def test_deliveries_keep_start_order() -> None:
    h = Harness(["first ", "second "])
    await h.c.toggle(EN)
    first = h.sessions[0]
    first.release_finish.clear()  # the first transcript is slow to arrive
    await h.c.toggle(EN)
    await h.c.toggle(EN)
    await h.c.toggle(EN)
    await h.settle()
    assert h.typed == []  # the second is ready but must wait for the first
    assert h.c.state()["state"] == "transcribing"
    first.release_finish.set()
    await h.settle()
    assert h.typed == ["first ", "second "]
    assert h.c.state()["state"] == "idle"


async def test_fatal_error_stops_recording_immediately() -> None:
    h = Harness(["x "])
    await h.c.press(EN)
    await h.c.release(EN)  # hands-free
    h.fatal[0](RuntimeError("bad key"))
    await h.settle()
    assert h.sessions[0].cancelled
    assert h.c.state()["state"] == "idle"
    assert "failed" in h.events
    assert h.typed == []


async def test_time_limit_stops_hands_free() -> None:
    h = Harness(["limited "], max_session_s=0)
    await h.c.toggle(EN)
    await asyncio.sleep(0.01)
    await h.settle()
    assert h.typed == ["limited "]


@pytest.mark.parametrize("text", ["", "words "])
async def test_subscribers_see_every_transition(text: str) -> None:
    h = Harness([text])
    seen: list[str] = []
    h.c.subscribe(lambda s: seen.append(s["state"] + (f":{s['mode']}" if s["mode"] else "")))
    await h.c.press(EN)
    h.now = 0.2
    await h.c.release(EN)
    h.now = 3.0
    await h.c.press(EN)
    await h.settle()
    assert seen[0] == "idle"
    assert "recording:held" in seen
    assert "recording:locked" in seen
    assert seen[-1] == "idle"


YUE = Profile(name="yue", language="zh-HK", label="粵")
JA = Profile(name="ja", language="ja")


async def test_language_key_while_holding_switches_and_locks() -> None:
    """Right Alt down, then C: Cantonese, hands-free. The Right Alt release may not come."""
    h = Harness(["粵語 "])
    await h.c.press(EN)
    h.now = 0.1
    assert await h.c.switch(YUE) == "switched"
    assert h.sessions[0].profiles == ["yue"]
    assert h.c.state() == {"state": "recording", "mode": "locked", "profile": "yue", "label": "粵"}
    h.now = 0.2
    assert await h.c.release(EN) == "ignored"  # if Hyprland delivers it after all
    h.now = 9.0
    assert await h.c.press(EN) == "stopped"  # a plain tap of Right Alt finishes
    h.now = 9.1
    assert await h.c.release(EN) == "ignored"
    await h.settle()
    assert h.typed == ["粵語 "]
    assert h.events == ["started", "switched", "stopped", "delivered"]


async def test_language_key_without_release_event_then_tap_to_stop() -> None:
    h = Harness(["日本語 "])
    await h.c.press(EN)
    h.now = 0.1
    await h.c.switch(JA)
    # Hyprland swallows the Right Alt release after a combination: no release() call.
    h.now = 7.0
    assert await h.c.press(EN) == "stopped"
    h.now = 7.1
    assert await h.c.release(EN) == "ignored"
    await h.settle()
    assert h.typed == ["日本語 "]


async def test_language_key_that_wins_the_race_starts_dictation() -> None:
    """The language key's command can reach the daemon before Right Alt's own press."""
    h = Harness(["race "])
    assert await h.c.switch(YUE) == "started"
    h.now = 0.05
    assert await h.c.press(EN) == "ignored"  # same key combination, not a stop
    h.now = 0.15
    assert await h.c.release(EN) == "ignored"
    assert h.c.state()["profile"] == "yue"
    assert h.c.state()["mode"] == "locked"
    h.now = 4.0
    assert await h.c.press(EN) == "stopped"


async def test_language_key_after_stopping_starts_the_next_dictation() -> None:
    """Hands-free English, then Right Alt + J: English is typed, Japanese starts."""
    h = Harness(["english ", "日本語 "])
    await h.c.toggle(EN)
    h.now = 5.0
    assert await h.c.press(EN) == "stopped"
    h.now = 5.1
    assert await h.c.switch(JA) == "started"
    await h.settle()
    assert h.typed == ["english "]
    assert h.c.state()["profile"] == "ja"


async def test_switching_to_the_same_language_does_not_restart() -> None:
    h = Harness(["x "])
    await h.c.toggle(YUE)
    h.now = 1.0
    await h.c.switch(YUE)
    assert h.sessions[0].profiles == []


@pytest.fixture
def fast_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepgram_dictate import controller

    monkeypatch.setattr(controller, "WATCH_INTERVAL_S", 0.005)
    monkeypatch.setattr(controller, "RELEASE_GRACE_S", 0.02)


async def test_combination_with_another_key_cancels(fast_watchdog: None) -> None:
    """Right Alt + Tab: Hyprland eats the release, the watchdog sees the key up."""
    keys = FakeKeys()
    h = Harness(["never "], keys=keys)
    await h.c.press(EN)
    assert keys.refreshed == 1
    keys.down = False
    await asyncio.sleep(0.1)
    assert h.sessions[0].cancelled
    assert h.c.state()["state"] == "idle"
    assert h.events == ["started", "cancelled"]
    assert h.typed == []


async def test_watchdog_lets_a_late_release_through(fast_watchdog: None) -> None:
    keys = FakeKeys()
    h = Harness(["held "], keys=keys)
    await h.c.press(EN)
    keys.down = False
    await asyncio.sleep(0.01)  # within the grace period
    h.now = 1.0
    assert await h.c.release(EN) == "stopped"
    await asyncio.sleep(0.05)
    await h.settle()
    assert h.typed == ["held "]
    assert "cancelled" not in h.events


async def test_watchdog_stands_down_when_hands_free(fast_watchdog: None) -> None:
    keys = FakeKeys()
    h = Harness(["free "], keys=keys)
    await h.c.press(EN)
    h.now = 0.1
    await h.c.release(EN)  # tap: hands-free; the key is now legitimately up
    keys.down = False
    await asyncio.sleep(0.1)
    assert h.c.state()["mode"] == "locked"


async def test_unknown_key_state_never_cancels(fast_watchdog: None) -> None:
    keys = FakeKeys()
    keys.down = None  # no access to input devices
    h = Harness(["x "], keys=keys)
    await h.c.press(EN)
    await asyncio.sleep(0.1)
    assert h.c.state()["mode"] == "held"
