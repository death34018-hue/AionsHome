"""Fetch pinned official runtime/model, or reuse the verified phone trial files.

Run before building: python scripts/prepare-local-asr.py
Optional: --trial-model ../../.tmp/offline-asr/bilingual/assets/model
"""
import argparse
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'app'
MODEL = 'sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16'
RELEASE = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/'
FILES = {
    'encoder-epoch-99-avg-1.int8.onnx': 'db6f51551762e40e549166fe041ea3e45464370b595e9ad23f06478ec3794fbb',
    'decoder-epoch-99-avg-1.onnx': '89be509a83175261695bdef5fd1c7b9ab1129a663d1284e7ba9f8507b21e0906',
    'joiner-epoch-99-avg-1.int8.onnx': 'bdda356d6f9b8c2d7cee9ee0e26075fa537490f7fd06520be408d287073667b9',
    'tokens.txt': 'a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3',
}
AAR_HASH = '633c24321e06b1fe79feafa03ea16cbc0f8a286641e2da3559bac91bdb13bd96'


def valid(path, expected):
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected


def download(url, path):
    subprocess.run(['curl', '-L', '--fail', '--retry', '2', '--connect-timeout', '20',
                    '--max-time', '900', '--output', str(path), url], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trial-model', type=Path)
    args = parser.parse_args()
    aar = APP / 'libs/sherpa-onnx-1.13.8.aar'
    dest = APP / 'src/main/assets/asr/zipformer-small-bilingual'
    dest.mkdir(parents=True, exist_ok=True)
    aar.parent.mkdir(parents=True, exist_ok=True)
    cache = APP.parent / 'build' / 'asr-downloads'
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aion-asr-', dir=cache) as temp:
        temp = Path(temp)
        if not valid(aar, AAR_HASH):
            archive = temp / 'runtime.aar'
            download(RELEASE + 'v1.13.8/sherpa-onnx-1.13.8.aar', archive)
            if not valid(archive, AAR_HASH):
                raise RuntimeError('Runtime SHA256 mismatch')
            shutil.copyfile(archive, aar)
        missing = {n: h for n, h in FILES.items() if not valid(dest / n, h)}
        if missing:
            if args.trial_model:
                for name, digest in missing.items():
                    src = args.trial_model / name
                    if not valid(src, digest):
                        raise RuntimeError('Trial model SHA256 mismatch: ' + name)
                    shutil.copyfile(src, dest / name)
            else:
                archive = temp / 'model.tar.bz2'
                download(RELEASE + 'asr-models/' + MODEL + '.tar.bz2', archive)
                with tarfile.open(archive) as tar:
                    for name, digest in missing.items():
                        # Read only four known files; never extract archive paths.
                        with tar.extractfile(MODEL + '/' + name) as src:
                            data = src.read()
                        if hashlib.sha256(data).hexdigest() != digest:
                            raise RuntimeError('Model SHA256 mismatch: ' + name)
                        (dest / name).write_bytes(data)
    print('Verified sherpa-onnx 1.13.8 and bilingual model (60,142,871 bytes).')


if __name__ == '__main__':
    main()
