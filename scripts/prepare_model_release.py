"""Export and verify an immutable MLflow model release candidate.

The exported directory is intentionally written below ``artifacts/``, which is
gitignored. Its ``release-manifest.json`` is the hand-off record consumed by the
next deployment phase; the large/binary model itself must not be committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import mlflow
from mlflow import MlflowClient


MODEL_URI_PATTERN = re.compile(r"^models:/([^/@]+)/([0-9]+)$")

# A small, fixed contract test taken from the registered run's July holdout.
# It proves that the exported object reproduces the model selected for release;
# it is not a model-quality or release-gate test.
SMOKE_REQUEST = {
    "date": [
        "2020-07-01",
        "2020-07-02",
        "2020-07-03",
        "2020-07-04",
        "2020-07-05",
    ],
    "nuclear_avail": [29049.0, 29466.0, 30605.0, 29438.0, 26110.0],
}
EXPECTED_PRICES = [
    34.68523989365982,
    35.20491425207375,
    34.94565669127315,
    32.37078821136852,
    31.499444759126654,
]

VERIFY_CODE = r"""
import json
import sys

import mlflow.pyfunc
import pandas as pd

model_path = sys.argv[1]
request = json.loads(sys.argv[2])
expected = json.loads(sys.argv[3])

model = mlflow.pyfunc.load_model(model_path)
prediction = model.predict(pd.DataFrame(request))
actual = prediction["predicted_price"].tolist()
max_abs_error = max(abs(a - b) for a, b in zip(actual, expected))
if max_abs_error > 1e-10:
    raise SystemExit(
        f"release smoke test failed: max_abs_error={max_abs_error}, actual={actual}"
    )

print(json.dumps({
    "actual_prices": actual,
    "expected_prices": expected,
    "max_abs_error": max_abs_error,
    "model_info": model.unwrap_python_model().model_info(),
}))
"""


def _tree_digest(root: Path) -> tuple[str, int, int]:
    """Return a stable SHA-256 over relative names and file contents."""
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                byte_count += len(chunk)
                digest.update(chunk)
        file_count += 1
    return digest.hexdigest(), file_count, byte_count


def _json_safe(value):
    """Convert non-finite floats so the manifest remains strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _git_value(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _verify_in_fresh_process(model_path: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in (
        "PYTHONPATH",
        "MLFLOW_TRACKING_URI",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        env.pop(name, None)

    with tempfile.TemporaryDirectory(prefix="epex-release-verify-") as cwd:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                VERIFY_CODE,
                str(model_path),
                json.dumps(SMOKE_REQUEST),
                json.dumps(EXPECTED_PRICES),
            ],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    return json.loads(completed.stdout)


def _remove_transient_bytecode(root: Path) -> None:
    """Remove interpreter caches that are not part of the model release."""
    for cache_dir in sorted(root.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache_dir)
    for bytecode in root.rglob("*.py[co]"):
        bytecode.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-uri",
        default="models:/french_spot_price_forecaster/1",
        help="Immutable numeric MLflow model URI; aliases are deliberately rejected.",
    )
    parser.add_argument("--tracking-uri", default="sqlite:///mlflow.db")
    parser.add_argument(
        "--output-dir",
        default="artifacts/releases/french_spot_price_forecaster-v1",
    )
    args = parser.parse_args()

    match = MODEL_URI_PATTERN.fullmatch(args.model_uri)
    if not match:
        raise SystemExit("--model-uri must be immutable: models:/<name>/<numeric-version>")
    model_name, version = match.groups()

    repo = Path(__file__).resolve().parents[1]
    output_dir = (repo / args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty release directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    mlflow.set_tracking_uri(args.tracking_uri)
    client = MlflowClient()
    model_version = client.get_model_version(model_name, version)
    if model_version.status != "READY":
        raise SystemExit(f"model version is not READY: {model_version.status}")
    run = client.get_run(model_version.run_id)

    downloaded = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=args.model_uri,
            dst_path=str(output_dir),
        )
    ).resolve()
    if not (downloaded / "MLmodel").is_file():
        raise SystemExit(f"download did not produce an MLflow model at {downloaded}")

    _remove_transient_bytecode(downloaded)
    verification = _verify_in_fresh_process(downloaded)
    tree_sha256, file_count, size_bytes = _tree_digest(downloaded)

    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "release_status": "candidate",
        "promotion_note": (
            "Technically verified, but not assigned the champion alias; "
            "the project has no automated release gate yet."
        ),
        "model": {
            "uri": args.model_uri,
            "registered_name": model_name,
            "version": version,
            "model_id": model_version.source.removeprefix("models:/"),
            "run_id": model_version.run_id,
            "status": model_version.status,
            "source": model_version.source,
        },
        "training_run": {
            "artifact_uri": run.info.artifact_uri,
            "git_commit": run.data.tags.get("mlflow.source.git.commit"),
            "git_branch": run.data.tags.get("mlflow.source.git.branch"),
            "git_repository": run.data.tags.get("mlflow.source.git.repoURL"),
            "parameters": dict(sorted(run.data.params.items())),
            "metrics": dict(sorted(run.data.metrics.items())),
        },
        "release_preparation": {
            "git_commit": _git_value(repo, "rev-parse", "HEAD"),
            "git_worktree_clean": not bool(_git_value(repo, "status", "--porcelain")),
            "python": sys.version.split()[0],
            "mlflow": mlflow.__version__,
        },
        "artifact": {
            "relative_path": str(downloaded.relative_to(repo)),
            "tree_sha256": tree_sha256,
            "file_count": file_count,
            "size_bytes": size_bytes,
        },
        "verification": verification,
    }

    manifest_path = downloaded / "release-manifest.json"
    manifest_path.write_text(json.dumps(_json_safe(manifest), indent=2, allow_nan=False) + "\n")
    print(json.dumps({"artifact": str(downloaded), "manifest": str(manifest_path)}, indent=2))


if __name__ == "__main__":
    main()
