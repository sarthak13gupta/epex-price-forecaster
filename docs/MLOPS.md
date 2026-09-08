# MLOps in This Project

A step-by-step reference for the machine-learning operations lifecycle as it
applies here. For each step:

1. **What it is** — the generic definition, independent of this project
2. **Why it exists** — the failure it prevents
3. **In this project** — the concrete implementation, with honest status
4. **Why this way** — the trade-off that was accepted
5. **On AWS** — the migration path

Companion to `DESIGN.md` (architecture), `INTERNALS.md` (code walkthrough),
`ASSESSMENT.md` (critique and roadmap), `CORRECTIONS.md` (measured findings),
`AWS_S3_EC2.md` (the cloud services) and `DOCKER.md` (containerisation).

---

## Maturity summary

| # | Step | Status | Tooling here |
|---|---|---|---|
| 1 | Data ingestion & access | 🟡 Partial | `data_loader.py`, `ENV` toggle, S3 path untested |
| 2 | Data validation | 🟢 Built | pandera, three schemas |
| 3 | Data & feature versioning | 🔴 Absent | — |
| 4 | Feature engineering | 🟢 Built | `build_features.py`, `ExogenousCascade` |
| 5 | Experiment tracking | 🟢 Built | MLflow, nested runs, SQLite backend |
| 6 | Hyperparameter optimisation | 🟢 Built | Optuna TPE + stagnation early stopping |
| 7 | Evaluation & release gating | 🟡 Partial | Walk-forward + leaderboard; **no gate** |
| 8 | Model packaging | 🟢 Built | `mlflow.pyfunc` + `code_paths` |
| 9 | Model registry & promotion | 🟡 Partial | Registry v1; **no aliases, no promotion rule** |
| 10 | Serving | 🟡 Partial | **FastAPI built** (4 endpoints, tested); Streamlit not written |
| 11 | Monitoring & drift | 🔴 Absent | — |
| 12 | Continuous training | 🔴 Absent | Manual invocation only |
| 13 | CI/CD | 🔴 Absent | No tests, no pipeline |
| 14 | Reproducibility & lineage | 🟡 Partial | Config + seeds + pinned deps; no data version |
| 15 | Explainability & governance | 🟢 Built | SHAP, dual explainer, logged artifacts |

Six built, four partial, five absent. That distribution is normal for a project
that has prioritised the modelling core; the absent items are concentrated in
operations, which is exactly what `ASSESSMENT.md` Tier 1 addresses.

---

# Phase A — Data

## Step 1. Data ingestion and access

**What it is.** Getting raw data from wherever it lives into the process that
needs it, with the *source* being a deployment decision rather than a code
decision.

**Why it exists.** Code that hardcodes `pd.read_csv("/home/me/data.csv")` cannot
run anywhere except that machine. The generic requirement is that the same
training code runs against a local sample during development and against the
real store in production, with no code change.

**In this project.** `src/data/data_loader.py` exposes one function,
`load_raw_dataset(dataset_type)`, which switches on an `ENV` environment
variable:

- `ENV=local` -> `_load_from_disk()` reads a path from config
- `ENV=production` -> `_load_from_s3()` streams the object body through
  `io.BytesIO` straight into `read_csv`

Every downstream module receives a DataFrame and never learns which branch ran.

**Why this way.**

- *Pro:* one seam, easily swapped; no other module knows about storage.
- *Pro:* streaming from S3 into memory avoids leaving files on an ephemeral
  container filesystem.
- *Con:* the S3 branch has never been executed. It is written but unverified.
- *Con:* it passes `aws_access_key_id` explicitly, which forces static
  credentials to exist.

**On AWS.**

1. **Drop the explicit credentials.** On EC2, omit `aws_access_key_id` /
   `aws_secret_access_key` from the `boto3.client` call and let boto3 pick up the
   **instance role**. Static keys then have no production role at all.
   Scope the role to `s3:GetObject` / `s3:PutObject` on one bucket.
2. **Partition raw arrivals by date:** `raw/epex/dt=YYYY-MM-DD/`. A daily feed
   becomes append-only, so a bad file damages one partition instead of
   overwriting history.
3. **Longer term:** if the feed becomes real-time, `Kinesis Firehose` -> S3, or
   `AWS Glue`/`Athena` over the partitioned prefixes so you can query raw data
   without loading it.

---

## Step 2. Data validation

**What it is.** Asserting, in code, the properties incoming data must have —
columns, types, nullability, ranges, uniqueness — and failing loudly at the
boundary when they are violated.

**Why it exists.** Data changes without telling you: a vendor renames a column,
a units change halves a series, an upstream job emits nulls. Without validation
these become a *quietly worse model* rather than an error. The whole point is to
convert silent corruption into a loud, local failure.

**In this project.** Three pandera `DataFrameModel` contracts in
`src/data/schema.py`, applied at three different boundaries:

| Schema | Boundary | Nullability rule |
|---|---|---|
| `RawDataSchema` | after ingestion | everything nullable — gaps expected |
| `ProcessedDataSchema` | after cleaning | everything **non-null** — gates training |
| `FutureExogenousSchema` | inference input | only nuclear non-null; rest `Optional` |

**Why this way.** The three-schema split is the design decision, and it was
forced. A single strict schema rejected the deliberately-empty forecast file and
made the inference path impossible. The training path demands completeness; the
inference path demands that *exactly one* column be complete and the rest be
absent. Those are different contracts, so they are different objects.

- *Pro:* each boundary asserts what is actually true there; failures are local.
- *Pro:* `coerce=True` normalises CSV integer columns to float without callers
  caring; `strict=False` lets engineered features accumulate on a frame.
- *Con:* the column list is repeated three times — drift risk when adding a
  column.
- *Con:* **structure only, no value ranges.** A price of 10,000 EUR/MWh or a
  temperature of 200 degC passes today.

**On AWS / future.**

1. **Add range and cross-column checks** in pandera — cheap and closes the
   biggest current hole. `Field(ge=-500, le=4000)` on price;
   `DEMAND > NUCLEAR` sanity assertions.
2. **AWS Glue Data Quality** or **Deequ** for validation *at rest*, before the
   training job starts, so a bad partition is quarantined rather than trained on.
3. **Fail the pipeline, alert via SNS.** A validation failure should page someone,
   not just raise in a log nobody reads.

---

## Step 3. Data and feature versioning

**What it is.** Being able to say "model version 7 was trained on *this exact*
data" and reproduce it. Code versioning (git) is universal; data versioning is
the frequently-missing half.

**Why it exists.** Without it, "the model got worse" is unanswerable — you cannot
tell whether the code changed, the data changed, or both. Reproducing a
six-month-old result requires the data as it was then, not as it is now.

**In this project.** 🔴 **Absent.** The raw CSVs are static files. `mlflow` records
parameters and metrics but not a dataset identity. `data/processed/` is
gitignored and regenerated.

**Why this is currently acceptable.** The input data has not changed since the
project began — it is a fixed historical extract. Versioning solves a problem
that does not yet exist here. **It stops being acceptable the moment a daily feed
is connected**, which is precisely what the AWS work introduces.

**On AWS.**

1. **S3 bucket versioning + date partitions** gives you most of this almost for
   free. Every object has a version id; `dt=` partitions make the arrival date
   explicit. Turn versioning on *before* you need it — it cannot be applied
   retroactively to objects already overwritten.
2. **`mlflow.log_input()` with a dataset digest.** MLflow supports logging a
   dataset (source URI + hash) against a run. That ties run -> exact data.
3. **DVC or LakeFS** if you need git-like branching over data. Probably overkill
   here; S3 versioning plus a logged digest covers the audit requirement.
4. **A feature store (SageMaker Feature Store / Feast)** becomes worthwhile only
   when multiple models share features or when online and offline features must
   provably match. With one model and a bundled cascade, it would add
   infrastructure without removing a real risk.

---

# Phase B — Development

## Step 4. Feature engineering and the train/serve boundary

**What it is.** Transforming raw columns into model inputs, and guaranteeing the
transformation is **identical** in training and serving.

**Why it exists.** *Train/serve skew* — the feature computed at training time
differing from the one computed at serving time — is among the most common and
hardest-to-diagnose production ML failures. It produces a model that scores well
offline and underperforms live, with no error anywhere.

**In this project.** Two deliberate layers:

- **Static features** (`build_static_features`) depend only on the index or on
  columns already present, so they are computed once over all history: calendar
  flags, residual demand.
- **Dynamic features** (`ExogenousCascade`) depend on a *training window* and are
  therefore fitted per fold: weather forecast, thermal state, degree days,
  simulated demand.

Skew is prevented structurally in two ways:

1. **One implementation.** `forecast()` calls the *same*
   `engineer_calendar_features` the training path calls. There is no second
   implementation to drift.
2. **Fitted state is persisted, not recomputed.** The day-of-year climatology
   (`t_norm_reference`), weather coefficients and load model are pickled into the
   artifact. Recomputing them at serve time would silently change the features.

**Why this way.**

- *Pro:* the static/dynamic split is what prevents leakage — computing the
  climatology over all history and then testing on part of it means the test
  period informed its own baseline.
- *Pro:* the `_add_thermal_features` helper encodes stage ordering structurally,
  so `fit` and `simulate` cannot disagree.
- *Con:* the cascade's sub-models are not scikit-learn transformers, so they do
  not compose into a `Pipeline` and persistence had to be hand-rolled.
- *Con:* seven cascade hyperparameters are frozen at conventional values and
  never tuned (see `CORRECTIONS.md` finding 7).

**On AWS / future.** The honest answer is that a feature store is *not* the next
step here — the bundled-cascade design already guarantees train/serve parity for
a single model. What would help:

1. **Property-based tests** asserting parity: for a given date range, the features
   produced by the training path and by `forecast()` must be identical.
2. **SageMaker Processing** jobs if feature computation outgrows one machine.
3. A feature store **only** when a second model wants the same features.

---

## Step 5. Experiment tracking

**What it is.** Recording, for every training run, the parameters, metrics,
artifacts and code version that produced it — in a queryable store rather than a
spreadsheet.

**Why it exists.** Without it you cannot answer: which configuration produced
the best result; is the new model better than last quarter's; where is that model
file. People substitute filenames like `model_v3_final_ACTUAL.pkl` and a
spreadsheet abandoned on day three.

**In this project.** 🟢 MLflow, with a deliberate **parent/child run structure**:

```
run: training_20260828_154210                  <- parent
  params:  active_schedule, n_folds, train_window_days, warmup_days,
           feature_set, n_features, selection_metric, target,
           hdd_cdd_reference, best_model
  metrics: best_mae, best_rmse, best_mae_std, best_worst_fold_mae,
           holdout_mean_predicted_price
  artifacts: leaderboard.csv, metrics_by_regime.csv, backtest_predictions.csv,
             model_metadata.json, residuals_*.png, backtest_predictions_*.png,
             holdout_forecast.csv/.png, shap_*.png, shap_importance.csv
  model:   price_forecaster (pyfunc) -> registry v1

  |- nested run: Baseline_Seasonal
  |    metrics: mae, rmse, mae_std, worst_fold_mae, fold_01_mae .. fold_12_mae
  |- nested run: ElasticNet
  |    params: regressor__model__alpha, regressor__model__l1_ratio
  |    metrics: + tuning_best_rmse, tuning_n_trials
  |- nested run: XGBoost
       params: max_depth, n_estimators, learning_rate, subsample, colsample
       metrics: + tuning_best_rmse, tuning_n_trials
```

**Why this way.**

- **Nested rather than flat**, because each model has its own hyperparameters and
  its own twelve fold metrics. Flattened, the parameter names would collide
  (`alpha` means different things to different models) and metrics would need
  manual prefixing.
- **Leaderboard metrics re-logged on the parent with a `best_` prefix**, so the
  parent alone answers "how good was this run" and runs are sortable in the UI.
- **Per-fold metrics logged individually** (`fold_01_mae` ...), making
  regime-level regression discoverable across runs. Fold *labels* live in the CSV
  because MLflow rejects metric names containing spaces, slashes and parentheses —
  which every regime label has.
- **SQLite backend, not the file store.** Two reasons, one fatal: MLflow 3.x puts
  the filesystem backend in maintenance mode, and **the file store has never
  supported the model registry.** A database backend is a hard requirement for
  registry features, not a preference.
- *Con:* SQLite is single-writer. Fine while one training job writes; contended
  the moment the API, the UI and a training run share it.
- *Con:* no dataset logging, so lineage stops at parameters.

**On AWS.**

1. **RDS Postgres (`db.t4g.micro`) as the backend store**, with
   `--default-artifact-root s3://.../mlflow-artifacts/`. This is the single most
   important MLflow change for deployment — it makes the registry safe for
   concurrent access and puts artifacts on durable storage.
2. **Run the tracking server on EC2 behind Nginx**, never exposing `:5000`.
   MLflow has no authentication of its own: anything that reaches that port can
   delete your registry.
3. **`mlflow.log_input()`** to close the lineage gap.
4. **SageMaker Experiments** is the managed alternative. It couples you to
   SageMaker; self-hosted MLflow keeps the stack portable. For a single-model
   project, self-hosted is the better trade.

---

## Step 6. Hyperparameter optimisation

**What it is.** Searching the hyperparameter space for the configuration that
minimises a validation objective, rather than accepting defaults or hand-tuning.

**Why it exists.** Defaults are rarely optimal, and manual tuning does not scale
past two or three parameters. The generic requirement is a *systematic,
reproducible, recorded* search.

**In this project.** 🟢 Optuna with a TPE sampler:

- **Objective:** mean fold RMSE across all 12 walk-forward folds —
  `metrics_df['rmse'].mean()`. One trial = 12 model fits.
- **Sampler:** `TPESampler(seed=random_state)` — Bayesian, more
  sample-efficient than random search, and seeded for reproducibility.
- **Early stopping:** a custom `StudyEarlyStoppingCallback` stops the study after
  20 trials without improvement. Measured effect: the XGBoost study finished at
  **19 of 50 trials**, cutting tuning time roughly 60% with no loss.
- **Search space:** defined as a callable per model in `registry.py`, with bounds
  read from config.

**Why this way.**

- **Tuning on the same objective the leaderboard reports** — otherwise the chosen
  hyperparameters are optimal for something nobody measures.
- **RMSE for tuning, MAE for selection** is deliberate: RMSE's quadratic penalty
  pushes the search away from large errors, MAE is the interpretable number for
  choosing between finished models. Both are reported so the choice is auditable.
- **Returns an *unfitted* estimator.** Tuning chooses hyperparameters;
  `PriceForecaster.fit` decides the final training window. Returning a fitted
  model would force tuning to also decide what data to fit on.
- **`GridSearchCV` / `RandomizedSearchCV` could not be used at all** — neither
  understands this custom fold structure.
- *Con:* the objective is hardcoded to the mean. A risk-averse consumer might
  prefer `mean + std` or minimax — the selected config is measurably worse than a
  shallower alternative on 3 of 12 folds (`CORRECTIONS.md` finding 8).
- *Con:* the seven cascade hyperparameters are excluded, which is what makes fold
  caching valid but leaves them untested (finding 7).

**The efficiency decision worth knowing.** The cascade depends only on the
training window, never on the price model's hyperparameters. The notebook
nonetheless refit it inside every trial: 50 x 12 = 600 cascade fits for
byte-identical results. Splitting `prepare_folds` from `evaluate_folds` reduced
that to **12**. It also guarantees every model is scored on identical design
matrices, so leaderboard comparisons cannot be confounded by fold construction.

**On AWS / future.**

1. **Optuna with an RDS storage backend** enables *distributed* studies — several
   EC2 workers pulling trials from one study. Straightforward win if tuning grows.
2. **SageMaker Automatic Model Tuning** is the managed option; it does not
   understand custom fold structures well, so Optuna is the better fit here.
3. **Two-tier tuning** (finding 7): tune cascade parameters against their own
   targets (thermal params vs demand error, weather params vs temperature error),
   keeping price-model caching intact.

---

# Phase C — Release

## Step 7. Model evaluation and release gating

**What it is.** Deciding whether a trained model is good enough to promote —
ideally as an automated, explicit rule rather than a human glance at a number.

**Why it exists.** Without a gate, "the pipeline ran" becomes "the model is
deployed." A regression on a subpopulation, or a model that beats the average
while failing a critical regime, ships silently.

**In this project.** 🟡 Partial. The *evaluation* is thorough:

- Walk-forward, 12 folds, one calendar month each, cascade refitted per fold.
- Leaderboard with mean MAE/RMSE, standard deviations, and **worst fold** — a
  model that is good on average and catastrophic once is not deployable, and the
  mean alone hides that.
- **Per-regime breakdown** (`summarize_by_regime`), which is what revealed both
  that XGBoost fixes the COVID collapse fold *and* that the naive baseline beats
  it during Lockdown Easing.
- A **retained naive baseline** in the registry, giving every metric a scale.
- Residual diagnostics: predicted-vs-actual, residuals over time, residual
  distribution, residuals by weekday.

The **gate is missing.** `select_best_model` picks the leaderboard leader and the
pipeline registers it. There is no rule that can reject a model.

**Why this way.** Deliberate, and defensible: the `year` finding is exactly why.
A model whose MAE improved but whose SHAP attribution is dominated by a
structural artefact should not auto-promote. A human look was the right call at
this maturity.

**On AWS / future.**

1. **Codify the gate** as explicit assertions before registration:
   - must beat `Baseline_Seasonal` by a configured margin
   - **no individual fold worse than X**
   - must not regress more than Y% against the current champion
   - top SHAP feature must not exceed Z% of total attribution (this would have
     flagged `year`)
2. **Add the Diebold-Mariano test** as a logged artifact so each run reports
   pairwise significance, not just point differences (`CORRECTIONS.md` finding 10
   — measured, pending promotion into the pipeline).
3. **Nested cross-validation** to remove the ~+0.5 MAE selection bias from tuning
   and reporting on the same folds (finding 11).
4. On AWS this is a **Step Functions** state machine: train -> evaluate ->
   *choice state* -> register or alert via SNS.

---

## Step 8. Model packaging

**What it is.** Serialising a trained model together with everything needed to
run it — dependencies, input/output schema, and any auxiliary fitted state — in
a format a consumer can load without knowing the internals.

**Why it exists.** A bare `pickle` records the object but not the Python version,
the library versions, or the expected input shape. Six months later it may not
deserialise, and nothing tells you what it expects.

**In this project.** 🟢 `mlflow.pyfunc.log_model` with a composite object:

```python
mlflow.pyfunc.log_model(
    name="price_forecaster",
    python_model=PriceForecasterModel(forecaster),   # an INSTANCE, not a class
    code_paths=["src"],
    signature=signature,
    input_example=example_input,
    registered_model_name="french_spot_price_forecaster",
)
```

**Why this way — the key design point.** "The model" here is **six pieces of
fitted state**, not one estimator:

1. `WeatherForecaster` coefficients (4 regions x 8 coefficients)
2. `t_norm_reference` day-of-year climatology (366 entries)
3. the fitted Ridge `LoadForecaster` (15 features)
4. the price model **including** the fitted arcsinh `median_` / `mad_`
5. the exact feature *ordering* (22 names)
6. the 14-day warm-up tail plus training-window bounds

`mlflow.sklearn` would not work — the object is not a scikit-learn estimator.
`pyfunc` is MLflow's generic interface: anything with a `predict()` method.

**Four of those six fail *silently* if not persisted:**

| Component | Failure mode if lost |
|---|---|
| Weather coefficients | Loud — crashes |
| `t_norm` climatology | **Silent** — recomputed baseline causes train/serve skew |
| LoadForecaster | Loud |
| arcsinh `median_`/`mad_` | **Silent** — wrong inverse transform, plausible numbers |
| Feature ordering | **Silent** with numpy arrays |
| Warm-up tail | **Silent** — cold EWM, first week wrong |

That asymmetry is the real argument for bundling: the dangerous failures are the
ones that return a confident number, not the ones that crash.

- *Pro:* `code_paths=["src"]` ships the source with the artifact, so a loading
  process needs nothing on its `PYTHONPATH`. Verified: loads in a fresh
  interpreter from an unrelated working directory.
- *Pro:* `signature` lets MLflow reject malformed requests at serve time;
  `input_example` is executable documentation.
- *Con:* cloudpickle of an arbitrary object is fragile across library upgrades —
  a `pandas` or `xgboost` major bump can break deserialisation.
- *Con:* `code_paths` prepends the artifact's `src/` to `sys.path`, which shadows
  the real package. This is what broke `PROJECT_ROOT` (see `INTERNALS.md` §2).

**On AWS / future.**

1. **Pin the environment tightly.** MLflow already writes `python_env.yaml`,
   `conda.yaml` and `requirements.txt` into the artifact. Build the serving image
   *from* those files so serving and training environments cannot diverge.
2. **Models-from-code** (MLflow's newer pattern) avoids cloudpickling an instance
   by logging a script that reconstructs the model. More robust across upgrades;
   worth migrating to.
3. **ECR** for the serving image; S3 for the artifact.

---

## Step 9. Model registry and promotion

**What it is.** A versioned catalogue of models with a mechanism for saying
"*this* version is live" independently of which run produced it.

**Why it exists.** Deployment must be decoupled from training. If the API
references a run id, promoting a model means a code change and a rebuild. If it
references an alias, promotion is a metadata change plus a restart — and
rollback is the same operation in reverse.

**In this project.** 🟡 Partial.

- Registered as `french_spot_price_forecaster`, currently **version 1**.
- Every version links back to the run that produced it, and therefore to its
  parameters, metrics, SHAP plots and fold-level results.
- **No aliases are set.** Nothing marks a version as champion.
- **No automatic promotion**, which is deliberate — see step 7.

**Why this way.** Registering without promoting is the right default at this
maturity: the pipeline records a candidate, a human decides. The `year` finding
is the concrete justification.

**On AWS / future.**

1. **Use an alias, not a version number.** The API should load
   `models:/french_spot_price_forecaster@champion`. Promotion becomes
   `set_registered_model_alias`, and rollback is the same call pointing back.
2. **Add a `challenger` alias** and shadow-deploy: route a copy of live traffic to
   the challenger, log both predictions, compare on settled prices. No user
   impact, real comparison.
3. **Require the registry backend to be RDS**, not SQLite, before relying on
   aliases operationally.

---

# Phase D — Operations

## Step 10. Serving

**What it is.** Running a long-lived process that holds the model in memory and
answers prediction requests over a network.

**Why it exists.** A Python function is callable only by Python, on that machine.
HTTP makes the model callable by anything — dashboards, schedulers, other
services, other languages.

**In this project.** 🟡 **Partially built.** The FastAPI backend exists and is
tested (`src/api/`, documented in `INTERNALS.md` section 11); Streamlit is not
yet written. The shape:

- **FastAPI** owns inference. Loads the bundle **once at startup**, holds it in
  memory, answers `POST /predict`. Pydantic validates the request body — which
  matters acutely because nuclear availability is the single real input to the
  cascade, and a null slipping through would produce `NaN` temperatures ->
  `NaN` demand -> a `NaN` price served confidently.
- **Streamlit** owns presentation, holds no model, calls the API over HTTP.
- Endpoints: `/health`, `/model-info` (returns `model_info()`), `/predict`,
  `/backtest-metrics`.

**Why this shape.**

- **Fitting moved out of the request path.** The notebook's `forecast_holdout`
  refits on every call. Behind HTTP that gives a multi-second training loop in a
  request handler, non-deterministic responses, no reproducibility and no
  rollback target.
- **Loading once at startup** makes MLflow a **startup dependency, not a runtime
  dependency**. A running API survives MLflow being down; a *restarting* one does
  not. Worth stating precisely — it is better than runtime coupling but is not
  zero coupling.
- **UI separate from API** gives failure isolation, independent scaling, and keeps
  the endpoint reusable by non-human clients.

**On AWS.**

1. **Split the container images** — `Dockerfile.api`, `Dockerfile.ui`,
   `Dockerfile.train` — so the UI image carries no XGBoost and the API image
   carries no Optuna or SHAP. Set `PYTHONUNBUFFERED=1` in all three or `print`
   output sits in a block buffer and CloudWatch shows nothing until exit.
2. **Single `t3.large` EC2 with docker compose** is right for this workload. The
   binding constraint is not serving — a forecast is a Ridge predict plus an
   XGBoost predict over 31 rows, single-digit milliseconds — it is the Optuna
   sweep. If a nightly retrain runs long, move *training* to a scheduled
   `c7i.xlarge` that terminates on completion.
3. **Nginx terminating TLS; only :443 open.** Ports 8000/8501/5000 stay on the
   Docker network.
4. **A `/health` endpoint that asserts the model actually loaded**, not one that
   returns 200 unconditionally. A service reporting healthy with no model is worse
   than one plainly down.
5. **Mitigate the startup dependency:** bake the model into the image at build
   time, or cache the artifact on an EBS volume with a fallback. Containers
   restart constantly — deploys, crashes, host maintenance, OOM kills.
6. **Managed alternatives:** `SageMaker Endpoints` handles autoscaling and blue/
   green natively but costs more and couples you to SageMaker. `ECS Fargate`
   removes instance management. For one model and one analyst audience, EC2 +
   compose is the honest choice.

---

## Step 11. Monitoring and drift detection

**What it is.** Watching, in production, whether inputs and outputs still
resemble what the model was trained on, and whether accuracy holds once ground
truth arrives.

**Why it exists.** Models degrade silently. Nothing errors when the world moves.
Three distinct things to watch:

| Type | Question | Detectable when? |
|---|---|---|
| **Data drift** | Have input distributions shifted? | Immediately |
| **Concept drift** | Has the input->output relationship changed? | Only with ground truth |
| **Performance decay** | Is accuracy falling? | When actuals settle |

**In this project.** 🔴 **Absent** entirely.

**Why this is the most consequential gap.** This project has a *specific,
predictable* drift exposure. The model relies on `year` for 11.53 EUR/MWh of mean
absolute SHAP attribution — more than every physical fundamental combined. Across
a January boundary an unseen `year` value makes the tree saturate at its highest
split. **The model will degrade at a known date, and nothing would notice.**

Second exposure: the model was validated on warm-season folds only, and winter
prices are set by fuel, carbon and imports — none of which are in the feature
set (`CORRECTIONS.md` finding 12).

**On AWS.**

1. **Write every forecast to S3** — `forecasts/dt=YYYY-MM-DD/`. This is the
   enabling step and costs nothing. It cannot be reconstructed retrospectively,
   so start now even before any monitoring exists.
2. **Join forecasts to settled prices** as they arrive; compute rolling MAE.
   **CloudWatch custom metric + alarm** when rolling 30-day MAE exceeds, say,
   1.5x backtest MAE.
3. **Input drift:** log per-request feature summaries; compare distributions to
   the training window (population stability index, or a KS test per feature).
   `SageMaker Model Monitor` does this managed; `Evidently` is the self-hosted
   option and is lighter.
4. **Attribution drift:** periodically recompute SHAP on recent predictions. A
   *change in which features matter* is an early warning that precedes accuracy
   decay.
5. **Alert on refusals.** The horizon guards raise on invalid requests — a spike
   in refusals means an upstream caller has changed behaviour.

---

## Step 12. Continuous training

**What it is.** Retraining on a schedule or a trigger, rather than when someone
remembers.

**Why it exists.** A model trained on a fixed window becomes progressively stale.
For a rolling-window design like this one, retraining is how the window actually
rolls.

**In this project.** 🔴 **Absent.** `python -m src.pipelines.train_pipeline` is
invoked by hand. Notably, the final fit already uses "the most recent 731 days",
so the *code* is retrain-ready — only the trigger is missing.

**On AWS.**

1. **EventBridge scheduled rule -> ECS task / EC2 run-task** executing the
   training container to completion, then exiting. Do not keep a trainer running.
2. **Trigger on data arrival**, not only on a clock: S3 `PutObject` event ->
   Lambda -> start training. Retrain when there is something new to learn from.
3. **Trigger on drift:** CloudWatch alarm from step 11 -> SNS -> retrain.
4. **Always gate before promoting** (step 7). Automatic retraining without a gate
   is a mechanism for automatically deploying a worse model.
5. **Step Functions** to orchestrate the whole sequence with retries, timeouts
   and a failure branch that alerts.

---

# Phase E — Cross-cutting

## Step 13. CI/CD for ML

**What it is.** Automated checks on every code change (CI) and automated
promotion of artifacts (CD). For ML it extends beyond unit tests to data
contracts and model quality.

**Why it exists.** Every "why" in `INTERNALS.md` is an assertion about behaviour;
none is currently enforced. A refactor can silently reintroduce the degree-day
ordering bug that made the migrated code non-functional.

**In this project.** 🔴 **Absent.** No `tests/`, no `.github/`, no pre-commit.

**On AWS / future.** Three tests earn their keep immediately:

1. **Stage ordering** — degree days cannot be built before national temperature.
   `engineer_degree_days` must raise when `T_lisse` is absent.
2. **Horizon guards** — a request before `train_end + 1` or beyond
   `max_horizon_days` must raise.
3. **A leakage assertion** — for every fold, no feature value may depend on data
   after that fold's `train_end`. This test is itself interview material.

Then:
- **GitHub Actions**: lint (`ruff`), type-check (`mypy`), unit tests, and a
  smoke-test pipeline run with `--skip-tuning --skip-shap --no-register` on a
  data sample.
- **Build and push images to ECR** on merge to main.
- **CodeDeploy or a compose pull** on the EC2 host for delivery.
- Dependencies are already fully pinned (34 of 34 with `==`), which is half of
  reproducible CI.

---

## Step 14. Reproducibility and lineage

**What it is.** Being able to reconstruct a past result, and to trace a
prediction back through model -> run -> code -> data.

**Why it exists.** "Why did we quote 34 EUR/MWh last July?" must be answerable
months later, both for debugging and for audit.

**In this project.** 🟡 Partial — decent, with one clear gap.

| Element | Status |
|---|---|
| Config externalised, single source | 🟢 `configs/config.yaml` |
| Seeds fixed | 🟢 `models.random_state: 42`, threaded to Optuna sampler, XGBoost, Ridge, SHAP sampling |
| Dependencies pinned | 🟢 34/34 with `==` |
| Feature ordering persisted | 🟢 in the artifact |
| Provenance in the artifact | 🟢 `model_info()` — model name, train window, backtest MAE, best params |
| Run -> parameters/metrics/artifacts | 🟢 MLflow |
| Run -> **exact data** | 🔴 no dataset digest |
| Run -> **exact code commit** | 🟡 MLflow captures git SHA only if run inside a repo — **this project is not a git repo yet** |

**The two gaps are related and both cheap.** Initialise git, and add
`mlflow.log_input()` with a dataset hash. Together they complete the lineage
chain.

**On AWS.** S3 object versions give data identity; ECR image digests give
environment identity; git SHA gives code identity. Log all three against the run
and lineage is closed end to end.

---

## Step 15. Explainability and governance

**What it is.** Being able to say *why* a model produced a given prediction, both
globally (which features matter) and locally (why this specific number).

**Why it exists.** Three reasons: debugging, trust, and regulation. In energy
trading a forecast nobody can interrogate will not be acted on.

**In this project.** 🟢 Built, and it earned its keep.

Two complementary views in `analysis/explainability.py`:

- **Model-agnostic** — a permutation explainer over the *full* pipeline including
  the inverse arcsinh, so contributions are in **EUR/MWh**. Slow, but readable by
  a trader.
- **Model-specific** — routed to `TreeExplainer` for XGBoost or `LinearExplainer`
  for linear models, on scaled features in arcsinh space. Exact and fast, but the
  units are not economically meaningful.

Both are logged as MLflow artifacts, plus a `shap_importance.csv` ranking.

**Why two views.** They answer different questions: the agnostic one is what you
show a stakeholder, the specific one is what you use to debug. **The `year`
finding came from the agnostic view**, where 11.53 is directly readable as
EUR/MWh — that is what made the problem obvious rather than abstract.

The forecast response also returns the **simulated drivers** (`T_lisse`,
`Delta_T`, degree days, demand, residual demand) alongside the price. A forecast
that arrives with its assumptions can be interrogated; a bare number can only be
trusted or ignored.

**On AWS / future.**

1. **Serve attribution with the prediction.** A `/explain` endpoint, or SHAP
   values inline in the `/predict` response for the top few features.
2. **Track attribution over time** — attribution drift precedes accuracy decay
   (step 11).
3. **Model cards.** A generated summary per registered version: intended use,
   training window, backtest performance by regime, known limitations
   (`year` dependence, warm-season-only validation). `SageMaker Model Cards`
   does this managed; a markdown template in the artifact works as well.
4. **Add the gate on attribution concentration** (step 7) so a `year`-like
   dependence is caught automatically next time.

---

# AWS service map

Consolidated target state, mapping each step to a service:

| Step | Service | Notes |
|---|---|---|
| 1 Ingestion | **S3** + IAM instance role | `raw/epex/dt=.../`, no static keys |
| 2 Validation | pandera in-process, **Glue Data Quality** at rest | Alert via **SNS** |
| 3 Versioning | **S3 versioning** + `mlflow.log_input()` | Enable before you need it |
| 4 Features | in-process (bundled cascade) | Feature store only if a 2nd model appears |
| 5 Tracking | **MLflow on EC2** + **RDS Postgres** + S3 artifacts | Never expose :5000 |
| 6 HPO | **Optuna** + RDS storage | Enables distributed studies |
| 7 Gating | **Step Functions** choice state | Assertions before registration |
| 8 Packaging | `mlflow.pyfunc` -> S3; image -> **ECR** | Build image from artifact's env files |
| 9 Registry | MLflow registry + `@champion` alias | Requires RDS backend |
| 10 Serving | **EC2 t3.large** + docker compose + **Nginx** | Or ECS Fargate / SageMaker Endpoint |
| 11 Monitoring | **CloudWatch** metrics + alarms, `forecasts/` in S3 | Or SageMaker Model Monitor / Evidently |
| 12 Retraining | **EventBridge** -> ECS task; S3 event -> **Lambda** | Always gate before promotion |
| 13 CI/CD | **GitHub Actions** -> ECR -> CodeDeploy | Start with 3 tests |
| 14 Lineage | S3 versions + ECR digests + git SHA | Initialise git first |
| 15 Governance | Model cards in the artifact; `/explain` endpoint | Attribution-concentration gate |

**Cost note.** `t3.large` on-demand is roughly $60/month, `db.t4g.micro` around
$13, S3 for this data volume is cents. A scheduled `c7i.xlarge` that runs one
hour a night adds a few dollars. Total well under $100/month — worth stating,
because "I designed for AWS" is stronger when you can size it.

---

# Recommended order of work

From `ASSESSMENT.md` Tier 1, expressed as MLOps steps:

1. **Step 10 (serving)** — the artifact already loads and predicts; this is the
   difference between a pipeline and a system. No research risk.
2. **Step 13 (three tests + CI)** — roughly four hours, and its absence is the
   most common resume-project tell.
3. **Step 7 (gating)** — codify what a human currently does by eye.
4. **Step 11 (write `forecasts/` to S3)** — costs nothing, and the data cannot be
   reconstructed later.
5. **Step 5 (RDS backend)** — required before aliases and concurrent access are
   safe.
6. **Step 12 (scheduled retraining)** — only after the gate exists.

Deliberately last: feature stores, Kubernetes, distributed training. Each solves
a problem this project does not have, and claiming them in an interview invites
questions the codebase cannot answer.
