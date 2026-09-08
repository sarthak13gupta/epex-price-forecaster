import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

CONFIG_RELATIVE_PATH = Path("configs") / "config.yaml"


def _discover_project_root() -> Path:
    """
    Locates the project root, the directory containing configs/config.yaml.

    Resolution order matters. MLflow packages this module into a model artifact
    via `code_paths` and prepends that copy to sys.path, so a served process
    importing `src.utils.config_loader` gets the artifact's copy — whose
    __file__ sits inside the artifact, not the repo. Deriving the root from
    __file__ alone would therefore look for config inside the artifact and fail.
    An explicit PROJECT_ROOT wins, then an upward search from the working
    directory, and only then the module's own location.
    """
    env_root = os.getenv("PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()

    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / CONFIG_RELATIVE_PATH).exists():
            return candidate

    module_root = Path(__file__).resolve().parents[2]
    if (module_root / CONFIG_RELATIVE_PATH).exists():
        return module_root

    # Nothing found. Return the module-relative guess so the eventual
    # FileNotFoundError names a concrete path.
    return module_root


PROJECT_ROOT = _discover_project_root()

DEFAULT_CONFIG_PATH = PROJECT_ROOT / CONFIG_RELATIVE_PATH

# Config keys under `paths:` that hold filesystem locations and should be made
# absolute relative to the project root.
_PATH_KEYS = (
    "raw_train_csv",
    "raw_pred_csv",
    "processed_parquet",
    "output_dir",
)


def resolve_path(path: str | os.PathLike[str]) -> Path:
    """Resolves a possibly-relative path against the project root."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def load_config(config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """
    Parses the central YAML configuration file.

    Relative entries under `paths:` are resolved against the project root so the
    same config file works from any working directory and inside a container.
    The CONFIG_PATH environment variable overrides the default location.
    """
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", str(DEFAULT_CONFIG_PATH))

    resolved_config_path = resolve_path(config_path)

    if not resolved_config_path.exists():
        raise FileNotFoundError(
            f"[ERROR] Configuration file not found at: {resolved_config_path}"
        )

    with open(resolved_config_path, "r") as file:
        config = yaml.safe_load(file)

    for key in _PATH_KEYS:
        if key in config.get("paths", {}):
            config["paths"][key] = str(resolve_path(config["paths"][key]))

    # The tracking URI is environment-driven in deployment; the config value is
    # only a local default. Relative local URIs are anchored to the project root
    # so every entry point (training, API, MLflow UI) shares one store no matter
    # what directory it was launched from.
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI") or config["mlflow"]["tracking_uri"]
    config["mlflow"]["tracking_uri"] = _resolve_tracking_uri(tracking_uri)

    config["mlflow"]["artifact_location"] = _resolve_artifact_location(config)

    return config


def _resolve_artifact_location(config: dict[str, Any]) -> str | None:
    """
    Chooses where MLflow writes run artifacts.

    In production this must be an **S3 URI**, not a local path. MLflow records
    the artifact location on the experiment at *creation* time and thereafter
    stores absolute paths, so a local location is not portable: a container
    reading the same tracking database looks for a host path that does not
    exist inside it, and even when bind-mounted at the same path it hits a
    uid mismatch on write. An `s3://` location resolves identically from any
    machine, any user, with no mount.

    Locally a relative path is anchored to the project root so every entry
    point agrees on one directory.
    """
    configured = config.get("mlflow", {}).get("artifact_location")

    bucket = os.getenv("S3_BUCKET_NAME")
    if os.getenv("ENV", "local").lower() == "production" and bucket:
        prefix = (
            config.get("paths", {})
            .get("s3_prefixes", {})
            .get("artifacts", "mlflow-artifacts/")
            .rstrip("/")
        )
        return f"s3://{bucket}/{prefix}"

    if configured:
        return str(resolve_path(configured))
    return None


def _resolve_tracking_uri(tracking_uri: str) -> str:
    """Anchors a relative sqlite:/// or file: tracking URI to the project root."""
    for scheme in ("sqlite:///", "file:"):
        if not tracking_uri.startswith(scheme):
            continue

        path = tracking_uri[len(scheme):]
        if not path or path.startswith("/"):
            # Already absolute, or a remote-style URI we should not touch.
            return tracking_uri

        return f"{scheme}{resolve_path(path)}"

    return tracking_uri


def get_feature_names(config: dict[str, Any]) -> list[str]:
    """
    Assembles the final price-model design matrix column order from config.

    The ordering matters: it is baked into the persisted model artifact and the
    API must present columns in exactly this order at inference time.
    """
    features = config["features"]
    fe = config["feature_engineering"]

    hdd_cdd_cols = [f"{ref}_HDD" for ref in fe["hdd_cdd_reference"]]
    hdd_cdd_cols += [f"{ref}_CDD" for ref in fe["hdd_cdd_reference"]]

    feature_names = (
        list(features["calendar"])
        + hdd_cdd_cols
        + list(features["thermal"])
        + list(features["residual_demand"])
    )

    feature_set = features.get("feature_set", "notebook")
    if feature_set == "notebook":
        # Keeps the raw demand/nuclear levels alongside Residual_Demand, as in
        # the notebook. Collinear by construction but reproduces its results.
        feature_names += [fe["nuclear_col"], fe["demand_col"]]
    elif feature_set != "decorrelated":
        raise ValueError(
            f"Unknown features.feature_set: {feature_set!r}. "
            "Expected 'notebook' or 'decorrelated'."
        )

    return feature_names


def get_hdd_cdd_columns(config: dict[str, Any]) -> list[str]:
    """Returns the HDD/CDD column names implied by the configured reference."""
    refs = config["feature_engineering"]["hdd_cdd_reference"]
    return [f"{ref}_HDD" for ref in refs] + [f"{ref}_CDD" for ref in refs]


def get_active_schedule(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns the backtest fold schedule selected by backtest.active_schedule."""
    backtest = config["backtest"]
    name = backtest["active_schedule"]

    if name not in backtest["schedules"]:
        available = ", ".join(backtest["schedules"])
        raise ValueError(
            f"Unknown backtest schedule {name!r}. Available: {available}"
        )

    return backtest["schedules"][name]
