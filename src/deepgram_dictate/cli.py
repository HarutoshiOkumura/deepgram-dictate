"""`dictate` command line.

Hotkey commands (press, release, toggle, cancel) only import the standard library, so they
return in a few milliseconds; everything heavier is imported lazily by the command needing it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import client

_MIC_ICON = "\U000f036c"  # nf-md-microphone
# Colours are inline Pango markup rather than CSS so the indicator looks the same in every
# Waybar theme. Catppuccin Mocha red (recording) and peach (transcribing).
_RECORDING_COLOR = "#f38ba8"
_TRANSCRIBING_COLOR = "#fab387"


def _hotkey(cmd: str, args: argparse.Namespace) -> int:
    fields = {"profile": args.profile} if getattr(args, "profile", None) else {}
    try:
        reply = client.request(cmd, **fields)
    except OSError as e:
        print(f"dictate: cannot reach the daemon ({e}). Is it running?", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(f"dictate: {reply.get('error')}", file=sys.stderr)
        return 1
    if args.verbose:
        print(json.dumps(reply))
    return 0


def waybar(state: dict) -> str:
    """One Waybar custom-module JSON line. Empty text hides the module while idle."""
    from html import escape

    name = state.get("state", "idle")
    if name == "recording":
        label = escape(state.get("label") or "")
        hands_free = state.get("mode") == "locked"
        text = f"<span color='{_RECORDING_COLOR}'>{_MIC_ICON}</span> {label}"
        tooltip = (
            f"Dictating ({label}), hands-free\nTap Right Alt to finish · Esc to cancel"
            if hands_free
            else f"Dictating ({label})\nRelease Right Alt to finish"
        )
        cls = ["recording", "locked" if hands_free else "held"]
    elif name == "transcribing":
        text = f"<span color='{_TRANSCRIBING_COLOR}'>{_MIC_ICON}</span> …"
        tooltip, cls = "Transcribing…", ["transcribing"]
    else:
        text, tooltip, cls = "", "", ["idle"]
    return json.dumps(
        {"text": text, "alt": name, "class": cls, "tooltip": tooltip}, ensure_ascii=False
    )


def _status(args: argparse.Namespace) -> int:
    def emit(state: dict) -> None:
        print(waybar(state) if args.waybar else json.dumps(state), flush=True)

    if not args.follow:
        try:
            reply = client.request("status")
        except OSError:
            emit({"state": "stopped"})
            return 1
        emit({k: v for k, v in reply.items() if k not in ("ok", "result")})
        return 0

    # Keep running across daemon restarts; a status bar module should never die.
    while True:
        try:
            for state in client.follow():
                emit(state)
        except OSError:
            pass
        emit({"state": "stopped"})
        time.sleep(2)


def _set_key(_args: argparse.Namespace) -> int:
    import asyncio
    import getpass

    from . import credentials
    from .doctor import check_key

    key = getpass.getpass("Paste your Deepgram API key (input is hidden): ").strip()
    if not key:
        print("No key entered; nothing changed.", file=sys.stderr)
        return 1
    print("Checking the key with Deepgram…")
    ok, detail = asyncio.run(check_key(key))
    if not ok:
        print(f"Deepgram did not accept this key: {detail}. Nothing was stored.", file=sys.stderr)
        return 1
    credentials.store(key)
    print("Key verified and stored in your keyring.")
    return 0


def _daemon(_args: argparse.Namespace) -> int:
    from .daemon import main

    main()
    return 0


def _doctor(_args: argparse.Namespace) -> int:
    from .doctor import main

    return main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dictate", description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="print the daemon's reply")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("press", "hotkey went down"),
        ("release", "hotkey came up"),
        ("toggle", "start, or stop and type"),
        ("switch", "dictate in this profile (hands-free), switching if already dictating"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("profile", nargs="?", help="profile from config.toml (default: en)")
        p.set_defaults(func=lambda a, cmd=name: _hotkey(cmd, a))

    sub.add_parser("cancel", help="discard the current dictation").set_defaults(
        func=lambda a: _hotkey("cancel", a)
    )
    st = sub.add_parser("status", help="show the daemon state")
    st.add_argument("-f", "--follow", action="store_true", help="keep printing every change")
    st.add_argument("--waybar", action="store_true", help="print Waybar custom-module JSON")
    st.set_defaults(func=_status)
    sub.add_parser("daemon", help="run the background service").set_defaults(func=_daemon)
    sub.add_parser("set-key", help="store your Deepgram API key").set_defaults(func=_set_key)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
