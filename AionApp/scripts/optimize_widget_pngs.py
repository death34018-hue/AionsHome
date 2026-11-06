"""Resize oversized widget PNGs and recompress them without changing small images."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from PIL import Image


def optimize_png(path: Path, max_edge: int) -> dict:
    path = Path(path)
    before_bytes = path.stat().st_size
    with Image.open(path) as source:
        source.load()
        original_size = source.size
        image = source.convert("RGBA")
        longest = max(image.size)
        if longest > max_edge:
            scale = max_edge / float(longest)
            target = (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            )
            image = image.resize(target, Image.Resampling.LANCZOS)

        handle, temporary_name = tempfile.mkstemp(
            prefix=path.stem + "-", suffix=".png", dir=path.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            image.save(temporary, format="PNG", optimize=True, compress_level=9)
            with Image.open(temporary) as check:
                check.load()
                if check.size != image.size:
                    raise RuntimeError(f"dimension verification failed: {path.name}")
                if check.convert("RGBA").tobytes() != image.tobytes():
                    raise RuntimeError(f"pixel verification failed: {path.name}")
            resized = image.size != original_size
            if resized or temporary.stat().st_size < before_bytes:
                os.replace(temporary, path)
            return {
                "name": path.name,
                "before_size": original_size,
                "after_size": image.size,
                "before_bytes": before_bytes,
                "after_bytes": path.stat().st_size,
            }
        finally:
            temporary.unlink(missing_ok=True)


def optimize_directory(directory: Path, character_max: int = 1000,
                       banner_max: int = 1400) -> list[dict]:
    results = []
    for path in sorted(Path(directory).glob("*.png")):
        limit = banner_max if path.name == "横幅.png" else character_max
        results.append(optimize_png(path, limit))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--character-max", type=int, default=1000)
    parser.add_argument("--banner-max", type=int, default=1400)
    args = parser.parse_args()
    results = optimize_directory(args.directory, args.character_max, args.banner_max)
    before = sum(item["before_bytes"] for item in results)
    after = sum(item["after_bytes"] for item in results)
    for item in results:
        print(
            f"{item['name']}: {item['before_size']} -> {item['after_size']}, "
            f"{item['before_bytes']} -> {item['after_bytes']} bytes"
        )
    print(f"total: {before} -> {after} bytes")


if __name__ == "__main__":
    main()
