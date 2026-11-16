"""`dictate-key <press|release|toggle|switch|cancel> [profile]`: what Hyprland keybinds run.

Every millisecond here delays the microphone, so this imports only `os`, `socket` and `sys`
(importing `json` alone would cost ~35 ms). The message is a flat object of short strings,
which `_json_str` encodes exactly. Errors go to stderr; the daemon reports user-facing ones.
"""

import os
import socket
import sys

_COMMANDS = ("press", "release", "toggle", "switch", "cancel")
_FORWARDED_ENV = ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY")


def _json_str(value: str) -> str:
    """Encode a string as a JSON string literal (RFC 8259 escaping)."""
    out = ['"']
    for ch in value:
        if ch == '"' or ch == "\\":
            out.append("\\" + ch)
        elif ch < " ":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def encode(cmd: str, profile: str | None, env: dict[str, str]) -> bytes:
    env_json = ",".join(f"{_json_str(k)}:{_json_str(v)}" for k, v in env.items())
    profile_json = f',"profile":{_json_str(profile)}' if profile else ""
    return f'{{"cmd":{_json_str(cmd)}{profile_json},"env":{{{env_json}}}}}\n'.encode()


def main() -> int:
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in _COMMANDS:
        print(f"usage: dictate-key {{{','.join(_COMMANDS)}}} [profile]", file=sys.stderr)
        return 2
    env = {k: os.environ[k] for k in _FORWARDED_ENV if k in os.environ}
    message = encode(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None, env)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    path = os.environ.get("DEEPGRAM_DICTATE_SOCKET") or f"{runtime}/deepgram-dictate.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(path)
            sock.sendall(message)
            # Wait for the reply so the daemon never sees a half-closed connection.
            sock.recv(4096)
    except OSError as e:
        print(f"dictate-key: cannot reach the dictation daemon: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
