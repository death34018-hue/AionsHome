import re
import shutil
import tempfile
import unittest
from pathlib import Path, PureWindowsPath


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CLEANUP_SCRIPT = REPOSITORY_ROOT / "清理个人数据.bat"
ENGLISH_CORNER_AUDIO = PureWindowsPath(
    r"aion-chat\data\english_corner_audio"
)


def _delete_and_recreate_pairs(script_text):
    lines = [
        line.strip()
        for line in script_text.splitlines()
        if line.strip() and not line.lstrip().startswith("::")
    ]
    pairs = []
    for index, line in enumerate(lines[:-1]):
        match = re.fullmatch(
            r'if exist "([^"]+)" rmdir /s /q "\1"',
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        target = PureWindowsPath(match.group(1))
        if lines[index + 1].casefold() == f'mkdir "{target}"'.casefold():
            pairs.append(target)
    return pairs


class PersonalDataCleanupTests(unittest.TestCase):
    def test_english_corner_audio_is_deleted_and_recreated_safely(self):
        script_text = CLEANUP_SCRIPT.read_text(encoding="utf-8-sig")
        cleanup_pairs = _delete_and_recreate_pairs(script_text)
        self.assertIn(ENGLISH_CORNER_AUDIO, cleanup_pairs)

        with tempfile.TemporaryDirectory() as temp_directory:
            sandbox = Path(temp_directory).resolve()
            target = sandbox.joinpath(*ENGLISH_CORNER_AUDIO.parts).resolve()
            target.mkdir(parents=True)
            personal_audio = target / "personal-voice.mp3"
            personal_audio.write_bytes(b"private audio")

            self.assertTrue(target.is_relative_to(sandbox))
            shutil.rmtree(target)
            target.mkdir(parents=True)

            self.assertTrue(target.is_dir())
            self.assertFalse(personal_audio.exists())


if __name__ == "__main__":
    unittest.main()
