# One image, three roles. The service is chosen by the command, not the image,
# so training and serving can never drift apart in their dependencies — the
# thing that causes "it trained fine but the API can't unpickle it".
FROM python:3.12-slim AS base

# PYTHONUNBUFFERED: without it, print() sits in a block buffer and container
#   logs stay empty until the process exits — which makes a hung training run
#   indistinguishable from a silent one.
# PYTHONDONTWRITEBYTECODE: no .pyc in a layer that is thrown away anyway.
# PROJECT_ROOT: config_loader resolves paths from this rather than __file__,
#   which matters because MLflow's code_paths shadows the src package.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PROJECT_ROOT=/app

WORKDIR /app

# Minimal build deps. libgomp1 is required at runtime by XGBoost (OpenMP);
# without it the import fails with an opaque shared-object error.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Which dependency set to install. Defaults to the full set, so a plain
# `docker build` still produces an image that can BOTH train and serve — which
# is what running training on EC2 later requires. Override for a slim service:
#
#   docker build --build-arg REQUIREMENTS=requirements-api.txt .
#
# Every version is pinned once in requirements-base.txt, which all the
# per-service files include, so no two images can resolve different versions of
# the same library. That matters because the model artifact is a cloudpickle of
# live objects and a version mismatch breaks unpickling.
ARG REQUIREMENTS=requirements.txt

# Requirements first, as their own layer: source changes then rebuild in
# seconds instead of reinstalling the whole dependency set. All the files are
# copied because the per-service ones use `-r requirements-base.txt`.
COPY requirements*.txt ./
# nvidia-nccl-cu13 arrives as an xgboost dependency and is 288 MB, but it is
# only used for multi-GPU collective ops. This project trains on CPU
# (tree_method="hist"), so it is removed in the SAME layer — uninstalling in a
# later layer would leave the bytes in the image.
RUN pip install --no-cache-dir -r "${REQUIREMENTS}" \
 && pip uninstall -y nvidia-nccl-cu13 \
 && find /usr/local/lib/python3.12/site-packages -name "tests" -type d -prune -exec rm -rf {} + \
 && find /usr/local/lib/python3.12/site-packages -name "*.pyc" -delete

COPY configs/ ./configs/
COPY src/ ./src/

# Non-root. Nothing here needs privileges, and a container that cannot write
# outside its own directories limits the damage from a dependency compromise.
RUN useradd --create-home --shell /bin/bash app \
 && mkdir -p /app/results /app/mlartifacts /app/data \
 && chown -R app:app /app

# ---------------------------------------------------------------------------
# Production inference target. The exact numeric MLflow model version prepared
# in Phase 1 is copied into the image and checked before the layer is accepted.
# This makes a container restart independent of S3, the MLflow registry,
# mlflow.db and host volumes. MODEL_URI is a local immutable path.
FROM base AS bundled-serve

ARG MODEL_ARTIFACT=artifacts/releases/french_spot_price_forecaster-v1
ARG MODEL_TREE_SHA256

COPY --chown=app:app ${MODEL_ARTIFACT}/ /app/model/

RUN test -n "${MODEL_TREE_SHA256}" \
 && python -m src.utils.artifact_digest /app/model \
      --exclude release-manifest.json \
      --expect "${MODEL_TREE_SHA256}"

ENV MODEL_URI=/app/model \
    MODEL_TREE_SHA256=${MODEL_TREE_SHA256}

USER app

EXPOSE 8000 8501

# Default to serving. docker-compose overrides this per service.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Default target retained for local registry-backed API, UI, MLflow and trainer
# builds. Only an explicit `--target bundled-serve` carries a model artifact.
FROM base AS generic

USER app

EXPOSE 8000 8501

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
