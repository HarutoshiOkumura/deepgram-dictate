"""Is a key physically held down right now? Asked of the kernel's input devices.

Hyprland does not deliver a release bind for a modifier that was used in a key combination
(e.g. Right Alt + Tab). When that happens the daemon asks here whether the hotkey is still
down, so it never waits forever for a release that will not come.

This only queries the instantaneous key-state bitmap (EVIOCGKEY); it never reads the event
stream, so no keystrokes are seen. It needs read access to /dev/input/event*, which members of
the `input` group have. Without it, the answer is None ("unknown").

Devices are picked by reading their key capabilities from sysfs, so only keyboards that have
the key are ever opened, and they are kept open: closing an evdev device costs the kernel
several milliseconds.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

KEY_RIGHTALT = 100  # from linux/input-event-codes.h

_KEY_MAX = 0x2FF
_BITMAP_BYTES = _KEY_MAX // 8 + 1
_IOC_READ = 2
_EVIOCGKEY = (_IOC_READ << 30) | (_BITMAP_BYTES << 16) | (ord("E") << 8) | 0x18
_LONG_BITS = 64


def has_key(capabilities: str, code: int) -> bool:
    """Parse sysfs `capabilities/key`: hex longs, most significant first."""
    words = capabilities.split()
    index = len(words) - 1 - code // _LONG_BITS
    return index >= 0 and bool(int(words[index], 16) >> (code % _LONG_BITS) & 1)


class KeyState:
    def __init__(self, code: int = KEY_RIGHTALT, sysfs: Path = Path("/sys/class/input")) -> None:
        self._code = code
        self._sysfs = sysfs
        self._fds: dict[str, int] = {}

    def _keyboards(self) -> set[str]:
        found = set()
        for caps in self._sysfs.glob("event*/device/capabilities/key"):
            try:
                if has_key(caps.read_text(), self._code):
                    found.add(caps.parent.parent.parent.name)
            except (OSError, ValueError):
                continue
        return found

    def refresh(self) -> None:
        """Pick up keyboards plugged in (or removed) since the last call."""
        current = self._keyboards()
        for name in set(self._fds) - current:
            os.close(self._fds.pop(name))
        for name in current - set(self._fds):
            try:
                self._fds[name] = os.open(
                    f"/dev/input/{name}", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
                )
            except OSError:
                continue

    def is_down(self) -> bool | None:
        """True if any keyboard reports the key held, False if none do, None if unknown."""
        if not self._fds:
            return None
        down = False
        for name, fd in list(self._fds.items()):
            state = bytearray(_BITMAP_BYTES)
            try:
                fcntl.ioctl(fd, _EVIOCGKEY, state)
            except OSError:  # unplugged
                os.close(self._fds.pop(name))
                continue
            down = down or bool(state[self._code // 8] & (1 << (self._code % 8)))
        return down if self._fds else None

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()
