"""Session against a local fake Deepgram (WebSocket + HTTP), with a fake microphone."""

import asyncio
import json
import math
import struct
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from deepgram_dictate import credentials, session
from deepgram_dictate.config import Config, Profile
from deepgram_dictate.credentials import KeyCache
from deepgram_dictate.deepgram import DeepgramError
from deepgram_dictate.session import (
    MicMissingError,
    MicMutedError,
    NoAudioError,
    Session,
    TranscriptionFailedError,
)

TONE = b"".join(
    struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)
)
SILENCE = bytes(len(TONE))


class FakeRecorder:
    pcm = TONE

    def __init__(self, source: str = "") -> None:
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        pass

    async def chunks(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self.pcm), 2561):  # odd size: exercises sample alignment
            yield self.pcm[i : i + 2561]
            await asyncio.sleep(0)
        await self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()

    def kill(self) -> None:
        self._stopped.set()


class FakeDeepgram:
    def __init__(self) -> None:
        self.mode = "ok"
        self.received = 0
        self.query = ""

    async def handler(self, ws) -> None:
        self.query = ws.request.path
        async for msg in ws:
            if isinstance(msg, bytes):
                self.received += len(msg)
                if self.mode == "drop":
                    await ws.close(1011, "NET-0001")
                    return
                continue
            if json.loads(msg)["type"] == "CloseStream":
                for text in ("Hello world.", "", "Second part"):
                    result = {
                        "type": "Results",
                        "is_final": True,
                        "channel": {"alternatives": [{"transcript": text}]},
                    }
                    await ws.send(json.dumps(result))
                await ws.send(json.dumps({"type": "Metadata"}))
                await ws.close()

    def process_request(self, connection, request):
        if request.headers.get("Authorization") != "Token test-key" or self.mode == "401":
            body = b'{"err_msg":"Invalid credentials."}'
            return Response(401, "Unauthorized", Headers({"Content-Length": str(len(body))}), body)
        return None


@pytest.fixture
async def deepgram() -> AsyncIterator[tuple[FakeDeepgram, Config]]:
    fake = FakeDeepgram()
    async with serve(fake.handler, "127.0.0.1", 0, process_request=fake.process_request) as server:
        port = server.sockets[0].getsockname()[1]
        yield fake, Config(api_base=f"http://127.0.0.1:{port}", tail_ms=0)


@pytest.fixture
def upload_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    state = {"status": 200, "bodies": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            state["bodies"].append(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps(
                {"results": {"channels": [{"alternatives": [{"transcript": "Uploaded text."}]}]}}
            ).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/listen"
    monkeypatch.setattr(session, "batch_url", lambda profile, cfg: url)
    yield state
    httpd.shutdown()


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def lookup() -> str:
        return "test-key"

    async def unmuted(source: str = "") -> str:
        return "unmuted"

    monkeypatch.setattr(credentials, "lookup", lookup)
    monkeypatch.setattr(session, "source_status", unmuted)
    monkeypatch.setattr(session, "Recorder", FakeRecorder)
    monkeypatch.setattr(FakeRecorder, "pcm", TONE)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


async def _dictate(cfg: Config, on_fatal=lambda e: None, profile: Profile | None = None) -> str:
    s = Session(profile or Profile(name="en"), cfg, KeyCache(), on_fatal)
    await s.start()
    await asyncio.sleep(0.2)
    return await s.finish()


async def test_streams_audio_and_assembles_text(deepgram) -> None:
    fake, cfg = deepgram
    assert await _dictate(cfg) == "Hello world. Second part "
    assert fake.received == len(TONE)  # every sample, aligned, nothing lost
    assert "model=nova-3" in fake.query and "language=en" in fake.query


async def test_falls_back_to_upload_when_stream_drops(deepgram, upload_server) -> None:
    fake, cfg = deepgram
    fake.mode = "drop"
    assert await _dictate(cfg) == "Uploaded text. "
    wav = upload_server["bodies"][0]
    assert wav.startswith(b"RIFF") and len(wav) == 44 + len(TONE)


async def test_failed_upload_keeps_the_recording(deepgram, upload_server, tmp_path) -> None:
    fake, cfg = deepgram
    fake.mode = "drop"
    upload_server["status"] = 500
    with pytest.raises(TranscriptionFailedError) as info:
        await _dictate(cfg)
    assert info.value.saved and info.value.saved.is_relative_to(tmp_path)
    assert info.value.saved.stat().st_size == 44 + len(TONE)


async def test_rejected_key_is_fatal_immediately(deepgram) -> None:
    fake, cfg = deepgram
    fake.mode = "401"
    fatal: list[BaseException] = []
    s = Session(Profile(name="en"), cfg, KeyCache(), fatal.append)
    await s.start()
    await asyncio.sleep(0.2)
    assert len(fatal) == 1 and isinstance(fatal[0], DeepgramError) and fatal[0].auth
    await s.cancel()


async def test_silence_is_reported_instead_of_typed(deepgram, monkeypatch) -> None:
    _, cfg = deepgram
    monkeypatch.setattr(FakeRecorder, "pcm", SILENCE)
    with pytest.raises(NoAudioError):
        await _dictate(cfg)


@pytest.mark.parametrize(
    ("status", "error"), [("muted", MicMutedError), ("missing", MicMissingError)]
)
async def test_unusable_microphone_refuses_to_start(deepgram, monkeypatch, status, error) -> None:
    """A missing source must fail loudly: pw-record would silently record the default mic."""
    _, cfg = deepgram

    async def fake_status(source: str = "") -> str:
        return status

    monkeypatch.setattr(session, "source_status", fake_status)
    s = Session(Profile(name="en"), cfg, KeyCache())
    with pytest.raises(error):
        await s.start()
    await s.cancel()
