# Runbook — how to run this project

Every command needed to go from a fresh clone to a served forecast, plus what
to do when each step fails. The failure notes are the point: most of them are
mistakes that were actually made here, not hypotheticals.

- [0. Prerequisites](#0-prerequisites)
- [1. First-time setup](#1-first-time-setup)
- [2. Train a model](#2-train-a-model)
- [3. Serve it](#3-serve-it)
- [4. Run it in Docker](#4-run-it-in-docker)
- [5. Tests and CI](#5-tests-and-ci)
- [6. Switch to S3](#6-switch-to-s3)
- [7. Regenerate the docs](#7-regenerate-the-docs)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Command index](#9-command-index)

---

## 0. Prerequisites

| Need | Version used here | Notes |
|---|---|---|
| Python | 3.12 | 3.11 should work; nothing is 3.12-specific |
| Docker | 29.2.1 | Only for §4. Engine or Desktop |
| Docker Compose | v5.0.2 | The `compose` plugin, not the old `docker-compose` binary |
| Disk | ~4 GB | 1.6 GB venv, plus images if you use Docker |
| AWS account | — | **Not required.** `ENV=local` runs everything from disk |
| AWS CLI v2 | 2.36.40 | Only for the IAM scripts. No sudo needed — see below |

The AWS CLI installs user-local, which matters on a machine without
passwordless sudo:

```bash
curl -sS https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
python -c "import zipfile; zipfile.ZipFile('/tmp/awscliv2.zip').extractall('/tmp')"
chmod +x /tmp/aws/install /tmp/aws/dist/aws
/tmp/aws/install --install-dir ~/.local/aws-cli --bin-dir ~/.local/bin
```

`unzip` is not present on this image and `python -c` does the same job.
Note that the CLI **does not read `.env`** — see `ROADMAP.md` §B0.

The raw CSVs are **not** in the repository (they are git-ignored). Place them at:

```
data/raw/epex_fr_temp_train.csv     # history through 2020-06-30
data/raw/epex_fr_temp_pred.csv      # the future-exogenous file: dates + nuclear only
```

## 1. First-time setup

```bash
git clone git@github.com:sarthak13gupta/epex-price-forecaster.git
cd epex-price-forecaster

python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt        # full env + test toolchain

cp .env.example .env                       # ENV=local is the default
./infra/host/git-hooks/install.sh          # secret guard, once per clone
```

Confirm the install before doing anything else — this takes 2 seconds and
catches a broken environment immediately:

```bash
pytest
# expect: 53 passed
```

**Which requirements file?**

| File | Installs | Use when |
|---|---|---|
| `requirements-dev.txt` | everything + pytest | developing (**start here**) |
| `requirements.txt` | everything | running training and serving, no tests |
| `requirements-api.txt` | inference only | building a slim serving image |
| `requirements-ui.txt` | Streamlit only | building the UI image |
| `requirements-train.txt` | training only | a training-only container |

They all inherit one pinned `requirements-base.txt`, so no set is ever
independently resolved and versions cannot drift between services.

## 2. Train a model

### Step 1 — cache the cleaned data

```bash
python -m src.data.preprocess
```

Writes `data/processed/clean_data.parquet`. Reindexes to a complete daily
calendar **before** interpolating, so a missing day is interpolated rather than
silently skipped.

### Step 2 — run the pipeline

```bash
# Full run: benchmark 3 models, 50 Optuna trials each, SHAP, register the winner
python -m src.pipelines.train_pipeline

# Fast path: default hyperparameters, no SHAP, no registry write
python -m src.pipelines.train_pipeline --skip-tuning --skip-shap --no-register
```

| Flag | Effect | When |
|---|---|---|
| `--skip-tuning` | Default hyperparameters instead of Optuna | Smoke-testing a code change |
| `--skip-shap` | Skips attribution, which dominates runtime | Iterating on the pipeline |
| `--no-register` | Logs to the run but does not touch the registry | Experiments you do not want served |

The six stages, in order: load and validate → static features → prepare folds →
benchmark and tune → select and register → holdout forecast. Fold preparation is
cached across models and Optuna trials, because the cascade is invariant to the
price model's hyperparameters — 12 cascade fits instead of 600.

### Step 3 — look at the results

```bash
mlflow ui --backend-store-uri sqlite:///mlflow.db      # → http://localhost:5000
```

The parent run holds the leaderboard; each model is a nested child run.

## 3. Serve it

Three processes, three terminals. Order matters: the UI needs the API, and the
API needs a registered model.

```bash
# 1. API
uvicorn src.api.main:app --reload
#    → http://localhost:8000/docs   (interactive OpenAPI)

# 2. UI
streamlit run src/ui/app.py
#    → http://localhost:8501

# 3. MLflow (optional, for browsing runs)
mlflow ui --backend-store-uri sqlite:///mlflow.db
```

Check the API is actually serving a model:

```bash
curl --noproxy '*' localhost:8000/health
# {"status":"ok","model_loaded":true,"model_uri":"models:/french_spot_price_forecaster/1"}
```

A **503** here means no model is registered — run §2 first. That is by design:
the API captures a load failure and starts unhealthy rather than crash-looping,
so you can ask it what went wrong.

Make a forecast:

```bash
curl --noproxy '*' -X POST localhost:8000/predict \
  -H 'Content-Type: application/json' \
  -d '{"start_date": "2020-07-01", "nuclear_avail": [29049, 29466, 30605]}'
```

The horizon is the length of `nuclear_avail`. Values are **MW**, not GW — the
band is 1,000–80,000 and anything outside it is rejected with a 422 naming the
offending index.

> **`--noproxy '*'` is not optional if you have `HTTP_PROXY` set.** `curl` honours
> it even for loopback, and you get a 504 from the proxy while the API is
> perfectly healthy. The Streamlit client sets `trust_env = False` for the same
> reason.

## 4. Run it in Docker

```bash
export HOST_PROJECT_DIR=$PWD HOST_UID=$(id -u) HOST_GID=$(id -g)

docker compose config --quiet          # validate first
docker compose up -d                   # api + mlflow
docker compose --profile ui up -d ui   # + Streamlit on :8501
docker compose ps                      # health status
docker compose logs -f api
docker compose down
```

One-off training run in a container:

```bash
docker compose --profile train run --rm train
```

`HOST_UID`/`HOST_GID` exist because MLflow **writes** `registered_model_meta`
when loading a model, so a uid mismatch on the bind-mounted artifact store gives
`Permission denied`. (`UID` is read-only in bash, hence the `HOST_` prefix.)

All ports bind to `127.0.0.1` only. **MLflow has no authentication** — port 5000
must never be internet-exposed.

Full detail, including the Dockerfile decisions and image sizes, is in
[`DOCKER.md`](DOCKER.md).

## 5. Tests and CI

```bash
pytest                       # 53 tests, ~2s
pytest -v                    # per-test names
pytest -k leakage            # one area
pytest --collect-only -q     # what exists
```

To reproduce CI's slim-environment job — a stronger check than a second
virtualenv, because the image's dependency set is the real one:

```bash
docker build --build-arg REQUIREMENTS=requirements-api.txt -t epex-forecaster:serve .

docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/pytest.ini:/app/pytest.ini:ro" \
  -v "$PWD/requirements-test.txt:/app/requirements-test.txt:ro" \
  -e PROJECT_ROOT=/app -e ENV=local --user root \
  epex-forecaster:serve \
  sh -c "pip install -q -r requirements-test.txt && python -m pytest -q -rs"
# expect: 48 passed, 5 skipped
```

The 5 skips are the training and UI import guards, and they are the point — see
[`CI.md`](CI.md).

## 6. Switch to S3

Only needed to exercise the production data path. Everything above works without
it.

```bash
# in .env
ENV=production
AWS_DEFAULT_REGION=eu-west-1
S3_BUCKET_NAME=<your-bucket>
S3_TRAIN_FILE_KEY=raw/epex/train/epex_fr_temp_train.csv
S3_PRED_FILE_KEY=raw/epex/predict/epex_fr_temp_pred.csv
```

With `ENV=production`, `data_loader` reads from S3, `S3Store` archives each
forecast under `forecasts/dt=YYYY-MM-DD/` (the date the forecast was **made** —
its vintage), and MLflow's artifact location becomes `s3://<bucket>/mlflow-artifacts`.

Credentials come from boto3's chain: environment variables, then
`~/.aws/credentials`, then an IAM instance role. **On EC2, `.env` must not
contain `AWS_ACCESS_KEY_ID`** — the chain stops at step 1 and never reaches the
instance role, and the symptom is an `AccessDenied` that looks like a broken
policy rather than a shadowed credential. `infra/iam/verify-on-instance.sh`
checks for exactly this.

See [`AWS_S3_EC2.md`](AWS_S3_EC2.md).

## 7. Regenerate the docs

Every `.docx` is generated from its Markdown, so the Markdown is the source:

```bash
python scripts/md_to_docx.py docs/DESIGN.md docs/DESIGN.docx
python scripts/md_to_html.py docs/DESIGN.md /tmp/design.html
```

Diagram sources are SVG alongside the rendered PNGs in `docs/images/`, so they
stay editable.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `504 Gateway Timeout` from `localhost:8000` while the API is healthy | `curl`/`requests` honour `HTTP_PROXY` even for loopback | `curl --noproxy '*'`; in Python, `session.trust_env = False` |
| `/health` returns 503, `"No registered versions found"` | No model in the registry | Run §2. This is correct behaviour, not a bug |
| `KeyError: ['T_lisse']` from `engineer_degree_days` | Degree days were built before the national-temperature stage | Stage ordering is a correctness requirement; `engineer_national_temperature` must run first |
| `Permission denied` writing MLflow artifacts in Docker | Container uid ≠ host uid, and MLflow *writes* on model load | `export HOST_UID=$(id -u) HOST_GID=$(id -g)` before `compose up` |
| `No such artifact: ''` from a container | MLflow bakes **absolute** artifact paths at experiment creation | Use an `s3://` artifact location, or recreate the experiment |
| `docker: command not found` after Docker Desktop stops | `/usr/bin/docker` is a symlink into Desktop's mount | Install a native engine: `sudo ./infra/host/install-docker-engine.sh` |
| `FileNotFoundError` for `configs/config.yaml` inside a served model | MLflow's `code_paths` shadows the `src` package, so `__file__` points inside the artifact | Set `PROJECT_ROOT`; the loader's 3-tier discovery handles the rest |
| `ConvergenceWarning` from ElasticNet | The `notebook` feature set is collinear by construction (condition number ~10¹⁶) | Expected. `features.feature_set: decorrelated` is the alternative |
| `SchemaError` on the prediction file | The training schema rejects the deliberately-empty future columns | `prepare_future_exogenous` + `FutureExogenousSchema` handle this; check you are on the predict path |
| `ssh: connect to github.com port 22: Connection timed out` | Transient, or the network blocks 22 | Retry. SSH-over-443 is **reset** on this network, so the real fallback is HTTPS with a PAT |
| Optuna `best_params` comes back as `{'check_inverse': True}` | `get_params(deep=False)` on `TransformedTargetRegressor` sees nothing | The pipeline threads the study's params through explicitly |

## 9. Command index

```bash
# --- setup ------------------------------------------------------------------
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
./infra/host/git-hooks/install.sh

# --- data and training ------------------------------------------------------
python -m src.data.preprocess
python -m src.pipelines.train_pipeline
python -m src.pipelines.train_pipeline --skip-tuning --skip-shap --no-register

# --- serving ----------------------------------------------------------------
uvicorn src.api.main:app --reload                    # :8000
streamlit run src/ui/app.py                          # :8501
mlflow ui --backend-store-uri sqlite:///mlflow.db    # :5000

# --- tests ------------------------------------------------------------------
pytest
pytest -k leakage -v

# --- docker -----------------------------------------------------------------
export HOST_PROJECT_DIR=$PWD HOST_UID=$(id -u) HOST_GID=$(id -g)
docker compose up -d
docker compose --profile ui up -d ui
docker compose --profile train run --rm train
docker compose down

# --- aws (one-time, needs an admin identity) --------------------------------
REPO=sarthak13gupta/epex-price-forecaster ./infra/iam/apply-github-oidc.sh
POLICY=inference-only ./infra/iam/apply.sh
./infra/iam/verify-on-instance.sh          # run ON the instance

# --- docs -------------------------------------------------------------------
python scripts/md_to_docx.py docs/DESIGN.md docs/DESIGN.docx
```
