"""Deterministic content digest for a model-artifact directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def tree_digest(root: Path, excluded_names: set[str] | None = None) -> tuple[str, int, int]:
    """Hash sorted relative filenames and contents, ignoring file metadata."""
    excluded_names = excluded_names or set()
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0

    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if path.name in excluded_names:
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                byte_count += len(chunk)
                digest.update(chunk)
        file_count += 1

    return digest.hexdigest(), file_count, byte_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--expect")
    args = parser.parse_args()

    sha256, file_count, size_bytes = tree_digest(args.root, set(args.exclude))
    result = {
        "root": str(args.root),
        "tree_sha256": sha256,
        "file_count": file_count,
        "size_bytes": size_bytes,
    }
    print(json.dumps(result, sort_keys=True))

    if args.expect and sha256 != args.expect:
        raise SystemExit(
            f"model artifact checksum mismatch: expected {args.expect}, got {sha256}"
        )


if __name__ == "__main__":
    main()
