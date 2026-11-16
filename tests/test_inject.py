import asyncio
import os
import time
from pathlib import Path

from deepgram_dictate import inject


async def test_copy_does_not_wait_for_the_forked_clipboard_server(tmp_path: Path) -> None:
    """wl-copy forks a child that serves the clipboard and keeps inherited pipes open."""
    fake = tmp_path / "wl-copy"
    fake.write_text(f"#!/bin/sh\ncat > '{tmp_path}/copied'\n(sleep 5) &\nexit 0\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    started = time.monotonic()
    await asyncio.wait_for(inject._copy("粵語 text".encode(), env), 2)
    assert time.monotonic() - started < 1
    assert (tmp_path / "copied").read_text() == "粵語 text"
