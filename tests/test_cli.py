import json

from deepgram_dictate.cli import waybar


def test_waybar_hidden_when_idle() -> None:
    out = json.loads(waybar({"state": "idle"}))
    assert out["text"] == ""  # Waybar hides a custom module with empty text
    assert out["class"] == ["idle"]


def test_waybar_recording_shows_language() -> None:
    out = json.loads(waybar({"state": "recording", "mode": "locked", "label": "粵"}))
    assert out["text"].endswith(" 粵")
    assert "#f38ba8" in out["text"]
    assert out["class"] == ["recording", "locked"]
    assert "hands-free" in out["tooltip"]


def test_waybar_escapes_markup() -> None:
    out = json.loads(waybar({"state": "recording", "mode": "held", "label": "A&B<"}))
    assert "A&amp;B&lt;" in out["text"]


def test_waybar_transcribing() -> None:
    out = json.loads(waybar({"state": "transcribing"}))
    assert "#fab387" in out["text"]
    assert out["class"] == ["transcribing"]
