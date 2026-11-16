"""Deepgram speech-to-text: a live WebSocket stream, plus a one-shot upload used as a fallback."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import urllib.error
import urllib.request
import wave
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .audio import SAMPLE_RATE
from .config import Config, Profile

log = logging.getLogger(__name__)


class DeepgramError(Exception):
    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def _feature_params(profile: Profile, cfg: Config) -> list[tuple[str, str]]:
    params = [
        ("model", profile.model),
        ("language", profile.language),
        ("smart_format", "true" if profile.smart_format else "false"),
    ]
    params += [("language_hint", h) for h in profile.language_hints]
    params += [("keyterm", t) for t in profile.keyterms]
    if cfg.mip_opt_out:
        params.append(("mip_opt_out", "true"))
    return params


def stream_url(profile: Profile, cfg: Config) -> str:
    params = [
        *_feature_params(profile, cfg),
        ("encoding", "linear16"),
        ("sample_rate", str(SAMPLE_RATE)),
        ("channels", "1"),
        ("interim_results", "false"),
        # Dictation only needs text at the end. Deepgram's default 10 ms endpointing cuts a
        # segment at every brief pause, and each segment loses the context of its
        # neighbours: on reference speech, turning it off cut errors by 8-27% in English,
        # Japanese, Cantonese and Mandarin alike.
        ("endpointing", "false"),
    ]
    base = "ws" + cfg.api_base.rstrip("/").removeprefix("http")  # https -> wss, http -> ws
    return f"{base}/v1/listen?{urlencode(params)}"


def batch_url(profile: Profile, cfg: Config) -> str:
    base = cfg.api_base.rstrip("/")
    return f"{base}/v1/listen?{urlencode(_feature_params(profile, cfg))}"


def _describe_http_error(status: int, body: bytes) -> DeepgramError:
    detail = ""
    try:
        payload = json.loads(body)
        detail = payload.get("err_msg") or payload.get("reason") or payload.get("message") or ""
    except (ValueError, AttributeError):
        pass
    if status == 401:
        return DeepgramError(
            "Deepgram rejected the API key (401). Run `dictate set-key`.", auth=True
        )
    if status == 403:
        return DeepgramError(f"Deepgram refused the request (403): {detail or 'no access'}")
    if status == 402:
        return DeepgramError("Deepgram account is out of credit (402).")
    return DeepgramError(f"Deepgram returned HTTP {status}: {detail or 'no details'}")


class LiveTranscriber:
    """Streams PCM to /v1/listen and collects finalized transcript segments."""

    def __init__(self, url: str, api_key: str) -> None:
        self._url = url
        self._api_key = api_key
        self._ws: ClientConnection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self.segments: list[str] = []
        self.languages: set[str] = set()

    async def connect(self, open_timeout_s: float = 5.0) -> None:
        try:
            self._ws = await connect(
                self._url,
                additional_headers={"Authorization": f"Token {self._api_key}"},
                open_timeout=open_timeout_s,
                max_size=None,
            )
        except InvalidStatus as e:
            raise _describe_http_error(e.response.status_code, e.response.body or b"") from e
        except (OSError, TimeoutError) as e:
            raise DeepgramError(f"Could not reach Deepgram: {e}") from e
        self._receiver = asyncio.create_task(self._receive(self._ws))

    async def _receive(self, ws: ClientConnection) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "Results" and msg.get("is_final"):
                alt = msg["channel"]["alternatives"][0]
                if text := alt.get("transcript", "").strip():
                    self.segments.append(text)
                self.languages.update(alt.get("languages") or ())
            elif kind == "Error":
                raise DeepgramError(f"Deepgram stream error: {msg.get('description') or msg}")

    async def send(self, pcm: bytes) -> None:
        assert self._ws
        if self._receiver and self._receiver.done():
            await self._receiver  # surfaces the server's error instead of a bare "closed"
        try:
            await self._ws.send(pcm)
        except ConnectionClosed as e:
            raise DeepgramError(f"Deepgram closed the stream: {e}") from e

    async def finish(self, wait_s: float) -> list[str]:
        """Signal end of audio and wait for the last transcripts."""
        assert self._ws and self._receiver
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
            await asyncio.wait_for(self._receiver, wait_s)
        except ConnectionClosed as e:
            raise DeepgramError(f"Deepgram closed the stream: {e}") from e
        except TimeoutError as e:
            raise DeepgramError("Timed out waiting for Deepgram's final transcript") from e
        return self.segments

    async def abort(self) -> None:
        if self._receiver:
            self._receiver.cancel()
        if self._ws:
            await self._ws.close()


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _post(url: str, api_key: str, body: bytes, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise _describe_http_error(e.code, e.read()) from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise DeepgramError(f"Could not reach Deepgram: {e}") from e


async def transcribe_recording(
    url: str, api_key: str, pcm: bytes, http_timeout_s: float
) -> list[str]:
    """Upload a whole recording in one request. Used when the live stream failed."""
    body = await asyncio.to_thread(_post, url, api_key, _wav(pcm), http_timeout_s)
    alt = body["results"]["channels"][0]["alternatives"][0]
    text = alt.get("transcript", "").strip()
    return [text] if text else []
