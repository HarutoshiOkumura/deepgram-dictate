import tomllib
from pathlib import Path

import pytest

from deepgram_dictate.config import Config, ConfigError, Profile, load, parse
from deepgram_dictate.deepgram import batch_url, stream_url


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    cfg = load(tmp_path / "nope.toml")
    assert cfg == Config()
    assert cfg.profile(None).language == "en"


def test_overrides_and_new_profiles() -> None:
    cfg = parse(
        tomllib.loads(
            """
            hold_threshold_ms = 300
            inject = "paste"
            [profiles.en]
            keyterms = ["Deepgram", "Hyprland"]
            [profiles.ja]
            language = "ja"
            trailing_space = false
            """
        )
    )
    assert cfg.hold_threshold_ms == 300
    assert cfg.inject == "paste"
    assert cfg.profile("en").keyterms == ("Deepgram", "Hyprland")
    assert cfg.profile("en").model == "nova-3"
    assert cfg.profile("ja") == Profile(name="ja", language="ja", trailing_space=False)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("hold_treshold_ms = 3", "unknown setting"),
        ("inject = 'xdotool'", "inject must be one of"),
        ("sounds = 'yes'", "true or false"),
        ("hold_threshold_ms = -1", "non-negative"),
        ("default_profile = 'fr'", "unknown profile"),
        ("[profiles.en]\nlanguag = 'en'", "profiles.en: unknown setting"),
        ("[profiles.en]\nlanguage_hints = ['ja']", "only works with language"),
        ("api_base = 'api.deepgram.com'", "must start with https://"),
        ("audio_source = 'my_sink.monitor'", "cannot be a .monitor"),
    ],
)
def test_rejects_bad_config(toml: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(tomllib.loads(toml))


def test_urls() -> None:
    cfg = parse(
        tomllib.loads(
            """
            mip_opt_out = true
            [profiles.cjk]
            language = "multi"
            language_hints = ["zh-HK", "ja"]
            keyterms = ["Deepgram"]
            """
        )
    )
    url = stream_url(cfg.profile("cjk"), cfg)
    assert url.startswith("wss://api.deepgram.com/v1/listen?model=nova-3&language=multi")
    for part in (
        "language_hint=zh-HK",
        "language_hint=ja",
        "keyterm=Deepgram",
        "mip_opt_out=true",
        "encoding=linear16",
        "sample_rate=16000",
        "interim_results=false",
        "endpointing=false",
    ):
        assert part in url
    assert batch_url(cfg.profile("cjk"), cfg).startswith("https://api.deepgram.com/v1/listen?")
    assert "encoding" not in batch_url(cfg.profile("cjk"), cfg)

    local = parse({"api_base": "http://127.0.0.1:9999/"})
    assert stream_url(local.profile(None), local).startswith("ws://127.0.0.1:9999/v1/listen?")
