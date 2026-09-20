# Model Release Preparation

Operational record and repeatable procedure for **Phase 1 — produce a deployable
model**. This document is deliberately separate from `MLOPS.md`: that document
describes the lifecycle; this one records the concrete artifact handed to the
container build.

Last verified: **2026-09-20**.

## Outcome

Phase 1 is **technically complete** for the existing registered model. An
immutable, self-contained release candidate has been exported and verified in a
fresh interpreter outside the repository working directory.

It is a **release candidate, not the champion**. No `champion` alias has been
assigned because the project does not yet have an automated release gate, and
the registered model's measured MAE differs from the older tuned result quoted
elsewhere in the design documentation.

| Field | Value |
|---|---|
| Registered model | `french_spot_price_forecaster` |
| Version | `1` |
| Immutable URI | `models:/french_spot_price_forecaster/1` |
| MLflow model ID | `m-a2631b971b174b44959586640bfeb465` |
| Training run | `15dffeab79694b8c8f0df81cc81914da` |
| Training Git commit | `fbe42020234e7195c3e1f0500ff1cb685fa0d0bc` |
| Model | XGBoost, 22 features |
| Training window | 2018-07-01 through 2020-06-30, 731 days |
| Maximum horizon | 31 days |
| Recorded backtest MAE / RMSE | 9.08374875 / 10.61897412 EUR/MWh |
| Exported model files | 39 |
| Exported model size | 642,818 bytes |
| Model-tree SHA-256 | `c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132` |

The export is at:

```text
artifacts/releases/french_spot_price_forecaster-v1/
```

`artifacts/` is gitignored. The directory contains binary model material and
must not be committed. Its `release-manifest.json` records provenance, metrics,
dependencies, the tree checksum and smoke-test results.

## What "deployable" means here

The exported MLflow directory contains everything required to reconstruct an
inference object:

- `python_model.pkl` — fitted `PriceForecaster` with the cascade and price model;
- `code/src/` — the Python class definitions required during unpickling;
- `MLmodel` — flavor, signature, model ID and loader metadata;
- `requirements.txt`, `conda.yaml`, `python_env.yaml` — the recorded runtime;
- input and serving examples.

It does not require the training CSV, notebook, Optuna study, MLflow database or
repository working tree to make a prediction. The next phase can copy this
directory into the serving image and load it from a local immutable path.

## Verification performed

The release tool performs four checks:

1. The URI must name a numeric version. A moving alias such as `@champion` is
   rejected as release input.
2. MLflow must report that exact model version as `READY`.
3. The exported directory must contain a valid `MLmodel` file.
4. A child Python process starts from a temporary directory with `PYTHONPATH`,
   `MLFLOW_TRACKING_URI` and AWS credentials removed, loads the exported model,
   and reproduces five known holdout predictions.

Verified prices:

| Date | Nuclear availability (MW) | Expected | Actual |
|---|---:|---:|---:|
| 2020-07-01 | 29,049 | 34.6852398937 | 34.6852398937 |
| 2020-07-02 | 29,466 | 35.2049142521 | 35.2049142521 |
| 2020-07-03 | 30,605 | 34.9456566913 | 34.9456566913 |
| 2020-07-04 | 29,438 | 32.3707882114 | 32.3707882114 |
| 2020-07-05 | 26,110 | 31.4994447591 | 31.4994447591 |

Maximum absolute difference: **0.0**.

This is a packaging/portability test. It proves that the exported object is the
same object that produced the recorded forecast; it does not prove that the
model is sufficiently accurate for production.

## Repeat the process

From the repository root with the project environment active:

```bash
venv/bin/python scripts/prepare_model_release.py
```

The tool refuses to overwrite a non-empty release directory. For a different
registered version, always use a new version-specific destination:

```bash
venv/bin/python scripts/prepare_model_release.py \
  --model-uri models:/french_spot_price_forecaster/2 \
  --output-dir artifacts/releases/french_spot_price_forecaster-v2
```

Do not export from `@champion`: aliases move. Resolve and record the numeric
version first so the model input to the image build is immutable.

## Important finding: candidate versus documented tuned model

Some earlier design prose reports XGBoost MAE **8.677**. The actual registered
version 1 and its embedded metadata report **9.08374875** and contain only the
base XGBoost parameters, not the older tuned parameter set.

Therefore:

- version 1 is the only registered, reproducibly exportable candidate today;
- it must not be described as the tuned 8.677-MAE model;
- assigning `@champion` requires an explicit decision or a release gate;
- the discrepancy should be resolved before claiming model-quality approval.

No alias was changed during Phase 1.

## Release identity and reproducibility

The training run recorded Git commit
`fbe42020234e7195c3e1f0500ff1cb685fa0d0bc`, which matches the current `HEAD` at
the time of preparation. The generated manifest records the working tree as
dirty because this release procedure and its documentation were being added at
the same time. Before building the final ECR image, rerun the exporter from the
committed release code and retain the new manifest with the image-build
evidence.

The tree SHA-256 covers sorted relative filenames and their bytes. It is
calculated before `release-manifest.json` is written, so the manifest does not
hash itself.

## Handoff to Phases 2 and 3

The local handoff is complete. The exact exported directory is now copied into
the `bundled-serve` Docker target as `/app/model`, verified during the build,
and exercised in an isolated, read-only container. It reproduced the Phase-1
prediction fixture without S3, an MLflow server, a database, host volumes, AWS
credentials or a training process.

See [`DEPLOYMENT_PHASES_2_3.md`](DEPLOYMENT_PHASES_2_3.md) for the architecture,
commands, acceptance evidence and the remaining GitHub Actions/ECR handoff.
That is the boundary between **model release** and **application image release**.
