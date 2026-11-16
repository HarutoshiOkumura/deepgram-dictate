"""The hotkey state machine.

One key does everything:

* press, release after `hold_threshold_ms` or more -> push-to-talk: stops on release
* press and release quickly (a tap)               -> hands-free: keeps recording
* press again while hands-free                    -> stops (and its release is ignored)
* hotkey + a language key (`switch`)              -> that language, hands-free
* `cancel` at any time                            -> discard the recording, type nothing

Hyprland never delivers the release of a modifier that was part of a key combination. So
`switch` does not wait for one, and while the key is held a watchdog asks the kernel whether
it is still down: if it went up without a release event (e.g. Right Alt + Tab), the
recording is cancelled instead of running forever.

Text from finished dictations is delivered strictly in the order the dictations started,
even if a later one transcribes faster.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .config import Config, Profile

log = logging.getLogger(__name__)

# Identical commands closer together than this are one key event bound twice.
DEBOUNCE_S = 0.1
# A press this soon after a `switch` started a dictation belongs to the same key combination
# (the two hotkey processes raced and the language key won).
CHORD_RACE_S = 0.3
# How often the watchdog looks at the physical key while it is held.
WATCH_INTERVAL_S = 0.1
# After the key is seen up, how long the release command gets to arrive before it is missed.
RELEASE_GRACE_S = 0.3


class SessionLike(Protocol):
    async def start(self) -> None: ...
    async def finish(self) -> str: ...
    async def cancel(self) -> None: ...
    async def switch_profile(self, profile: Profile) -> None: ...


class KeyStateLike(Protocol):
    def refresh(self) -> None: ...
    def is_down(self) -> bool | None: ...


class Mode(enum.Enum):
    HELD = "held"
    LOCKED = "locked"


@dataclass
class _Active:
    session: SessionLike
    profile: Profile
    mode: Mode
    pressed_at: float
    timer: asyncio.TimerHandle | None = None
    watchdog: asyncio.Task[None] | None = None


SessionFactory = Callable[[Profile, Callable[[BaseException], None]], SessionLike]
Deliver = Callable[[str], Awaitable[None]]
Report = Callable[[str, BaseException | None], None]


class Controller:
    def __init__(
        self,
        cfg: Config,
        new_session: SessionFactory,
        deliver: Deliver,
        report: Report,
        clock: Callable[[], float] = time.monotonic,
        keys: KeyStateLike | None = None,
    ) -> None:
        self._cfg = cfg
        self._new_session = new_session
        self._deliver = deliver
        self._report = report
        self._clock = clock
        self._keys = keys
        self._active: _Active | None = None
        self._swallow_release = False
        self._chord_until = 0.0
        self._last_command: dict[str, float] = {}
        self._pending: set[asyncio.Task[None]] = set()
        self._last_delivery: asyncio.Future[None] | None = None
        self._listeners: list[Callable[[dict], None]] = []

    # -- state reporting ---------------------------------------------------------------

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        listener(self.state())
        return lambda: self._listeners.remove(listener)

    def state(self) -> dict:
        if self._active:
            return {
                "state": "recording",
                "mode": self._active.mode.value,
                "profile": self._active.profile.name,
                "label": self._active.profile.display_label,
            }
        if self._pending:
            return {"state": "transcribing", "mode": None, "profile": None, "label": None}
        return {"state": "idle", "mode": None, "profile": None, "label": None}

    def _changed(self) -> None:
        snapshot = self.state()
        for listener in list(self._listeners):
            listener(snapshot)

    # -- commands ----------------------------------------------------------------------

    def _duplicate(self, command: str) -> bool:
        now = self._clock()
        last = self._last_command.get(command)
        self._last_command[command] = now
        return last is not None and now - last < DEBOUNCE_S

    async def press(self, profile: Profile) -> str:
        if self._duplicate("press"):
            return "ignored"
        active = self._active
        if active is None:
            self._swallow_release = False
            return await self._start(profile, Mode.HELD)
        if active.mode is Mode.LOCKED:
            if self._clock() < self._chord_until:
                self._swallow_release = True
                return "ignored"  # part of the hotkey + language key combination
            self._swallow_release = True
            self._stop()
            return "stopped"
        # Held, yet pressed again: the release in between was never delivered.
        await self._discard(active, "stale")
        return await self._start(profile, Mode.HELD)

    async def release(self, profile: Profile) -> str:
        if self._duplicate("release"):
            return "ignored"
        if self._swallow_release:
            self._swallow_release = False
            return "ignored"
        active = self._active
        if active is None or active.mode is not Mode.HELD:
            return "ignored"
        if self._clock() - active.pressed_at >= self._cfg.hold_threshold_ms / 1000:
            self._stop()
            return "stopped"
        self._lock(active)
        self._report("locked", None)
        self._changed()
        return "locked"

    async def switch(self, profile: Profile) -> str:
        """Hotkey + language key: dictate in `profile`, hands-free."""
        if self._duplicate("switch"):
            return "ignored"
        active = self._active
        if active is None:
            self._chord_until = self._clock() + CHORD_RACE_S
            return await self._start(profile, Mode.LOCKED)
        if active.mode is Mode.HELD:
            # The hotkey's own release will not be delivered after a combination; if it is
            # anyway, it must not stop the dictation just switched to.
            self._lock(active)
            self._swallow_release = True
        if active.profile != profile:
            active.profile = profile
            await active.session.switch_profile(profile)
        self._report("switched", None)
        self._changed()
        return "switched"

    async def toggle(self, profile: Profile) -> str:
        if self._active is None:
            return await self._start(profile, Mode.LOCKED)
        self._stop()
        return "stopped"

    async def cancel(self) -> str:
        active = self._active
        if active is None:
            return "ignored"
        self._swallow_release = False
        await self._discard(active, "cancelled")
        return "cancelled"

    async def shutdown(self) -> None:
        await self.cancel()
        for task in list(self._pending):
            task.cancel()

    # -- internals ---------------------------------------------------------------------

    def _detach(self, active: _Active) -> None:
        if self._active is active:
            self._active = None
        if active.timer:
            active.timer.cancel()
        if active.watchdog and active.watchdog is not asyncio.current_task():
            active.watchdog.cancel()

    def _lock(self, active: _Active) -> None:
        active.mode = Mode.LOCKED
        if active.watchdog:
            active.watchdog.cancel()
            active.watchdog = None

    async def _discard(self, active: _Active, event: str) -> None:
        self._detach(active)
        await active.session.cancel()
        self._report(event, None)
        self._changed()

    async def _start(self, profile: Profile, mode: Mode) -> str:
        session = self._new_session(profile, self._on_fatal)
        active = _Active(session, profile, mode, self._clock())
        self._active = active
        if mode is Mode.HELD and self._keys:
            self._keys.refresh()
        try:
            await session.start()
        except Exception as e:
            self._detach(active)
            await session.cancel()
            self._report("start-failed", e)
            self._changed()
            return "failed"
        if self._active is not active:
            return "cancelled"  # cancelled while the microphone was starting
        active.timer = asyncio.get_running_loop().call_later(
            self._cfg.max_session_s, self._time_limit, active
        )
        if active.mode is Mode.HELD and self._keys:
            active.watchdog = asyncio.create_task(self._watch_hold(active))
        self._report("started", None)
        self._changed()
        return "started"

    async def _watch_hold(self, active: _Active) -> None:
        assert self._keys is not None
        while self._active is active and active.mode is Mode.HELD:
            if self._keys.is_down() is False:
                await asyncio.sleep(RELEASE_GRACE_S)
                if self._active is active and active.mode is Mode.HELD:
                    log.info("hotkey went up without a release event (key combination)")
                    await self._discard(active, "cancelled")
                return
            await asyncio.sleep(WATCH_INTERVAL_S)

    def _time_limit(self, active: _Active) -> None:
        if self._active is active:
            log.warning("dictation hit the %ss limit; stopping", self._cfg.max_session_s)
            self._swallow_release = False
            self._stop()

    def _on_fatal(self, exc: BaseException) -> None:
        """The session cannot succeed (e.g. bad API key): stop listening right away."""
        active = self._active
        if active is None:
            return
        self._detach(active)
        self._swallow_release = False

        async def abort() -> None:
            await active.session.cancel()
            self._report("failed", exc)
            self._changed()

        self._track(asyncio.create_task(abort()))

    def _stop(self) -> None:
        active = self._active
        assert active is not None
        self._detach(active)
        previous = self._last_delivery
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._last_delivery = done
        self._report("stopped", None)
        self._track(asyncio.create_task(self._finish(active, previous, done)))
        self._changed()

    async def _finish(
        self,
        active: _Active,
        previous: asyncio.Future[None] | None,
        done: asyncio.Future[None],
    ) -> None:
        try:
            try:
                text = await active.session.finish()
            except Exception as e:
                self._report("failed", e)
                return
            if previous is not None:
                await asyncio.shield(previous)
            try:
                await self._deliver(text)
            except Exception as e:
                self._report("deliver-failed", e)
                return
            self._report("delivered" if text else "empty", None)
        finally:
            done.set_result(None)

    def _track(self, task: asyncio.Task[None]) -> None:
        self._pending.add(task)

        def finished(t: asyncio.Task[None]) -> None:
            self._pending.discard(t)
            self._changed()

        task.add_done_callback(finished)
