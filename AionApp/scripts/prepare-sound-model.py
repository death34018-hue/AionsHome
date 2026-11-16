"""Fetch the pinned official YAMNet mobile model before building the Android app.

Run: python scripts/prepare-sound-model.py
Source: https://developers.google.com/edge/mediapipe/solutions/audio/audio_classifier
"""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import zipfile

APP = Path(__file__).resolve().parents[1] / 'app'
URL = 'https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/latest/yamnet.tflite'
SHA256 = '4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf'


def main():
    dest = APP / 'src/main/assets/sound'
    dest.mkdir(parents=True, exist_ok=True)
    model = dest / 'yamnet.tflite'
    if not model.is_file() or hashlib.sha256(model.read_bytes()).hexdigest() != SHA256:
        with tempfile.TemporaryDirectory(prefix='yamnet-') as temp:
            downloaded = Path(temp) / 'yamnet.tflite'
            subprocess.run(['curl', '-L', '--fail', '--retry', '2', '--connect-timeout', '20',
                            '--max-time', '300', '--output', str(downloaded), URL], check=True)
            data = downloaded.read_bytes()
            if hashlib.sha256(data).hexdigest() != SHA256:
                raise RuntimeError('YAMNet SHA256 mismatch')
            model.write_bytes(data)
    with zipfile.ZipFile(model) as archive:
        labels = archive.read('yamnet_label_list.txt')
    if len(labels.decode('utf-8').splitlines()) != 521:
        raise RuntimeError('Expected 521 YAMNet labels')
    (dest / 'labels.txt').write_bytes(labels)
    print(f'Verified YAMNet: {model.stat().st_size:,} bytes, 521 labels.')


if __name__ == '__main__':
    main()
