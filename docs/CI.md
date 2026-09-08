# Continuous Integration and the Test Suite

Two workflows, one test suite, and one design decision that shapes all of it.

- [Why these tests and not others](#why-these-tests-and-not-others)
- [The suite](#the-suite)
- [The CI workflow](#the-ci-workflow)
- [The publish workflow](#the-publish-workflow)
- [Bugs CI found before it ever ran](#bugs-ci-found-before-it-ever-ran)
- [Running it locally](#running-it-locally)
- [What is deliberately not tested](#what-is-deliberately-not-tested)

---

## Why these tests and not others

A forecasting project invites the wrong kind of test. It is tempting to assert
that MAE is below some number — but that pins a *result*, and results move when
data, folds or hyperparameters move. Such a test fails for reasons that are not
bugs, gets marked `xfail`, and stops meaning anything.

The tests here assert **structural invariants**: properties that, when broken,
produce plausible-looking numbers that are silently wrong. Those are the
failures a backtest cannot catch, because a leaking backtest reports a *better*
score, not a worse one.

Four invariants earn a test:

| Invariant | What breaking it looks like |
|---|---|
| Cascade stage ordering | HDD/CDD derived from raw city temperatures instead of `T_lisse`. The original migration defect. |
| Horizon bounds | A 90-day "forecast" that is really climatology, or an in-sample prediction served as a forecast. |
| Fold disjointness | A training window reaching into its own test month. Improves the score, invalidates it. |
| Request unit validation | 29 GW submitted as `29`, driving residual demand deeply negative and producing a confident nonsense price. |

Plus two cheap-but-high-yield checks: that every module imports under the
service's own dependency set, and that the API degrades rather than crashes when
no model is available.

## The suite

53 tests, ~2 seconds, no network, no AWS, no trained model required.

| File | Tests | Asserts |
|---|---|---|
| `tests/conftest.py` | — | A synthetic three-year processed frame, plus a variant already carrying the fold-independent features — mirroring exactly what the training pipeline hands to `prepare_folds`. |
| `tests/test_stage_ordering.py` | 5 | Degree days raise a named `KeyError` before stage 2 and succeed after; HDD and CDD are never both active (the 15–22 °C dead zone); the 22-name design-matrix order is stable; an unknown `feature_set` is rejected. |
| `tests/test_horizon_guards.py` | 7 | `earliest_forecast_date` is `train_end + 1`; the limit itself is accepted and limit+1 rejected; a start inside the training window is rejected, including the off-by-one on `train_end`; a later start is allowed; the limit comes from config, not a literal. |
| `tests/test_fold_leakage.py` | 7 | Runs the **real** `prepare_folds`: `train_end < test_start`, index intersection empty, window length equals `train_window_days`, folds advance monotonically, every configured feature present in order with no NaNs, and a schedule beyond the data raises rather than silently returning nothing. |
| `tests/test_api_schemas.py` | 10 | The GW/MW unit error, NaN, negatives, absurd magnitudes, an empty horizon, inclusive band edges, and that the message names the offending index. |
| `tests/test_api_degraded.py` | 5 | With no model: `/health` returns 503 rather than a bare 200, `/predict` and `/model-info` return 503, a malformed payload still gets 422 (validation runs before the model is consulted), and the OpenAPI schema renders. |
| `tests/test_imports.py` | 19 | Every module imports; the documented routes exist. Training and UI modules guard themselves with `importorskip`. |

The fold-leakage tests are the expensive ones — they fit the whole exogenous
cascade twice — which makes them an integration smoke test of the feature
pipeline as a side effect.

## The CI workflow

`.github/workflows/ci.yml`, on every push and pull request.

```
secret scan  ──┐
tests (serve) ─┼─> images
tests (full)  ─┘
```

**`secret scan`** refuses forbidden filenames (`.env`, `*.pem`, `*.key`,
`mlflow.db`, `*.bak`) and credential-shaped content anywhere in the tree. There
is a local `pre-commit` hook doing the same in `infra/host/git-hooks/`, but a
hook only protects whoever installed it and `git commit --no-verify` skips it —
so the same check runs server-side, where neither is true. It scans the working
tree rather than the diff, so it also catches anything committed before the
hook existed.

This is not ceremony. This repository's `.env` holds a live AWS key pair, and
`git add -f` bypasses `.gitignore` without a warning. The cost of the control
failing once is a force-push plus a key rotation, not an edit.

**`tests (serve)`** installs only `requirements-api.txt` + `requirements-test.txt`
and runs the whole suite. This is the job that keeps the requirements split
honest: if a training-only import (`optuna`, `shap`, `matplotlib`) leaks into
the serving path, this job fails while `tests (full)` still passes. The training
and UI import tests skip here by design.

**`tests (full)`** installs everything and runs with no skips.

**`images`** builds the `serve` and `ui` targets with GitHub Actions layer
caching, then boots the API container and asserts:

- it answers `/health` at all (so it did not crash-loop),
- the generated `openapi.json` renders and contains `/predict` — response-model
  misconfiguration only surfaces at render time,
- the container is **not** running as uid 0.

A 503 from `/health` is the *expected* result in CI: there is no model registry,
and the API is designed to capture a load failure and start unhealthy rather
than raise. A crash-loop and a bare 200 both fail the step — the second because
a service reporting healthy with no model is worse than one plainly down, since
an orchestrator would route traffic to it.

The smoke test publishes on port **18000**, not 8000, and this is not
arbitrary — see below.

No AWS credentials are configured anywhere in `ci.yml`, and `ENV` stays
`local`, so nothing in it can reach S3.

## The publish workflow

`.github/workflows/publish.yml`, on `v*` tags and manual dispatch. Builds the
`serve` and `ui` images and pushes them to ECR.

Authentication is **OIDC**. GitHub mints a short-lived token per run; AWS trades
it for temporary credentials via `github-actions-ecr-push`. No access key is
ever stored as a GitHub secret. The reasoning and the one condition that
actually enforces the boundary are in [`infra/iam/README.md`](../infra/iam/README.md)
— short version: the `aud` check alone would let *any* GitHub repository assume
the role, and the `sub` condition is what scopes it to this one.

Configuration is three repository **variables**, not secrets:

| Variable | Example |
|---|---|
| `AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/github-actions-ecr-push` |
| `AWS_REGION` | `eu-west-1` |
| `ECR_REPOSITORY` | `epex-forecaster` |

A role ARN is not a credential, and keeping it visible makes the wiring
auditable. Until all three are set, a `preflight` job skips the build and writes
*why* into the run summary — so a fresh clone has green CI without an AWS
account, and a misconfiguration reads as a message rather than an unexplained
skip.

Each image gets two tags: a moving one (`:serve`) and an immutable one
(`:serve-<sha>`). **Deployments should pin the SHA tag** — rollback is then a
matter of naming the previous commit.

Run `infra/iam/apply-github-oidc.sh` once to create the OIDC provider, the ECR
repository and the role; it prints the three variable values to paste in.

## Bugs CI found before it ever ran

Writing these checks and verifying them locally surfaced three real defects.
Recording them because they are the argument for the checks existing.

**1. `src/models/registry.py` imported `optuna` at module scope.** `optuna` is a
training dependency, absent from the slim serving environment — but `registry`
is on the *serving* path, because `NaiveForecaster` lives there and unpickling a
`Baseline_Seasonal` champion imports it. So a baseline champion could not have
been loaded by the `:serve` image at all. This was latent: it only bites when
the baseline wins. Fixed by moving `optuna` behind `TYPE_CHECKING` with
`from __future__ import annotations` — it is used only in type positions, and
the search spaces are called only during tuning, where it is installed.

**2. The container smoke test passed against the wrong process.** A leftover
host `uvicorn` was bound to `127.0.0.1:8000`. A host process on the loopback
address wins over Docker's `0.0.0.0:8000` port proxy, so `curl 127.0.0.1:8000`
answered from the host — reporting a healthy model that the container did not
have. Publishing on 18000 removes the ambiguity. The general lesson: a smoke
test that can be satisfied by something other than the artifact under test is
not a test.

**3. `pd.Timedelta(days=N)` emits a `DeprecationWarning`** under pandas 2.3.3
with numpy 2.5.2, from inside pandas' own constructor, and it is documented to
become an error. Five call sites across `backtest`, `forecaster`, `registry` and
`train_pipeline` — all on the date arithmetic that defines fold boundaries.
`pd.Timedelta(N, unit="D")` is identical in meaning and takes a different path.

An earlier draft of the IAM policy files carried `_comment` keys for
readability; IAM accepts only `Version`, `Id` and `Statement` at the top level
and would have failed at apply time with `MalformedPolicyDocument`. The
explanation moved to `infra/iam/README.md`.

## Running it locally

Everything in this section, plus the non-CI commands, is collected in
[`RUNBOOK.md`](RUNBOOK.md).

```bash
pip install -r requirements-dev.txt
pytest                      # 53 tests, ~2s
pytest -v -k leakage        # one area
```

Install the secret-guard hook once per clone:

```bash
./infra/host/git-hooks/install.sh    # sets core.hooksPath
```

`core.hooksPath` is used rather than copying into `.git/hooks`, so the hook
stays version-controlled and a fix arrives with the next pull.

To reproduce CI's slim-environment job without installing a second virtualenv,
run the suite *inside* the serving image — which is a stronger check, because
the image's dependency set is the real one:

```bash
docker build --build-arg REQUIREMENTS=requirements-api.txt -t epex-forecaster:serve .

docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/pytest.ini:/app/pytest.ini:ro" \
  -v "$PWD/requirements-test.txt:/app/requirements-test.txt:ro" \
  -e PROJECT_ROOT=/app -e ENV=local --user root \
  epex-forecaster:serve \
  sh -c "pip install -q -r requirements-test.txt && python -m pytest -q -rs"
```

Expect **48 passed, 5 skipped** — the skips are the training and UI import
guards, and they are the point.

## What is deliberately not tested

Stated plainly, because an untested area you know about is a different thing
from one you do not.

- **Model accuracy.** Belongs to the walk-forward backtest, which needs the real
  dataset. CI has no data and should not.
- **The training pipeline end to end.** Minutes of compute and it needs the real
  CSVs. Run locally; the fold-leakage tests cover its riskiest stage.
- **S3 reads and writes.** No credentials in CI, by design. `S3Store` is gated on
  `ENV` *and* a bucket name, so it is inert in tests. Verified manually against
  the real bucket.
- **The Streamlit UI's rendered output.** `AppTest` could cover it; the `:ui`
  image build and the `src.ui.app` import check are what CI does today.
- **Notebook fidelity.** Proven twice by hand (fold-level 8.35/11.33 MAE, tuned
  ElasticNet 11.737 vs the notebook's 11.775). Re-proving it needs the dataset.
