import json

import pytest

from deepgram_dictate.hotkey import encode


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"WAYLAND_DISPLAY": "wayland-1", "HYPRLAND_INSTANCE_SIGNATURE": "abc_123_456"},
        {"DISPLAY": 'odd "quoted" \\ value\twith\ncontrols\x01 and ünïcødé 粵語'},
    ],
)
@pytest.mark.parametrize("profile", [None, "en", "yue"])
def test_encode_is_valid_json(env: dict[str, str], profile: str | None) -> None:
    raw = encode("press", profile, env)
    assert raw.endswith(b"\n")
    expected = {"cmd": "press", "env": env} | ({"profile": profile} if profile else {})
    assert json.loads(raw) == expected
