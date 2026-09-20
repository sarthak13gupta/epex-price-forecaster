"""Create and verify deterministic model-release archives.

The model binary stays out of Git. A small, versioned JSON descriptor records
where its GitHub Release asset lives and the two identities that matter:

* archive SHA-256: proves the downloaded transport object is unchanged;
* model-tree SHA-256: proves the extracted model is the Phase-1 release.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.artifact_digest import tree_digest


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalised_info(path: Path, archive_name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(archive_name)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    if path.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    elif path.is_file():
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        info.size = path.stat().st_size
    else:
        raise ValueError(f"release archive does not permit special files: {path}")
    return info


def create_archive(source: Path, output: Path, archive_root: PurePosixPath) -> dict:
    if not source.is_dir():
        raise ValueError(f"model release directory does not exist: {source}")
    if archive_root.is_absolute() or ".." in archive_root.parts:
        raise ValueError(f"archive root must be a safe relative path: {archive_root}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                entries = [source, *sorted(source.rglob("*"))]
                for path in entries:
                    relative = path.relative_to(source)
                    name = archive_root if not relative.parts else archive_root / relative.as_posix()
                    info = _normalised_info(path, name.as_posix())
                    if path.is_file():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)

    tree_sha, file_count, size_bytes = tree_digest(
        source, excluded_names={"release-manifest.json"}
    )
    return {
        "archive": str(output),
        "archive_sha256": file_sha256(output),
        "archive_root": archive_root.as_posix(),
        "model_tree_sha256": tree_sha,
        "model_file_count": file_count,
        "model_size_bytes": size_bytes,
    }


def _validate_members(archive: tarfile.TarFile, expected_root: PurePosixPath) -> None:
    members = archive.getmembers()
    if not members:
        raise ValueError("model release archive is empty")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive path: {member.name}")
        if path != expected_root and expected_root not in path.parents:
            raise ValueError(f"archive member is outside {expected_root}: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise ValueError(f"links and special files are forbidden: {member.name}")


def extract_archive(
    archive_path: Path,
    destination: Path,
    expected_root: PurePosixPath,
    expected_archive_sha256: str,
    expected_tree_sha256: str,
) -> dict:
    actual_archive_sha = file_sha256(archive_path)
    if actual_archive_sha != expected_archive_sha256:
        raise ValueError(
            "release archive checksum mismatch: "
            f"expected {expected_archive_sha256}, got {actual_archive_sha}"
        )

    target = destination.joinpath(*expected_root.parts)
    if target.exists():
        raise ValueError(f"refusing to replace existing release directory: {target}")

    with tarfile.open(archive_path, mode="r:gz") as archive:
        _validate_members(archive, expected_root)
        archive.extractall(destination, filter="data")

    actual_tree_sha, file_count, size_bytes = tree_digest(
        target, excluded_names={"release-manifest.json"}
    )
    if actual_tree_sha != expected_tree_sha256:
        shutil.rmtree(target)
        raise ValueError(
            "extracted model checksum mismatch: "
            f"expected {expected_tree_sha256}, got {actual_tree_sha}"
        )

    return {
        "archive": str(archive_path),
        "archive_sha256": actual_archive_sha,
        "extracted_to": str(target),
        "model_tree_sha256": actual_tree_sha,
        "model_file_count": file_count,
        "model_size_bytes": size_bytes,
    }


def _load_descriptor(path: Path) -> dict:
    descriptor = json.loads(path.read_text())
    required = {
        "model_name",
        "model_version",
        "artifact_path",
        "model_tree_sha256",
        "github_release_repository",
        "github_release_tag",
        "github_release_asset",
        "archive_sha256",
    }
    missing = sorted(required - descriptor.keys())
    if missing:
        raise ValueError(f"release descriptor is missing: {', '.join(missing)}")
    if not str(descriptor["model_version"]).isdigit():
        raise ValueError("model_version must be numeric")
    for key in ("model_tree_sha256", "archive_sha256"):
        value = descriptor[key]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"{key} must be a lowercase SHA-256 digest")
    return descriptor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--archive-root", type=PurePosixPath, required=True)

    extract = subparsers.add_parser("extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, default=Path("."))
    extract.add_argument("--expected-root", type=PurePosixPath, required=True)
    extract.add_argument("--expected-archive-sha256", required=True)
    extract.add_argument("--expected-tree-sha256", required=True)

    inspect = subparsers.add_parser("inspect-descriptor")
    inspect.add_argument("--descriptor", type=Path, required=True)
    inspect.add_argument("--github-output", type=Path)

    args = parser.parse_args()
    if args.command == "create":
        result = create_archive(args.source, args.output, args.archive_root)
    elif args.command == "extract":
        result = extract_archive(
            args.archive,
            args.destination,
            args.expected_root,
            args.expected_archive_sha256,
            args.expected_tree_sha256,
        )
    else:
        result = _load_descriptor(args.descriptor)
        if args.github_output:
            with args.github_output.open("a") as output:
                for key, value in result.items():
                    print(f"{key}={value}", file=output)

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
