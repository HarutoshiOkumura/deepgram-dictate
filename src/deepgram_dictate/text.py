"""Turn Deepgram's finalized transcript segments into the text that gets typed."""

from __future__ import annotations

import re

# Scripts written without spaces between words: Han (Chinese characters, Japanese kanji),
# kana, CJK punctuation, and full-width forms.
_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # Half-width and full-width forms
)
_CJK = "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES)
_KANA = re.compile("[぀-ヿ]")
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
# Half-width punctuation right after a CJK character, ending the text or followed by more
# CJK text (any spaces around it are dropped).
_HALF_WIDTH_AFTER_CJK = re.compile(rf"(?<=[{_CJK}])\s*([,.!?:;])(?:\s*(?=[{_CJK}])|\s*$)")
_WHITESPACE = re.compile(r"\s+")
_ENDS_CJK = re.compile(rf"[{_CJK}]$")

_FULL_WIDTH = {",": "，", ".": "。", "!": "！", "?": "？", ":": "：", ";": "；"}
_FULL_WIDTH_JA = _FULL_WIDTH | {",": "、"}


def assemble(segments: list[str], *, trailing_space: bool) -> str:
    """Join segments into the text to type.

    Segments are joined with single spaces; then, for Chinese and Japanese, spaces between
    characters are dropped and half-width punctuation between them becomes full-width.
    """
    text = _WHITESPACE.sub(" ", " ".join(s.strip() for s in segments)).strip()
    marks = _FULL_WIDTH_JA if _KANA.search(text) else _FULL_WIDTH
    text = _HALF_WIDTH_AFTER_CJK.sub(lambda m: marks[m.group(1)], text)
    text = _SPACE_BETWEEN_CJK.sub("", text)
    if text and trailing_space and not _ENDS_CJK.search(text):
        text += " "
    return text
