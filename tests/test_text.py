import pytest

from deepgram_dictate.text import assemble


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        ([], ""),
        (["Hello there."], "Hello there. "),
        (["Hello there.", "How are you?"], "Hello there. How are you? "),
        (["  spaced   out  ", ""], "spaced out "),
        # Chinese and Japanese never put spaces between characters, and get no trailing space.
        (["今天 天氣 很 好", "我 想 出去"], "今天天氣很好我想出去"),
        (["今日は いい 天気 ですね。"], "今日はいい天気ですね。"),
        # Spaces between a Latin word and CJK text are kept.
        (["我 用 Deepgram 做 听写"], "我用 Deepgram 做听写"),
        (["Meeting at 3 PM 好嗎"], "Meeting at 3 PM 好嗎"),
    ],
)
def test_assemble(segments: list[str], expected: str) -> None:
    assert assemble(segments, trailing_space=True) == expected


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        # Deepgram ended a segment with a half-width comma (seen with zh-HK).
        (["自由法國軍就解放了男,", "法並轉向德國"], "自由法國軍就解放了男，法並轉向德國"),
        (["今天很好. 我想出去!"], "今天很好。我想出去！"),
        (["你好? 是的: 對"], "你好？是的：對"),
        # Japanese uses 、 for the comma.
        (
            ["群島や湖では, 必ずしもヨットは必要ありません."],
            "群島や湖では、必ずしもヨットは必要ありません。",
        ),
        # Punctuation next to Latin text is left alone.
        (["我用 Deepgram, 很好"], "我用 Deepgram, 很好"),
        (["Version 2.5 版本"], "Version 2.5 版本"),
    ],
)
def test_cjk_punctuation_is_full_width(segments: list[str], expected: str) -> None:
    assert assemble(segments, trailing_space=True) == expected


def test_no_trailing_space_when_disabled() -> None:
    assert assemble(["Done."], trailing_space=False) == "Done."
