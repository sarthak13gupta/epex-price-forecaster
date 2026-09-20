# Deployment Phases 2 and 3 — Bundled Inference Image

This is the implementation record and repeatable runbook for:

- **Phase 2 — package the released model with the API**;
- **Phase 3 — prove that the container serves without external state**.

Last verified: **2026-09-20**.

## Outcome

Both phases are complete locally. The project now has a dedicated
`bundled-serve` Docker target whose model is fixed at build time. A container
created from it can start, restart and predict with:

1. no S3 or other network access;
2. no MLflow tracking server or tracking URI;
3. no MLflow database;
4. no host volume and a read-only root filesystem;
5. no training process.

The word *MLflow* needs one important distinction: the image still contains the
MLflow Python library because the Phase-1 artifact is in MLflow's pyfunc format.
It uses that library locally to deserialize `/app/model`; it does not contact an
MLflow server, registry, database or artifact store.

## The serving flow

```text
Phase-1 numeric model version
        |
        | exported once; immutable checksum recorded
        v
artifacts/releases/french_spot_price_forecaster-v1/
        |
        | Docker build copies and verifies it
        v
bundled image: code + dependencies + /app/model
        |
        | container boot: mlflow.pyfunc.load_model("/app/model")
        v
model held in API-process memory
        |
        | POST /predict
        v
JSON forecast
```

The image layer is read-only. The model is read from that layer during startup
and then used from process memory for requests. There is no runtime artifact
download.

## Phase 2 — package the model

### 2.1 Release input

The build consumes the exact candidate prepared in Phase 1:

| Field | Value |
|---|---|
| Model | `french_spot_price_forecaster` version `1` |
| Artifact directory | `artifacts/releases/french_spot_price_forecaster-v1` |
| Model-tree SHA-256 | `c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132` |
| Files / bytes covered | 39 / 642,818 |

The digest excludes `release-manifest.json`, matching the Phase-1 calculation
in which the manifest is written after the model tree is hashed.

### 2.2 Docker build boundary

The Dockerfile has two final targets:

- `generic` remains the default for the existing local registry-backed API,
  MLflow UI, Streamlit UI and training workflows;
- `bundled-serve` is the self-contained deployment target.

`bundled-serve` installs `requirements-api.txt`, copies the release directory
to `/app/model`, and runs `src.utils.artifact_digest` during the build. An empty
or incorrect expected checksum makes the build fail. A deliberate build with an
all-zero checksum was rejected and reported the correct actual checksum.

`.dockerignore` excludes generated artifacts by default and allow-lists only
the version-1 release directory. This prevents unrelated experiments, databases
or models from entering the build context accidentally.

To build directly:

```bash
docker build \
  --target bundled-serve \
  --build-arg REQUIREMENTS=requirements-api.txt \
  --build-arg MODEL_ARTIFACT=artifacts/releases/french_spot_price_forecaster-v1 \
  --build-arg MODEL_TREE_SHA256=c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132 \
  -t epex-forecaster:serve-bundled .
```

At runtime the target fixes `MODEL_URI=/app/model` and runs as the non-root
`app` user.

### 2.3 Compose path

The `api-bundled` service is behind the `bundled` profile so it does not alter
the established local development stack:

```bash
docker compose --profile bundled build api-bundled
docker compose --profile bundled up -d api-bundled

curl --noproxy '*' http://127.0.0.1:18000/health
curl --noproxy '*' -X POST http://127.0.0.1:18000/predict \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2020-07-01","nuclear_avail":[29049,29466,30605]}'

docker compose --profile bundled down
```

Port `18000` is intentionally separate from the development API on `8000`.
It is bound to loopback only. The service has no volumes, drops all Linux
capabilities, enables `no-new-privileges`, and makes the root filesystem
read-only.

## Phase 3 — isolation and contract validation

Run the complete acceptance test from the repository root:

```bash
bash scripts/verify_bundled_container.sh
```

The script owns one temporary container named `epex-phase3-verify`, refuses to
replace a pre-existing container with that name, and removes only its own
container on exit. It deliberately starts the candidate with `--network none`,
no published port, no mount, and a read-only root filesystem. Requests are made
from inside the container.

### Verified evidence

| Requirement | Evidence from the verified container |
|---|---|
| Correct artifact | In-image tree checksum exactly matched Phase 1; 39 files, 642,818 bytes |
| API boot | `/health` returned `status=ok`, `model_loaded=true`, `model_uri=/app/model` |
| Prediction contract | Five Phase-1 prices reproduced with maximum absolute error `0.0` |
| No S3 / network | Docker network mode was `none`; no AWS credential variables existed |
| No MLflow server | No tracking URI and no tracking service; the only process was the API and its workers |
| No database | `/app/mlflow.db` did not exist |
| No persistent/local writable disk | Mount list was empty and root filesystem was read-only |
| No training | No `train_pipeline` or `optuna` process was present |
| Least privilege | Container user was `app`, all capabilities dropped, `no-new-privileges` enabled |
| Restart independence | Container restart completed and `/health` returned OK again |

Verified prices:

```text
[34.68523989365982,
 35.20491425207375,
 34.94565669127315,
 32.37078821136852,
 31.499444759126654]
```

The final local image inspection reported:

| Field | Value |
|---|---|
| Image ID | `sha256:ed364170cdb9ac6d3f1823f164ad2b9c6a2640939030cdf917f6cfcae1cf070f` |
| Size | 288,277,896 bytes |
| OS / architecture | Linux / arm64 |
| Configured user | `app` |

The local image ID is evidence for this build, not the eventual deployment
identity. Build provenance can change a local ID between otherwise equivalent
BuildKit builds. After publishing, the ECR repository digest is the immutable
identifier to record and deploy.

`/backtest-metrics` is not part of this isolated guarantee: it reads generated
results that are intentionally absent from the image. The deployment contract
validated here is model startup, `/health`, and online `/predict`.

## Releasing the next model version

Never overwrite version 1 or build from a moving alias. For version 2:

1. Export `models:/french_spot_price_forecaster/2` to a new version-specific
   directory with `scripts/prepare_model_release.py`.
2. Review the new manifest, quality metrics and smoke predictions before
   approving it.
3. Add only that new directory to the narrow `.dockerignore` allow-list.
4. Change the `MODEL_ARTIFACT` and `MODEL_TREE_SHA256` defaults for
   `api-bundled`, or pass them explicitly at build time.
5. Run `bash scripts/verify_bundled_container.sh` with those two environment
   variables.
6. Publish the verified image and record the numeric model version, model-tree
   checksum, Git commit and ECR digest together.

Example validation without changing defaults:

```bash
MODEL_ARTIFACT=artifacts/releases/french_spot_price_forecaster-v2 \
MODEL_TREE_SHA256=<version-2-tree-sha256> \
IMAGE=epex-forecaster:serve-bundled-v2 \
bash scripts/verify_bundled_container.sh
```

The expected prediction fixture in the verification script must also be updated
from approved version-2 evidence; otherwise the old model's numbers correctly
cause the test to fail.

## Phase 4 handoff

Phases 2 and 3 prove the image locally; they do not publish or deploy it. The
Phase-4 mechanism is now implemented: a checksummed GitHub Release asset supplies
the gitignored model, the workflow builds `bundled-serve` for Linux/amd64, runs
this same isolation contract, and pushes that tested image to ECR through OIDC.

The amd64 candidate passed locally. External publication still requires the
model asset, AWS OIDC setup, repository variables and a committed release tag.
See [`DEPLOYMENT_PHASE_4.md`](DEPLOYMENT_PHASE_4.md) for the exact procedure and
exit criteria.

The image still contains the full MLflow package to read the local pyfunc
bundle. At roughly 288 MB by Docker inspection it is acceptable for the first
deployment; replacing it with a lighter serialization or dependency set is a
later optimization, not a Phase-3 requirement.
