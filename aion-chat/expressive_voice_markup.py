"""The same voice spans are used for attachments, streaming and ordinary reading."""
import re


BRACKETS = str.maketrans('【】［］', '[][]')
OPEN = r'[\[【［]语音[\]】］]'
CLOSE = r'[\[【［]/语音[\]】］]'
LEGACY_OPEN = r'[\[【［]语音\s*[|｜:：]'
# Unfinished blocks are hidden too, but only a closed block can be synthesized.
VOICE_SPAN = re.compile(
    OPEN + r'(?P<script>.*?)(?:(?P<close>' + CLOSE + r')|\Z)'
    + '|' + LEGACY_OPEN + r'(?P<legacy>[^\]】］]*)(?:(?P<legacy_close>[\]】］])|\Z)',
    re.S,
)


def strip_voice_blocks(text):
    return VOICE_SPAN.sub('', text)
