# Measured Corrections and Reformations

Verified findings from code-level investigation, each with the evidence that
produced it and the change it implies. Distinct from `ASSESSMENT.md`, which holds
the strategic roadmap — everything here is a concrete, measured, local fix.

Status legend: **OPEN** = not yet applied · **MEASURED** = evidence gathered, change pending

---

## 1. `is_lockdown` is a dead feature in 10 of 12 folds — OPEN

**Evidence.** The COVID lockdown window is `2020-03-17` to `2020-05-11`. Each
backtest fold trains on a 731-day window; only folds 11 and 12 overlap that
window. In the other 10 folds the column is **constant zero**.

A zero-variance column makes the standardised design matrix exactly singular on
its own, independent of any other collinearity. Measured condition number with
the column present and dead: ~10^16.

**Implications.**
- It contributes no signal in 10 of 12 folds, only numerical damage.
- Any regularisation path computed on those folds is operating on a singular
  matrix.

**Change to make.** One of:
- Drop `is_lockdown` from `features.calendar` and accept the 2020 folds lose
  their regime marker; or
- Generalise it to a `is_demand_shock` flag with a configurable list of windows,
  so it can carry future events (2022 nuclear crisis, etc.); or
- Keep it but assert non-constancy in `prepare_folds` and warn, so the situation
  is at least visible.

**Recommendation:** generalise to a configurable shock-window list. The feature
is conceptually right; its hard-coded single window is what makes it dead.

---

## 2. `decorrelated` feature set is now measured, not hypothetical — MEASURED

`ASSESSMENT.md` §10 lists this as untested. It has now been measured.

**Evidence.** Condition number of the fold-1 design matrix, dead columns excluded:

| Feature set | Features | Condition number |
|---|---|---|
| `notebook` (residual + both levels) | 21 | 7.2 x 10^15 |
| `decorrelated` (residual only) | 19 | 42.6 |

Reproduced across all 12 folds: `notebook` is ~10^16 in every fold,
`decorrelated` is 41-43 in every fold.

**This explains the ElasticNet `ConvergenceWarning`s.** Coordinate descent on an
exactly singular design matrix is precisely the failure mode. It was never a
tolerance issue — it is the identity `Residual_Demand = DEMAND - NUCLEAR`.

**Consequence for interpretation.** Under `notebook`, coefficients are not
identified: adding any multiple of the null direction `(1, -1, 1)` across
`(NUCLEAR, DEMAND, Residual_Demand)` leaves predictions and R^2 unchanged to six
decimal places while moving the `Residual_Demand` coefficient from -0.019 to
+0.051. No statement of the form "1,000 MW more residual demand raises price by
X" can be made under the current config.

**Note:** this harms interpretation only, not prediction. XGBoost won at 8.68 MAE
and is immune — trees have no coefficients to be non-identified. The current
default is therefore defensible *because the selected model is a tree*.

---

## 3. `decorrelated` over-corrects — a better third option exists — OPEN

**Evidence.** Removing any *one* of the three dependent columns breaks the
identity. `decorrelated` removes two.

| Variant | Features | Condition number |
|---|---|---|
| `notebook` (residual + both levels) | 21 | 7.2 x 10^15 |
| `decorrelated` (residual only) | 19 | 42.6 |
| **residual + NUCLEAR only** | 20 | **45.0** |
| residual + DEMAND only | 20 | 46.2 |

Keeping one level costs ~2 points of condition number and retains information
`decorrelated` discards: `Residual = 10,000` is produced both by
`DEMAND 50,000 / NUCLEAR 40,000` and by `DEMAND 60,000 / NUCLEAR 50,000`, which
are physically different grid states.

**Why NUCLEAR is the level to keep.** At inference time `NUCLEAR` is *exact* —
the published maintenance schedule — whereas `DEMAND` is simulated by the
LoadForecaster and carries ~2.2% error. `Residual_Demand` inherits that error.
Adding the exact column lets the model separate certain (nuclear outage) from
estimated (demand) contributions to residual demand. Adding `DEMAND` instead
contributes a column no more reliable than the residual already is.

**Change to make.** Add a third `features.feature_set` option,
`residual_plus_nuclear`, and benchmark all four variants on MAE/RMSE.

---

## 4. Weather-model error saturates, it does not compound — MEASURED

**Evidence.** Cascade fitted through 2020-05-31, simulating June 2020 (where
actuals are known). Paris temperature, mean absolute error by horizon bucket:

| Horizon | MAE |
|---|---|
| days 1-7 | 4.11 degC |
| days 8-14 | 3.66 degC |
| days 15-21 | 3.24 degC |
| days 22-30 | 3.08 degC |
| whole month | 3.49 degC |

**Interpretation.** The AR(2) term decays within roughly a week, after which the
forecast is pure climatology — whose error is horizon-independent. So error rises
to the climatology level and flattens rather than compounding. This *confirms*
the `max_horizon_days: 31` rationale in `DESIGN.md`, and corrects the looser
verbal claim that error "compounds with horizon".

**No change required** — but the numbers are worth quoting rather than asserting.

---

## 5. Cascade error attenuates through the degree-day transform — MEASURED

**Evidence.** Same June 2020 simulation: temperature MAE 3.49 degC produced
demand MAE of only 926 MW (2.2% of the actual mean).

**Interpretation.** In June, `T_lisse` sits in the 15-22 degC band where both HDD
and CDD are zero. Demand is temperature-insensitive there, so temperature error
largely vanishes through the transform. The calendar features carry the demand
forecast in that regime.

**Implication.** Error propagation through a cascade is *not* uniform — it
depends on the local sensitivity of each transform. The same 3.5 degC error would
propagate far more in January, where HDD is large and demand is steeply
temperature-dependent. **A winter-fold error-propagation check is worth adding**,
since the current `extended_summer` schedule never tests the sensitive regime.

---

## 6. `INTERNALS.md` overstates the arcsinh rationale — OPEN

`INTERNALS.md` states arcsinh was chosen "for its closed-form invertibility".
That is weak: Yeo-Johnson is also closed-form invertible (piecewise).

**The real, measurable reason is parameter robustness.** Adding 3 extreme
observations to a 700-point sample (0.4%):

| Parameter | Before | After | Change |
|---|---|---|---|
| arcsinh `median_` | 37.368 | 37.395 | +0.1% |
| arcsinh `mad_` | 5.943 | 6.037 | +1.6% |
| Yeo-Johnson `lambda_` | 1.063 | -0.233 | **-122%** |

Median and MAD are robust statistics; lambda is an MLE over the whole
distribution. With 12 folds on spiky electricity prices, a fitted lambda would
differ per fold, putting the target on a *different scale in each fold* and making
transformed-space losses non-comparable.

**Also worth correcting:** arcsinh is not "leakage-proof by form". It has fitted
parameters (`median_`, `mad_`) exactly as Yeo-Johnson does. Leakage protection
comes from `TransformedTargetRegressor` refitting inside each fold, not from the
transform's functional form.

**Change to make.** Rewrite the arcsinh rationale in `INTERNALS.md` around
parameter robustness and cross-fold comparability.

---

---|---|---|---|
| 1 | `is_lockdown` dead in 10/12 folds | code + config | small |
| 2 | `decorrelated` conditioning measured | doc update | trivial |
| 3 | Add `residual_plus_nuclear` variant + benchmark all four | code + experiment | small |
| 4 | Quote measured weather error instead of asserting | doc update | trivial |
| 5 | Add a winter-fold error-propagation check | experiment | small |
| 6 | Fix arcsinh rationale | doc update | trivial |

---

## 7. Cascade hyperparameters are frozen and untested — OPEN

**Evidence.** Every parameter in the Optuna search space is prefixed
`regressor__model__` — i.e. price-model only. Seven cascade hyperparameters are
never tuned:

| Parameter | Value | Provenance |
|---|---|---|
| `heating_threshold_c` | 15.0 | industry convention |
| `cooling_threshold_c` | 22.0 | industry convention |
| `ewm_alpha` | 0.5 | chosen (~1-day half-life) |
| `train_window_days` | 731 | chosen (2 annual cycles) |
| `warmup_days` | 14 | chosen |
| `LoadForecaster` Ridge `alpha` | 1.0 | sklearn default, hardcoded |
| Fourier harmonics | 2 | hardcoded |

**Why this matters.** The fold-caching optimisation (12 cascade fits instead of
600) is valid *only because* these are excluded from the search — that exclusion
is what makes `X_train` constant across trials. It is a consequence of what was
chosen to tune, not a property of the cascade.

Meanwhile the tuner set `colsample_bytree` to 0.998 — effectively "use all
features" — suggesting it had little left to gain in the price model, while
genuinely influential physical constants sit at conventional guesses.

**Change to make — two-tier tuning.** Do not jointly tune cascade params against
price error; the signal is weak and it invalidates caching. Instead tune each
subordinate model against its own target:

- `ewm_alpha`, `heating_threshold_c`, `cooling_threshold_c` -> minimise **demand**
  MAE (the LoadForecaster's actual objective)
- Fourier harmonic count, `train_window_days` -> minimise **temperature** MAE
  (the WeatherForecaster's actual objective)

Cheap, better-posed, and leaves the price-model caching intact.

---

## 8. The tuning objective is mean RMSE — consider a risk-aware alternative — OPEN

**Evidence.** `train_tune.py` objective returns `metrics_df['rmse'].mean()`.
Measured consequence: the tuned winner (mean RMSE 10.085) is **worse than a
shallower alternative on 3 of 12 folds**, and materially worse on fold 5
(12.888 vs 7.984).

Fold-level RMSE for the selected config ranges from 6.67 to 20.26 — a 3x spread.

**Options.**

| Objective | Behaviour |
|---|---|
| `rmse.mean()` (current) | best typical performance |
| `rmse.max()` | minimax, best worst case |
| `rmse.mean() + lambda * rmse.std()` | penalises inconsistency |
| `rmse.median()` | ignores outlier folds |

Given the August fold sits at more than double the typical RMSE, a risk-averse
consumer may prefer `mean + std`. **Change to make:** expose the objective as a
config key (`models.optuna.objective`) rather than hardcoding the mean.

---

## 9. Hyperparameters are tuned and reported on the same folds — OPEN

Optuna minimises mean fold RMSE over the 12 folds, and the leaderboard then
reports mean fold MAE over **those same 12 folds**. That is a mild optimistic
bias: the reported score has had hyperparameters fitted to it.

Compounding it, consecutive folds share ~700 of 731 training days, so fold scores
are strongly correlated and the effective sample size is well below 12.

**Change to make.** Nested cross-validation — an inner loop for tuning, an outer
loop for reporting. At minimum, hold out the final 2 folds from tuning and report
on them separately.

**Related:** there is no significance test between models. Mean MAE 8.68 vs 11.74
across 12 overlapping folds is not established as statistically significant. The
field standard is the **Diebold-Mariano test** (Lago et al. 2021); roughly half a
day of work.

---
---

## 10. Diebold-Mariano test run — leaderboard ordering IS significant — MEASURED

Half of finding 9 is now resolved. DM test on `results/backtest_predictions.csv`,
absolute-error loss, Newey-West HAC variance (lag 5, data-driven), n = 367 daily
forecasts:

| Comparison | Mean MAE gap | DM stat | p-value |
|---|---|---|---|
| ElasticNet vs XGBoost | 3.044 | 5.419 | < 0.0001 |
| Baseline_Seasonal vs XGBoost | 8.322 | 8.376 | < 0.0001 |
| Baseline_Seasonal vs ElasticNet | 5.278 | 6.610 | < 0.0001 |

All three pairwise orderings are significant at any conventional level. The
leaderboard ranking is established, not merely observed.

**Two caveats.**
1. Tested at the daily level (367 obs), not the fold level (12 obs). Daily errors
   within a month are autocorrelated; HAC absorbs some of this, but a fold-level
   test would have far less power. State the result as "significant across 367
   daily forecasts".
2. **DM does not address selection bias.** It takes forecasts as given and cannot
   know that XGBoost's hyperparameters were tuned on these same folds. The
   ranking is real; the absolute 8.68 MAE is still optimistic. Both are true.

**Change to make.** Promote this into `train_pipeline.py` as a logged artifact so
every training run reports pairwise significance alongside the leaderboard.
Remaining half of finding 9 (nested CV) is still open.

---

## 11. Selection bias measured — approximately +0.5 MAE — MEASURED

**Evidence.** 15-config random search, tuning on folds 1-8, reporting on folds 9-12:

| Quantity | MAE |
|---|---|
| Mean of all 15 configs on tuning folds | 10.457 |
| Best config's score on tuning folds (what you'd publish) | 9.491 |
| That same config on unseen folds | 9.959 |
| Mean of all 15 configs on unseen folds | 7.421 |

**Optimistic bias: +0.47 MAE** from only 15 configs. The production run used 50,
where the bias would be larger.

More concerning: the tuning-winner (9.959 on unseen) was *worse than the average
config* (7.421 on unseen). Folds 9-12 are easier overall, which explains the level
shift, but the ranking failed to transfer at all -- consistent with hyperparameter
selection fitting fold-specific noise given only 8 heavily-overlapping tuning
folds.

Caveat: 15 configs, single seed, 8 tuning folds. Direction is clear; magnitude is
not precisely estimated.

---

## 12. `extended_summer` is a deployment-matched schedule, not an oversight — CONTEXT

The active schedule covers months 5-9; the holdout is July 2020. The validation
regime was deliberately matched to the deployment period, which is defensible.

**But it bounds the claim:** 8.68 MAE is a *warm-season* number, not "the model's
accuracy". State it that way.

**Why winter would likely be materially worse -- structural, not incidental.**
Summer sits on the flat part of the merit-order curve: low residual demand, cheap
nuclear and renewables set the price, so price is insensitive to residual demand.
This is confirmed by the §5 experiment -- replacing simulated exogenous inputs
with *actual* ones changed MAE by only -0.03.

Winter sits on the steep part: high residual demand, gas and coal set the price,
and France imports heavily during cold snaps. Price then becomes a function of
fuel and carbon costs, and cross-border prices -- **none of which are in the
dataset.** Available columns are only nuclear, demand, price and four city
temperatures.

**Implication.** Running `twelve_month_span` would likely *reveal* this gap rather
than close it. That is a reason to run it (to size the problem honestly), but the
fix is the missing feature families in `ASSESSMENT.md` section 3.4, not a schedule
change.

---
---
---

## 13. MLflow bakes absolute artifact paths — broke the container — FIXED

**Symptom.** `docker compose up api` started, and `/health` returned 503 with
`Failed to load model ...: No such artifact: ''`.

**Cause.** MLflow records `artifact_location` on the experiment at *creation*
time and stores absolute paths thereafter. The experiment was created with
`/home/sarthakgupta/quantitave_forecasting/mlartifacts`, which does not exist
inside a container.

```
mlflow.db experiments table:
  french_spot_price_forecasting -> /home/sarthakgupta/quantitave_forecasting/mlartifacts
container filesystem:
  /home/sarthakgupta -> No such file or directory
  /app/mlartifacts   -> exists, but MLflow never looks there
```

**Fix applied.** `config_loader._resolve_artifact_location()` is now
environment-aware. With `ENV=production` and a bucket set it returns
`s3://<bucket>/mlflow-artifacts` — which resolves identically from any machine,
any user, with no bind mount. Locally it still resolves to a project-root path.

**Residual limitation.** MLflow cannot retroactively change an existing
experiment's artifact location. The current experiment stays pinned to the local
path; the S3 location applies to experiments created under `ENV=production`.
Switching the existing one requires a new experiment name.

---

## 14. Container/host UID mismatch on a bind-mounted artifact store — WORKED AROUND

**Symptom.** After mounting artifacts at the recorded host path, `/health`
returned 503 with
`[Errno 13] Permission denied: .../artifacts/registered_model_meta`.

**Cause.** Two facts combined:
- host uid **1001**, container `app` user uid **1000**
- **MLflow WRITES `registered_model_meta` into the artifact directory** when
  loading a model by `models:/` URI — so read-only permissions are insufficient

**Fix applied (local only).** `docker-compose.yml` now passes
`user: "${HOST_UID:-1000}:${HOST_GID:-1000}"` and mounts artifacts at
`${HOST_PROJECT_DIR}`. Note `UID` is readonly in bash, hence the `HOST_` prefix.

**The real fix is finding 13.** With artifacts on S3 there is no bind mount, no
uid mapping and no local write, so both workarounds disappear. They exist only
to make the local-artifact-store path usable from a container.

**Worth noting the failure mode behaved as designed:** the API started
*unhealthy* and reported the exact missing path, rather than crash-looping. That
is the deliberate choice documented in `INTERNALS.md` section 11.

---

## 15. Docker image was 2.65 GB — reduced to 1.64 GB — FIXED

**Evidence.** `site-packages` measured 1.7 GB. Top offender:
**`nvidia-nccl-cu13` at 288 MB**, pulled in transitively by `xgboost` but used
only for multi-GPU collective operations. Verified that XGBoost trains normally
without it (`tree_method="hist"`, CPU).

**Fix applied.** In the *same* Dockerfile layer as the install (uninstalling in a
later layer leaves the bytes in the image):

```
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y nvidia-nccl-cu13 \
 && find .../site-packages -name "tests" -type d -prune -exec rm -rf {} + \
 && find .../site-packages -name "*.pyc" -delete
```

**Result:** image **2.65 GB -> 1.64 GB**; site-packages **1.7 GB -> 1011 MB**.
Only an 8 KB empty `nvidia/` namespace directory remains. Predictions unchanged
and the healthcheck still reports healthy.

**Remaining opportunity — OPEN.** The API image still carries training-only
dependencies:

| Package | Size | Needed by | Needed by the API? |
|---|---|---|---|
| `llvmlite` | 171 MB | `numba` <- `shap` | no |
| `plotly` | 39 MB | Streamlit UI | no |
| `streamlit` | 30 MB | UI service | no |
| `statsmodels` | ~30 MB | notebook EDA only | no |
| `optuna` | small | tuning | no |

Splitting `requirements.txt` into base / api / train would remove roughly
270 MB more from the serving image. This is the per-service-image
recommendation in `MLOPS.md` step 10, and it matters for the AWS free tier,
where `t3.micro` ships an 8 GiB EBS volume by default.

---

# Reference note — statistical significance testing for forecasts

Not a finding. Kept here as the conceptual reference behind findings 9, 10 and 11.

## The question it answers

A leaderboard difference is an *estimate* measured on a finite sample. A
significance test asks: **is this difference larger than the noise in my
measurement?** With fold MAEs here ranging 5.2 to 19.0, fold-to-fold variation is
large, so the question is substantive rather than ceremonial.

## The machinery

1. **Null hypothesis (H0)** — assume the boring explanation: the two models are
   equally accurate and the observed gap is chance.
2. **Test statistic** — roughly *observed difference / uncertainty in that
   difference*. It measures the gap in "noise units".
3. **p-value** — if H0 were true, how often would a gap this large appear by
   chance?
4. **Decision** — a small p (conventionally < 0.05) means the gap is hard to
   explain as luck, so H0 is rejected.

## Why not a plain t-test

A t-test assumes independent observations. Forecast errors are **autocorrelated** —
over-predicting on the 14th makes over-predicting on the 15th likely. Ignoring
that understates the variance and manufactures false confidence.

## Diebold-Mariano

The standard test for comparing two forecast series (Diebold & Mariano 1995; the
reference implementation for electricity price forecasting is Lago, De Ridder &
De Schutter 2021).

1. Per period, compute each model's loss: `L_A(t)`, `L_B(t)` — absolute error for
   an MAE comparison, squared error for RMSE.
2. Form the loss differential `d(t) = L_A(t) - L_B(t)`.
3. Test `mean(d) = 0` using a **HAC / Newey-West** long-run variance estimate,
   which corrects for autocorrelation up to a chosen lag.
4. The statistic is asymptotically standard normal; sign indicates which model
   wins.

## Two traps

- **p < 0.05 is a convention, not a law.** It means "would occur by chance less
  than 1 in 20 times". Nothing more.
- **Statistical significance is not practical significance.** With enough data a
  0.01 MAE gap becomes "significant" and stays worthless. Always report the
  **effect size** beside the p-value. Here the effect is 3.04 EUR/MWh on a ~40
  EUR/MWh price level — economically meaningful as well as statistically real.

## What DM does NOT do

It takes the forecasts as given. It cannot know that one model's hyperparameters
were tuned on the same folds being tested. **Significance and selection bias are
independent problems** (findings 10 and 11): the ranking can be real while the
absolute number is still optimistic.

## Reproducing it here

Inputs: `results/backtest_predictions.csv`, which carries `model`,
`actual_price`, `predicted_price` per date. Pivot absolute error by model, then
apply the DM statistic pairwise with a Newey-West lag of
`floor(4 * (n/100)^(2/9))`. Current result: all three pairwise orderings
significant at p < 0.0001 across n = 367 daily forecasts.

**Intended home:** `src/analysis/evaluate.py` as a `pairwise_significance()`
function, logged by `train_pipeline.py` as `significance.csv` (finding 10).

---

## 16. `optuna` was imported at module scope in `registry.py` — FIXED

**Found by** adding `tests/test_imports.py` and running it inside the slim
`:serve` image, not by running the pipeline.

`src/models/registry.py` imported `optuna` at module level. `optuna` is a
*training* dependency, deliberately excluded from `requirements-api.txt` — but
`registry` is on the *serving* path, because `NaiveForecaster` is defined there
and unpickling a `Baseline_Seasonal` champion imports the module.

**Consequence.** Had the seasonal baseline ever won model selection, the
registered artifact would have been **unloadable by the serving image**. The
failure was latent: XGBoost won, so it never fired. This is the exact class of
bug the requirements split creates and the slim CI job exists to catch.

**Fix applied.** `optuna` appears only in type positions (the `ParamSpace` alias
and two `param_space` signatures), so:

```python
from __future__ import annotations   # annotations stay strings

if TYPE_CHECKING:
    import optuna

ParamSpace = Callable[["optuna.trial.Trial"], dict[str, Any]]
```

The search-space callables are invoked only during tuning, where optuna is
installed. Verified: the registry builds and both search spaces still produce
parameters under a live Optuna trial, and the suite passes on the slim set
(48 passed, 5 skipped — the skips being the training and UI guards).

**Open follow-up.** The same reasoning applies one level up: `requirements-ui.txt`
inherits `requirements-base.txt`, which pins full `mlflow` — and `mlflow` is
what drags `matplotlib` and `fastapi` into the UI image. Nothing under `src/ui/`
imports mlflow at all. Moving the UI to `mlflow-skinny`, or dropping mlflow from
its set entirely, should take a few hundred MB off the 1.34 GB `:ui` image.
Measured, not yet acted on.

---

## 17. A smoke test can be satisfied by the wrong process — FIXED

**Symptom.** The container smoke step reported `HTTP 200`, `model_loaded: true`,
and a real `models:/french_spot_price_forecaster/1` — from a container whose
MLflow registry was demonstrably empty (`search_registered_models()` returned
`[]`).

**Cause.** A leftover host `uvicorn` was bound to `127.0.0.1:8000`. A host
process on the loopback address takes precedence over Docker's `0.0.0.0:8000`
port proxy, so `curl 127.0.0.1:8000/health` never reached the container.

**Fix applied.** The CI smoke step publishes on `18000:8000`. Re-run against the
container, the honest result appeared: `HTTP 503`, `model_loaded: false`, with
the actionable message *"No registered versions found... Run
`python -m src.pipelines.train_pipeline`"* — which is the designed behaviour.

**Generalisation worth keeping.** A green check that another process could have
produced is not evidence. The two ways this hides: a port already bound by
something else, and a proxy answering on the service's behalf (this project has
already hit the second — `requests` honouring `HTTP_PROXY` for loopback, which
is why `trust_env = False` is set in `src/ui/api_client.py` and `--noproxy '*'`
appears in every `curl` here).

---

## Summary of pending work

| # | Finding | Type | Effort |
|---|---|---|---|
| 1 | `is_lockdown` dead in 10/12 folds | code + config | small |
| 2 | `decorrelated` conditioning measured (10^16 -> 42.6) | doc update | trivial |
| 3 | Add `residual_plus_nuclear` variant + benchmark all four | code + experiment | small |
| 4 | Quote measured weather error instead of asserting compounding | doc update | trivial |
| 5 | Add a winter-fold error-propagation check | experiment | small |
| 6 | Fix arcsinh rationale (robustness, not closed form) | doc update | trivial |
| 7 | Two-tier tuning for the 7 frozen cascade hyperparameters | code + experiment | medium |
| 8 | Make the Optuna objective configurable (mean vs risk-aware) | code | small |
| 9 | Nested CV (DM half now done, see 10) | experiment | medium |
| 10 | Promote DM test into the training pipeline as a logged artifact | code | small |
| 11 | Selection bias measured at ~+0.5 MAE; report it as a caveat | doc update | trivial |
| 12 | Restate 8.68 MAE as a warm-season figure everywhere | doc update | trivial |
| 13 | MLflow absolute artifact paths — fixed via S3-aware location | done | — |
| 14 | Container UID mismatch — worked around; S3 removes the need | done | — |
| 15 | Split requirements per service (~270 MB more off the API image) | code | small |
| 16 | `optuna` module-scope import in `registry.py` — fixed | done | — |
| 16b | Move the UI image off full `mlflow` (`mlflow-skinny`) — ~300 MB | code | small |
| 17 | Smoke test hit a host process, not the container — fixed (port 18000) | done | — |
