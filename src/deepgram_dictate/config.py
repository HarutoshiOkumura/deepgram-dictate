"""User configuration: ~/.config/deepgram-dictate/config.toml (optional).

Every setting has a default, so the file only needs the values you want to change.
Unknown keys are rejected so a typo never silently does nothing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from . import paths


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    """One way of transcribing: a model plus language settings, selected per hotkey."""

    name: str
    # Short tag shown in the status bar while dictating (default: the name in capitals).
    label: str = ""
    model: str = "nova-3"
    language: str = "en"
    # Only used with language = "multi": biases detection toward these languages.
    language_hints: tuple[str, ...] = ()
    # Names and jargon Deepgram should favour (Nova-3 / Flux only).
    keyterms: tuple[str, ...] = ()
    smart_format: bool = True
    # Type a space after the text when it ends in a Latin-script character,
    # so consecutive dictations do not run together.
    trailing_space: bool = True

    @property
    def display_label(self) -> str:
        return self.label or self.name.upper()


@dataclass(frozen=True)
class Config:
    # A press shorter than this is a "tap" (locks hands-free mode); longer is push-to-talk.
    hold_threshold_ms: int = 400
    # Keep recording this long after you stop, so the last syllable is not clipped.
    tail_ms: int = 150
    # Safety net for a forgotten hands-free session.
    max_session_s: int = 900
    # How to deliver text: "type" (virtual keyboard via wtype) or "paste" (clipboard + Ctrl+V).
    inject: str = "type"
    sounds: bool = True
    sound_volume: float = 0.4
    # Opt out of Deepgram's Model Improvement Program for every request.
    mip_opt_out: bool = False
    # PipeWire/PulseAudio source to record from; empty means the system default microphone.
    audio_source: str = ""
    # e.g. https://api.eu.deepgram.com for in-region processing.
    api_base: str = "https://api.deepgram.com"
    default_profile: str = "en"
    profiles: dict[str, Profile] = field(default_factory=lambda: {"en": Profile(name="en")})

    def profile(self, name: str | None) -> Profile:
        key = name or self.default_profile
        try:
            return self.profiles[key]
        except KeyError:
            known = ", ".join(sorted(self.profiles))
            raise ConfigError(f"unknown profile {key!r} (configured: {known})") from None


_INJECT_METHODS = ("type", "paste")


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")


def _coerce(name: str, value: Any, expected: Any, where: str) -> Any:
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.{name} must be true or false")
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"{where}.{name} must be a non-negative integer")
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}.{name} must be a number")
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{name} must be a string")
        return value
    if expected == tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}.{name} must be a list of strings")
        return tuple(value)
    raise AssertionError(f"unhandled type for {name}")


def _field_types(cls: type) -> dict[str, Any]:
    hints = {
        "str": str,
        "int": int,
        "bool": bool,
        "float": float,
        "tuple[str, ...]": tuple[str, ...],
    }
    return {f.name: hints[f.type] for f in fields(cls) if f.type in hints}


def parse(data: dict[str, Any]) -> Config:
    top_types = _field_types(Config)
    _check_keys(data, set(top_types) | {"profiles"}, "config")
    values = {k: _coerce(k, v, top_types[k], "config") for k, v in data.items() if k != "profiles"}

    profiles = dict(Config().profiles)
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ConfigError("[profiles] must be a table")
    profile_types = _field_types(Profile)
    profile_types.pop("name")
    for name, table in raw_profiles.items():
        where = f"profiles.{name}"
        if not isinstance(table, dict):
            raise ConfigError(f"[{where}] must be a table")
        _check_keys(table, set(profile_types), where)
        base = profiles.get(name, Profile(name=name))
        profiles[name] = replace(
            base, **{k: _coerce(k, v, profile_types[k], where) for k, v in table.items()}
        )

    cfg = Config(**values, profiles=profiles)
    if cfg.inject not in _INJECT_METHODS:
        raise ConfigError(f"config.inject must be one of {', '.join(_INJECT_METHODS)}")
    if not 0.0 <= cfg.sound_volume <= 1.0:
        raise ConfigError("config.sound_volume must be between 0 and 1")
    if cfg.audio_source.endswith(".monitor"):
        raise ConfigError(
            "config.audio_source cannot be a .monitor: PipeWire cannot record it by that name "
            "and would silently use the default microphone instead. Create a source from it "
            "with `pactl load-module module-remap-source master=<name>.monitor "
            "source_name=<new>` and use <new>."
        )
    if not cfg.api_base.startswith(("https://", "http://")):
        raise ConfigError("config.api_base must start with https:// (or http:// for local servers)")
    cfg.profile(cfg.default_profile)  # validates default_profile
    for p in cfg.profiles.values():
        if p.language_hints and p.language != "multi":
            raise ConfigError(
                f'profiles.{p.name}.language_hints only works with language = "multi"'
            )
    return cfg


def load(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        return parse(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
