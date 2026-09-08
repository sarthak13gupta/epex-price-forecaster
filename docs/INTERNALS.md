# System Internals

**A file-by-file, function-by-function walkthrough: what each piece requires,
performs and returns; why it exists; why it was built this way; and what the
alternatives were.**

Companion to `DESIGN.md` (architecture) and `ASSESSMENT.md` (critique and
roadmap).

---

## How to read this document

Every function is documented against a fixed template:

- **Requires** — inputs and preconditions
- **Performs** — what it actually does
- **Returns** — output and its shape
- **Why** — the reason this function exists as a separate unit
- **Alternative** — other ways it could have been done, and why they were not

Private helpers (leading underscore) are documented where their logic is
non-obvious, and summarised otherwise.

Each module section opens with a **Function flow** block: an ASCII call trace
showing what actually runs, in order, with the branches and failure exits. The
glyphs are consistent throughout:

| Glyph | Meaning |
|---|---|
| `├─` `└─` | a step inside the enclosing call, top to bottom |
| `─►` | produces / calls into |
| `▼` | the value that falls out of the block above |
| `?` on a line | a branch; the outcomes follow on the indented lines |
| `──► ValueError` / `──► 422` | a failure exit and what it raises or returns |
| `▲` then `└─` | a footnote explaining the line directly above |
| `◄──` | an annotation pointing at the line it sits on |
| `⚠` | a known gap, cross-referenced in `CORRECTIONS.md` or `MLOPS.md` |

ASCII rather than Mermaid because this document is also rendered to Word: a
fenced block survives that conversion as monospace, a Mermaid diagram would not.

A recurring theme is worth stating once up front. This system has an unusual
constraint: **the features the model needs do not exist at prediction time.**
Almost every design decision below traces back to that, and to its corollary —
that anything fitted on training data must be *persisted*, because it will be
needed again at serving time.

---

## 1. The end-to-end flow

![Module dependency graph: configuration feeds every layer; data loading and preprocessing produce a validated frame; static feature engineering runs once; the backtest prepares folds through the exogenous cascade; the registry supplies candidates that tuning and evaluation score; the winner is bundled into a forecaster and logged to MLflow.](images/06-module-graph.png)

Read the arrows as "calls" or "produces input for". Configuration is omitted
from most arrows because every module reads it.

### The trace, in order

```
python -m src.pipelines.train_pipeline
│
├─ load_config()                                    utils/config_loader.py
│    └─ discovers project root, resolves paths, derives 22 feature names
│
├─ STAGE 1  load_raw_dataset("train")               data/data_loader.py
│           preprocess_data()                       data/preprocess.py
│             └─ RawDataSchema.validate → dedupe → reindex daily → interpolate
│                → ProcessedDataSchema.validate
│
├─ STAGE 2  build_static_features()                 features/build_features.py
│             └─ engineer_calendar_features() + engineer_residual_demand()
│           drop_incomplete_rows()
│
├─ STAGE 3  prepare_folds()                         models/backtest.py
│             for each of 12 folds:
│               ExogenousCascade().fit(train_window) models/simulate_exogenous.py
│                 ├─ WeatherForecaster.fit()
│                 ├─ engineer_national_temperature(is_train=True)
│                 ├─ engineer_degree_days()
│                 └─ LoadForecaster.fit()
│               cascade.simulate(test_month)
│                 ├─ WeatherForecaster.predict()
│                 ├─ stitch 14-day warm-up, recompute thermal
│                 ├─ LoadForecaster.predict()
│                 └─ engineer_residual_demand()
│             → 12 × PreparedFold (X_train, y_train, X_test, y_test)
│
├─ STAGE 4  build_model_registry()                  models/registry.py
│           for each enabled model:
│             tune_model(folds, ...)                models/train_tune.py
│               └─ Optuna: each trial calls evaluate_folds() on cached folds
│             evaluate_folds(folds, best_estimator)  models/backtest.py
│             mlflow.log_metrics(...) in a nested run
│
├─ STAGE 5  build_leaderboard() / select_best_model() analysis/evaluate.py
│           plot_residual_diagnostics() / plot_backtest_predictions()
│
└─ STAGE 6  PriceForecaster.fit(latest_window)      models/forecaster.py
            forecaster.forecast(future_exog)         → holdout forecast
            explain_predictions_shap()               analysis/explainability.py
            mlflow.pyfunc.log_model(PriceForecasterModel(forecaster))
              → registered as french_spot_price_forecaster
```

### The inference trace

The serving path is much shorter, and the asymmetry is the point: `fit` on the
training path, `simulate` on the inference path.

```
POST /predict
│
├─ (startup, once)  load_config()                       utils/config_loader.py
│                   load_model()                        api/model_loader.py
│                     └─ resolve MODEL_URI / @champion / latest version
│                     └─ mlflow.pyfunc.load_model() -> unwrap PriceForecaster
│
├─ ForecastRequest validation                           api/schemas.py
│    └─ nuclear_avail: not NaN, within [1_000, 80_000] MW
│
└─ forecaster.forecast(frame)                           models/forecaster.py
     ├─ FutureExogenousSchema.validate()                data/schema.py
     ├─ _validate_horizon()          <- raises -> HTTP 422
     ├─ engineer_calendar_features()                    features/build_features.py
     ├─ cascade.simulate()                              models/simulate_exogenous.py
     │    ├─ WeatherForecaster.predict()      (stage 1)
     │    ├─ stitch 14-day warm-up, thermal   (stage 2)
     │    ├─ LoadForecaster.predict()         (stage 3)
     │    └─ engineer_residual_demand()       (stage 4)
     └─ price_model.predict()  -> price + simulated drivers
```

Note what the request path does **not** touch: no MLflow call, no fitting, no
file I/O. Everything fitted was loaded once at startup.

---

## 2. Configuration layer

### `configs/config.yaml`

**Why this file exists.** Everything that an operator might reasonably want to
change without editing Python: paths, feature block membership, cascade
constants, backtest schedules, hyperparameter bounds, MLflow wiring, the model
shortlist. Secrets deliberately live in the environment instead, because a YAML
file in git is the wrong place for credentials.

**Why YAML rather than Python config or environment variables.** YAML holds
nested structure (the three named backtest schedules, the regional temperature
weights) that flat environment variables cannot express, and unlike a Python
config module it cannot execute code — a config file that can run arbitrary
Python is a config file that will eventually contain business logic.

**Alternative considered:** Hydra or Pydantic Settings. Hydra adds
composition and CLI override for free and would be the right choice at larger
scale; it was skipped because a single 200-line config with one consumer does
not justify the dependency. Pydantic Settings would give typed validation of the
config itself, which is the most defensible upgrade here — currently a typo in a
config key surfaces as a `KeyError` deep in a stage rather than at load time.

---

### `src/utils/config_loader.py` (172 lines)

**Function flow**

```
load_config(config_path=None)
│
├─ config_path or  $CONFIG_PATH  or  DEFAULT_CONFIG_PATH
│                                      │
│                                      └─ PROJECT_ROOT / configs/config.yaml
│                                            ▲
│                                            └─ _discover_project_root()   [at import]
│                                                 1. $PROJECT_ROOT
│                                                 2. upward search from cwd
│                                                 3. Path(__file__).parents[2]
│
├─ resolve_path(config_path) ──► exists?  ── no ──► FileNotFoundError
│
├─ yaml.safe_load()  ──► nested dict
│
├─ for key in _PATH_KEYS:  paths[key] = resolve_path(...)      make absolute
│
├─ tracking_uri = $MLFLOW_TRACKING_URI or config value
│     └─ _resolve_tracking_uri()   anchor relative sqlite:/// or file: to root
│
└─ return dict


Derived helpers, called by the pipeline (not by load_config):

get_feature_names(config) ─► calendar + HDD/CDD + thermal + residual
                              └─ feature_set == "notebook"? ─► + [nuclear, demand]
                              └─ unknown value            ─► ValueError
get_hdd_cdd_columns(config) ─► [f"{ref}_HDD", f"{ref}_CDD" for ref in hdd_cdd_reference]
get_active_schedule(config) ─► schedules[active_schedule]  or ValueError listing options
```

**Why this file exists.** To make config loading location-independent. Three
different processes read this config — the training job, the API, and any
interactive session — from three different working directories, and one of them
(the API) loads a *copy* of this very module out of an MLflow artifact.

#### `_discover_project_root() -> Path`

- **Requires** nothing; reads `PROJECT_ROOT` env var and the working directory.
- **Performs** resolution in three ordered attempts: an explicit `PROJECT_ROOT`
  environment variable; an upward search from the current working directory for
  `configs/config.yaml`; finally the module's own location via `__file__`.
- **Returns** the directory containing `configs/config.yaml`.
- **Why.** This is a bug fix elevated to a design decision. MLflow's
  `code_paths=["src"]` copies `src/` into the model artifact and prepends that
  copy to `sys.path`. A served process importing `src.utils.config_loader`
  therefore gets the *artifact's* copy, whose `__file__` sits inside the
  artifact directory. Deriving the root from `__file__` alone made the API look
  for config inside the artifact and raise `FileNotFoundError`.
- **Alternative.** Ship the config *into* the artifact as an MLflow artifact
  file, so the served model reads its own frozen config. That is arguably more
  correct — a model should be served with the config it was trained under — and
  is the natural next step. The current design sidesteps the problem instead by
  storing the resolved config dict inside the pickled `PriceForecaster`, so the
  serving path never calls `load_config()` at all.

#### `resolve_path(path) -> Path`

- **Requires** a string or `PathLike`.
- **Performs** returns absolute paths unchanged; joins relative paths onto
  `PROJECT_ROOT`.
- **Returns** an absolute `Path`.
- **Why.** The original config held paths absolute to one developer's home
  directory, which breaks in any container. Relative-in-config,
  absolute-at-runtime is the portable form.

#### `load_config(config_path=None) -> dict`

- **Requires** optionally a path; otherwise reads `CONFIG_PATH` env var or the
  default.
- **Performs** parses the YAML, rewrites every key in `_PATH_KEYS` to an
  absolute path, resolves the MLflow tracking URI (env var wins over file),
  anchors a relative `sqlite:///` or `file:` URI to the project root.
- **Returns** a plain nested `dict`.
- **Why a dict rather than a typed object.** Every consumer indexes it directly,
  and a dict pickles into the model artifact without dragging a class definition
  along. The cost is no validation and no autocomplete.
- **Alternative.** A frozen dataclass or Pydantic model would catch config typos
  at load time instead of at use time. This is the clearest remaining
  improvement in this file.

#### `_resolve_tracking_uri(tracking_uri) -> str`

- **Performs** for `sqlite:///` and `file:` schemes with a relative path,
  rewrites the path against the project root; leaves absolute and remote URIs
  untouched.
- **Why.** Without this, launching training from the repo root and the MLflow UI
  from elsewhere silently creates two separate databases — a failure mode that
  looks like "my runs disappeared".

#### `get_feature_names(config) -> list[str]`

- **Requires** the config's `features` and `feature_engineering` blocks.
- **Performs** concatenates calendar, derived HDD/CDD names, thermal, and
  residual-demand blocks in a fixed order, then appends the raw nuclear and
  demand levels when `feature_set == "notebook"`. Raises on an unknown
  `feature_set`.
- **Returns** the ordered list of 22 column names.
- **Why ordering matters, and why this is centralised.** The design matrix
  column order is baked into the fitted model. Training and serving must agree
  exactly, so exactly one function may decide it. Deriving the HDD/CDD names
  from `hdd_cdd_reference` rather than hard-coding `T_lisse_HDD` is what keeps
  the config key and the feature list from drifting apart.
- **Alternative.** List all 22 names literally in the config. Simpler to read,
  but then changing `hdd_cdd_reference` silently desynchronises the feature list
  — the exact class of bug this project already had once.

#### `get_hdd_cdd_columns(config)` / `get_active_schedule(config)`

Small derivations kept next to `get_feature_names` so all config-to-name logic
lives in one place. `get_active_schedule` raises with the available names listed
when the requested schedule does not exist, because a silent fallback to a
default schedule would produce metrics that quietly do not mean what the
operator thinks.

---

## 3. Data layer

### `src/data/data_loader.py` (94 lines)

**Function flow**

```
load_raw_dataset(dataset_type="train")
│
├─ dataset_type in ("train","predict")?  ── no ──► ValueError
│
├─ load_config()
│
├─ pick source by dataset_type
│     train   ─► paths.raw_train_csv   /  $S3_TRAIN_FILE_KEY
│     predict ─► paths.raw_pred_csv    /  $S3_PRED_FILE_KEY
│
└─ $ENV == "production" ?
   │
   ├─ yes ─► _load_from_s3(key)
   │           ├─ $S3_BUCKET_NAME set?  ── no ──► ValueError
   │           ├─ boto3.client("s3", region_name=...)    ◄── NO credentials passed:
   │           │                                             boto3 chain resolves
   │           │                                             env vars locally,
   │           │                                             IAM role on EC2
   │           ├─ get_object(Bucket, Key)
   │           └─ read_csv(io.BytesIO(body))
   │
   └─ no  ─► _load_from_disk(path)
               └─ read_csv(path)
                        │
                        ▼
           raw pd.DataFrame — date still a column, no schema applied
```

**Why this file exists.** To make the source of data a deployment concern rather
than a code concern. Every other module receives a DataFrame and never learns
whether it came from disk or object storage.

#### `load_raw_dataset(dataset_type="train") -> pd.DataFrame`

- **Requires** `dataset_type` in `("train", "predict")`; `ENV` and, in
  production, the S3 environment variables.
- **Performs** validates the dataset type, loads config, picks the local path
  and S3 key for that dataset, then delegates to `_load_from_s3` or
  `_load_from_disk` based on `ENV`.
- **Returns** a raw, unvalidated DataFrame — dates still a column, no schema
  applied.
- **Why the train/predict split is a parameter and not two functions.** The two
  paths differ only in which path and key to read; the branching logic is
  identical. Two functions would duplicate the `ENV` switch.
- **Why it returns raw data.** Loading and validating are separate concerns.
  This function's only job is bytes-to-DataFrame; preprocessing owns correctness.
- **Alternative.** Return a lazy handle (Polars `scan_csv`, Dask) instead of an
  eager frame. Pointless at 2,007 rows; necessary at hourly multi-market scale.

#### `_load_from_s3(s3_file_key)` / `_load_from_disk(local_path)`

`_load_from_s3` streams the object body through `io.BytesIO` into `read_csv`
rather than downloading to a temp file, which avoids leaving artefacts on an
ephemeral container filesystem. It raises with a clear message if
`S3_BUCKET_NAME` is unset, because the default boto3 error for a `None` bucket
is unhelpfully opaque.

**Credential resolution (fixed).** This function passes **no** credentials to
`boto3.client`. That is deliberate: boto3 walks its own chain — environment
variables locally (loaded from `.env`), then `~/.aws/credentials`, then the
**IAM instance role** on EC2. One code path works in both places, and the
long-lived `AKIA…` keys have no production role at all. `s3_store.py` follows
the same rule.

---

### `src/data/s3_store.py` (196 lines)

**Function flow**

```
S3Store(config)
│
├─ bucket = $S3_BUCKET_NAME ;  env = $ENV
│
├─ .enabled  ──►  env == "production"  AND  bucket set
│                  ▲
│                  └─ gated on BOTH so a stray credential in the environment
│                     cannot make a local run start writing to the cloud
│
└─ .client (lazy)  ──►  boto3.client("s3", region_name=...)
                          ▲
                          └─ credentials deliberately NOT passed: boto3 resolves
                             them from the environment locally and from the IAM
                             INSTANCE ROLE on EC2, which is what allows the
                             static keys to be deleted in deployment

WRITES — fail-soft: report and swallow

put_bytes(data, key)        ──► put_object      ── error ──► warn, return None
put_file(local_path, key)   ──► upload_file     ── error ──► warn, return None
                                  └─ multipart-aware for large files
put_dataframe(df, key, fmt) ──► io.BytesIO
                                  ├─ "parquet" ─► to_parquet(index=True)   default
                                  ├─ "csv"     ─► to_csv().encode()
                                  └─ other     ─► ValueError
                                       │
                                       └─► put_bytes()
save_forecast(df, run_date) ──► put_dataframe(forecast_key(config, run_date))

READS — raise: the caller depends on the result

get_bytes(key)              ──► get_object["Body"].read()
read_dataframe(key, fmt)    ──► read_parquet / read_csv on the buffer
exists(key)                 ──► head_object
                                  ├─ 404 / NoSuchKey ─► False
                                  └─ any other error ─► raise
list_prefix(prefix)         ──► get_paginator("list_objects_v2")   yields keys
                                  ▲
                                  └─ paginated on purpose: bare list_objects_v2
                                     silently caps at 1,000 keys
load_forecast_archive()     ──► list_prefix(forecasts/) ─► read each ─► concat
                                  └─ recovers dt=YYYY-MM-DD from the key into a
                                     `forecast_vintage` column

KEY BUILDERS

forecast_key(config, run_date) ─► "forecasts/dt=YYYY-MM-DD/forecast.parquet"
artifact_key(config, run, file) ─► "mlflow-artifacts/runs/<run_id>/<file>"
_prefix(config, name)           ─► paths.s3_prefixes[name], one trailing slash
```

**Why this file exists.** `data_loader.py` reads *from* S3; nothing wrote *to*
it. The motivating case is the forecast archive: **a forecast that is not stored
on the day it is made cannot be reconstructed later**, so the realised-accuracy
record can only begin once this module exists. It is the enabling step for
`MLOPS.md` step 11 (monitoring), and it costs almost nothing.

#### `S3Store.enabled` (property)

- **Returns** `True` only when `ENV == "production"` **and** a bucket is set.
- **Why both conditions.** Either alone is unsafe. Gating on the bucket only
  would mean a developer with `S3_BUCKET_NAME` exported starts writing to the
  cloud from a local experiment; gating on `ENV` only would mean a
  production run with a missing bucket fails deep inside a write instead of
  no-opping.
- **Why a property rather than a check at each call site.** Every method
  short-circuits on it, so calling code never needs an
  `if ENV == "production"` branch. The pipeline calls
  `S3Store(config).save_forecast(...)` unconditionally and it is simply inert
  locally.

#### `S3Store.client` (lazy property)

- **Performs** builds `boto3.client("s3", region_name=...)` on first use.
- **Why credentials are not passed explicitly.** This is the deliberate
  difference from `data_loader._load_from_s3`, which does pass them. Omitting
  them lets boto3 walk its resolution chain — environment variables locally,
  **IAM instance role on EC2** — so the same code needs no static keys in
  deployment. `data_loader` should be changed to match; see `AWS_S3_EC2.md`
  Part 5 step 3.
- **Why lazy.** Constructing `S3Store` is then free, so it can be instantiated
  in a local run without touching botocore or looking for credentials.

#### Writes: `put_bytes` / `put_file` / `put_dataframe`

- **Returns** the `s3://` URI on success, `None` when disabled or on failure.
- **Why writes are fail-soft and reads are not.** Asymmetric on purpose. Losing
  an archive entry is bad but must not take down a training run that produced a
  valid model — the same reasoning as the pipeline's holdout `try/except`. A
  read, by contrast, is something the caller is *depending* on, so failing
  silently would hand back wrong data.
- **Why Parquet is the default.** It preserves the datetime index and dtypes
  without a re-parse, and it compresses. Compression matters here beyond disk:
  **S3 charges per request and per GB**, so fewer, smaller, self-describing
  objects is the cheaper and more robust shape. CSV stays available for anything
  a human needs to open directly.
- **Why `io.BytesIO` rather than writing a temp file.** On an ephemeral
  container there is no reason to touch the filesystem, and nothing is left to
  clean up. Same pattern as the read path in `data_loader`.

#### `forecast_key` — why date partitions

- **Returns** `forecasts/dt=YYYY-MM-DD/forecast.parquet`.
- **Why `dt=` rather than a flat `forecast_2020-07-01.parquet`.** Three
  reasons: the archive stays append-only, so a bad write damages one partition
  instead of the set; a single day can be reprocessed in isolation; and
  **Athena and Glue recognise `key=value` directory naming as a partition
  column automatically**, so the archive is queryable with SQL later without
  any migration.
- **Why `run_date` is the date the forecast was *made*.** Not the period it
  covers. A forecast must be scorable against actuals with its **vintage**
  known — "what did we believe on the 1st?" is the question the accuracy record
  answers, and a horizon-keyed archive cannot answer it.

#### `list_prefix` — the 1,000-key trap

- **Performs** paginates `list_objects_v2` and yields keys.
- **Why a paginator is not optional.** A bare `list_objects_v2` returns **at
  most 1,000 keys with no error**, so code that looks correct silently processes
  only the first thousand objects. At one forecast per day the archive passes
  1,000 keys in under three years.

#### `load_forecast_archive`

- **Returns** every archived forecast concatenated, with a `forecast_vintage`
  column recovered from each key.
- **Why the vintage comes from the key rather than the data.** The key is the
  authoritative record of when the forecast was written; a column inside the
  file could be wrong if the frame were ever rebuilt. The partition *is* the
  metadata.
- **What this unlocks.** Join it to settled prices and you have a **live
  scorecard** rather than a backtest number — the difference between a demo and
  a product.

**Verified end to end** (`ENV=production`, real bucket): wrote 8,256 B of
Parquet to `forecasts/dt=2020-07-01/forecast.parquet`, read it back identical
(`DataFrame.equals` -> True), `exists` -> True, `list_prefix` found it, and
`load_forecast_archive` returned it with the vintage attached.

**Where it is called.** `train_pipeline.py` stage 6, immediately after the
holdout forecast is logged to MLflow. Inert locally, archives in production.

**Not yet called from the API.** Archiving inside `/predict` is the right
pattern for a live product but would add an S3 round trip to every request, so
it should be fire-and-forget or queued rather than inline. Deferred.

---

### `src/data/schema.py` (74 lines)

**Function flow**

```
Declarative contracts. Not called in sequence; applied at three boundaries.

  raw CSV
     │
     ▼
  RawDataSchema.validate()          all columns nullable
     │                              "did we receive the columns we expect?"
     ▼
  (dedupe, reindex daily, interpolate)
     │
     ▼
  ProcessedDataSchema.validate()    all columns NON-NULL
     │                              "did cleaning actually work?"  gates training
     ▼
  training / backtest


  forecast CSV or API request
     │
     ▼
  FutureExogenousSchema.validate()  only NUCLEAR non-null
                                    rest Optional + nullable, filled by the cascade
     │                              "is the one real input present?"
     ▼
  cascade.simulate()

Config on all three:  strict = False   (extra engineered columns allowed)
                      coerce = True    (CSV int columns -> float)
```

**Why this file exists.** To make data expectations executable rather than
documented. A schema violation should fail loudly at the boundary, not surface
as a `NaN` in a metric forty lines later.

**Why three schemas and not one.** This is the crux of the file, and the reason
the original single-schema design made the predict path impossible.

| Schema | Applied at | Nullability | Purpose |
|---|---|---|---|
| `RawDataSchema` | After ingestion | Everything nullable | Confirms columns exist and coerce to float; gaps are expected and about to be fixed |
| `ProcessedDataSchema` | After cleaning | Everything **non-null** | The gate on model training: no missing values may reach a fold |
| `FutureExogenousSchema` | Inference input | Only nuclear non-null; rest `Optional` | The forecast frame is *supposed* to be mostly empty |

- **Why `strict = False`.** Extra columns are permitted, so engineered features
  can accumulate on a frame without invalidating it against the schema.
- **Why `coerce = True`.** CSV integer columns arrive as `int64` and would fail
  a `float` check; coercion normalises them rather than forcing the loader to
  care about dtypes.
- **Why `Optional[Series[float]]` rather than a `required=False` field.** Not a
  style choice — pandera 0.32 has no `required` argument on `Field`, and
  optionality is expressed through the type annotation.
- **Alternative.** Great Expectations gives richer checks (distributional drift,
  value ranges, cross-column assertions) and a data-docs UI, at the cost of
  significant configuration. Pydantic cannot do this job — it validates records,
  not columnar frames.

**What is missing.** These schemas check structure and nullability but no
*values*. A price of 10,000 EUR/MWh or a temperature of 200 °C would pass. Range
checks are the obvious next addition and are cheap in pandera.

---

### `src/data/preprocess.py` (138 lines)

**Function flow**

```
TRAINING PATH                          INFERENCE PATH
preprocess_data(df, config)            prepare_future_exogenous(df, config)
│                                      │
├─ _set_datetime_index() ◄─── shared ──┤
│    parse date_col, sort, name "Date" │
│                                      │
├─ RawDataSchema.validate()            │
│                                      │
├─ _drop_duplicate_dates() ◄── shared ─┤
│    keep="first", report count        │
│                                      │
├─ date_range(min,max,freq="D")        ├─ date_range(min,max,freq="D")
│    missing days > 0 ?                │    missing days > 0 ?
│      └─ reindex  (NaN rows appear)   │      └─ reindex only
│      └─ interpolate(method="time",   │
│           limit_direction="both")    ├─ interpolate ONLY the nuclear column
│         ▲                            │    └─ other columns stay NaN on purpose:
│         └─ reindex BEFORE            │       filling them would invent the very
│            interpolate — absent      │       values the cascade exists to produce
│            dates are missing ROWS,
│            not NaN rows              │
│                                      │
├─ ProcessedDataSchema.validate()      └─ FutureExogenousSchema.validate()
│    raises if any NaN remains              only nuclear must be complete
▼                                      ▼
gap-free, complete daily frame         gap-free index, mostly empty by design


run_preprocessing()          standalone entry point, `python -m src.data.preprocess`
  └─ load_raw_dataset("train") ─► preprocess_data() ─► to_parquet(processed_parquet)
     (a cache for interactive work; the training pipeline re-derives instead)
```

**Why this file exists.** To turn a raw CSV into a frame satisfying
`ProcessedDataSchema`: a gap-free daily index with no missing values. The
cascade's exponentially-weighted smoothing assumes a continuous daily series, so
this is a precondition rather than a nicety.

#### `_set_datetime_index(df, config)` / `_drop_duplicate_dates(df)`

Shared by both public entry points. Parsing the date column, sorting, and naming
the index `Date` is identical work for training and inference frames, so it is
factored out. `_drop_duplicate_dates` keeps the first occurrence and reports how
many it dropped, because a silent dedupe hides a data-source problem.

#### `preprocess_data(df, config) -> DataFrame[ProcessedDataSchema]`

- **Requires** a raw frame with a parseable date column and the expected value
  columns.
- **Performs** sets the index, validates against `RawDataSchema`, drops
  duplicate dates, reindexes onto a complete daily `date_range`, time-interpolates
  the resulting holes, then validates against `ProcessedDataSchema`.
- **Returns** a clean daily frame; raises if any column is still null.
- **Why reindex-then-interpolate, in that order.** Interpolating first does
  nothing: absent dates are not `NaN` rows, they are *missing rows*, and
  `interpolate` cannot fill a row that does not exist. Reindexing materialises
  them as `NaN` first. This ordering was the subtle fix in the original
  notebook and is preserved here.
- **Why `method="time"`.** It interpolates proportionally to the actual date gap
  rather than to row position, which is the correct behaviour for a series with
  irregular gaps.
- **Why validate twice.** The two validations answer different questions.
  Before: "did we receive the columns we expect?" After: "did cleaning actually
  work?" The second is what turns a silent `NaN` into a loud failure.
- **Alternative.** Forward-fill instead of interpolate. Cheaper and never
  invents an unobserved value, but it flat-lines temperature across a gap, which
  distorts degree days. Interpolation is the better trade for a smooth physical
  series. For a *price* series, forward-fill would arguably be safer — prices
  jump, they do not glide.

#### `prepare_future_exogenous(df, config) -> DataFrame[FutureExogenousSchema]`

- **Requires** a forward-looking frame with a date column and nuclear
  availability.
- **Performs** the same index handling, then reindexes to a continuous daily
  horizon, interpolates **only** the nuclear column, and validates against the
  lenient schema.
- **Returns** a frame whose unknowable columns remain `NaN`.
- **Why this exists as a second function rather than a flag on the first.** The
  two paths differ in intent, not just degree. `preprocess_data` asserts
  completeness; this one asserts that exactly one column is complete and leaves
  the rest deliberately empty. Expressing that as `preprocess_data(strict=False)`
  would hide the asymmetry behind a boolean.
- **Why only the nuclear column is interpolated.** Interpolating the temperature
  or demand columns would silently invent exogenous values that the cascade
  exists to produce — filling them would mean the cascade quietly overwrites
  fabricated data with simulated data, and any bug in that overwrite would be
  invisible. Leaving them `NaN` makes the cascade's contribution explicit and
  errors detectable.

#### `run_preprocessing() -> None`

A standalone entry point (`python -m src.data.preprocess`) that caches the clean
frame to Parquet. Parquet rather than CSV because it preserves the datetime index
and dtypes without a re-parse. Note that the training pipeline does **not** read
this cache — it re-derives the clean frame each run, so the two cannot fall out
of sync. The cache exists for interactive work.

---

## 4. Feature layer

### `src/features/build_features.py` (236 lines)

**Function flow**

```
STATIC — computed once over all history (index-only or already-present columns)

build_static_features(df, config)
│
├─ engineer_calendar_features(df, config)
│    ├─ year, month, day_of_year, day_of_week, is_weekend
│    ├─ holidays.France(years) ─► is_holiday        (compare on .date objects,
│    │                                               not datetime64)
│    ├─ is_lockdown              from config window
│    ├─ vacation phases: is_school_end_july
│    │                   is_bastille_bridge   (dynamic: Mon-before / Fri-after)
│    │                   is_grandes_vacances_july
│    │                   is_august_vacation
│    │      └─ mutual exclusion: bridge day zeroes the other two
│    └─ sin/cos day_of_year      cyclical encoding for the linear model
│
└─ engineer_residual_demand(df, demand_col, nuclear_col)
     └─ Residual_Demand = DEMAND - NUCLEAR,  + _sq, + _cu

drop_incomplete_rows(df, config)
  └─ dropna on calendar + weather + residual + market + target
     ▲
     └─ deliberately NOT on thermal columns — they don't exist yet


DYNAMIC — window-dependent, called per fold from inside the cascade

engineer_national_temperature(df, config, is_train, t_norm_reference)
│
├─ T_raw   = Σ region × weight        (Paris .69 Lyon .13 Marseille .10 Bordeaux .08)
├─ T_lisse = T_raw.ewm(alpha).mean()  thermal inertia of building stock
│
├─ is_train ?
│    ├─ yes ─► _build_seasonal_reference(T_lisse, index)
│    │           ├─ groupby(dayofyear).mean()
│    │           └─ day 366 missing? ─► copy day 365   (731-day window may
│    │                                                  contain no Feb 29)
│    └─ no  ─► use the supplied t_norm_reference       ◄── FITTED STATE,
│                                                          must be persisted
├─ T_norm  = dayofyear.map(reference)
│    └─ any unmapped? ─► ValueError naming the days
├─ Delta_T = T_lisse - T_norm         "how unusual for the time of year"
│
└─ return (frame, t_norm_reference)   reference returned so it can be stored

engineer_degree_days(df, temp_features, config)      MUST RUN AFTER the above
│
├─ source columns present? ── no ──► KeyError naming the ordering requirement
└─ for col in temp_features:   (= hdd_cdd_reference = ["T_lisse"])
     ├─ {col}_HDD = max(15 - T, 0)     heating response
     └─ {col}_CDD = max(T - 22, 0)     cooling response
```

**Why this file exists.** To hold every transformation from raw columns to model
inputs, with a strict separation the rest of the system depends on:

- **Static features** depend only on the index or on columns already present, so
  they can be computed once over all history.
- **Dynamic features** (thermal state, simulated demand) depend on a *training
  window* and must therefore be computed per fold, inside the cascade.

Mixing those two would leak: computing a day-of-year temperature climatology
over all history and then testing on a subset of it means the test period
informed its own baseline.

#### `engineer_calendar_features(df, config) -> pd.DataFrame`

- **Requires** a `DatetimeIndex`; the lockdown dates from config.
- **Performs** derives 13 features: year, month, day-of-year, day-of-week,
  weekend flag, French public holidays, a COVID lockdown window, four
  French-summer-vacation regime flags, and sine/cosine day-of-year encodings.
- **Returns** the input frame plus those columns.
- **Why calendar features are safe to compute globally.** They are functions of
  the index alone. No observation informs another, so there is nothing to leak.
- **Why sine/cosine encoding alongside raw `day_of_year`.** The raw integer makes
  31 December and 1 January maximally distant when they are physically adjacent.
  The cyclical pair fixes that for the linear model. Trees do not need it, which
  is why both representations are present — one model benefits, the other
  ignores it.
- **Why the holiday check compares `date` objects.** `dt_index.isin(fr_holidays)`
  compares `datetime64` values against a holidays mapping, which pandas
  deprecated; converting to `date` first is both correct and warning-free.
- **Why bridge days are computed dynamically.** Bastille Day falls on a fixed
  date but the *bridge* (`pont`) depends on which weekday it lands on: a Monday
  before or a Friday after gets absorbed into the holiday. The mutual-exclusion
  lines afterwards ensure a day belongs to exactly one vacation phase, so the
  flags stay interpretable as a partition rather than overlapping indicators.
- **Alternative.** A single categorical "regime" column instead of four binary
  flags. More compact and better for trees, but it forces one-hot encoding for
  the linear model and loses the ability for two regimes to co-occur.
- **Known issue.** `year` belongs in this function's output and is the project's
  main modelling problem — see `ASSESSMENT.md` §3.1.

#### `engineer_residual_demand(df, demand_col, nuclear_col) -> pd.DataFrame`

- **Requires** the demand and nuclear columns present.
- **Performs** computes `Residual_Demand = demand - nuclear` plus its square and
  cube.
- **Returns** the frame plus three columns.
- **Why residual demand at all.** It is the economically meaningful quantity:
  the load that must be met by dispatchable, price-setting plant after
  must-run nuclear. Price responds to *that*, not to gross load.
- **Why polynomial terms.** The supply curve is convex — price rises
  disproportionately as residual demand approaches available capacity. Squared
  and cubed terms let a *linear* model express that curvature. Trees derive it
  from splits and do not need them.
- **Why this function takes explicit column names rather than reading config.**
  It is called from three places, including inside the cascade where the demand
  column has just been overwritten with simulated values. Explicit arguments
  make the caller's intent visible.
- **Alternative.** A spline basis or a piecewise-linear "capacity margin"
  feature would model convexity more faithfully than a cubic, which extrapolates
  explosively outside the training range.

#### `engineer_degree_days(df, temp_features, config) -> pd.DataFrame`

- **Requires** the columns named in `temp_features` to exist; thresholds from
  config.
- **Performs** for each source column, `HDD = max(15 - T, 0)` and
  `CDD = max(T - 22, 0)`.
- **Returns** the frame plus two columns per source.
- **Why degree days rather than raw temperature.** Electricity demand responds
  to temperature asymmetrically and non-linearly: below ~15 °C heating load
  rises, above ~22 °C cooling load rises, and in between demand is
  temperature-insensitive. A single linear temperature coefficient cannot
  represent a V-shape. Degree days encode the physics directly.
- **Why the explicit `missing` check that raises `KeyError`.** This guards the
  exact defect that made the migrated code non-functional: degree days were
  being derived from the raw city columns instead of from `T_lisse`, producing
  `PARIS_AVGTEMP_C_HDD` and never the `T_lisse_HDD` the model requires. The
  error message names the ordering requirement so the failure is
  self-explanatory.
- **Alternative.** Fit the breakpoints instead of fixing them at 15 and 22 —
  a hinge/segmented regression would learn where the thermal response actually
  turns, and those thresholds vary by country and building stock.

#### `_build_seasonal_reference(smoothed, dt_index) -> pd.Series`

- **Performs** groups smoothed temperature by day-of-year and takes the mean;
  then, if day 366 is absent but 365 is present, copies 365 into 366.
- **Why the leap-day fallback exists.** A 731-day rolling window need not
  contain a 29 February. Without the fallback, `T_norm` is unmapped on leap days
  and `Delta_T` becomes `NaN`, which propagates silently into the design matrix.
- **Alternative.** A smooth harmonic (Fourier) climatology instead of a
  day-of-year mean. Better: it would be continuous, need no leap-day special
  case, and be far less noisy given only two observations per day-of-year.

#### `engineer_national_temperature(df, config, is_train=True, t_norm_reference=None)`

- **Requires** the four regional temperature columns; weights and `ewm_alpha`
  from config; a reference series when `is_train=False`.
- **Performs** computes `T_raw` as the weighted regional average, `T_lisse` as
  its exponentially-weighted mean, `T_norm` as the day-of-year climatology, and
  `Delta_T = T_lisse - T_norm`. In training mode it *derives* the climatology; in
  inference mode it *applies* a supplied one. Raises if any day-of-year is
  unmapped.
- **Returns** `(enriched_frame, t_norm_reference)` — the reference is returned so
  the caller can persist and reuse it.
- **Why `T_lisse` (exponential smoothing) rather than raw temperature.**
  Buildings have thermal mass. Heating demand today depends on the last several
  days' temperature, not only today's. `alpha=0.5` gives a half-life of one day,
  which is the standard French utility construction this feature is named after.
- **Why the `is_train` flag rather than two functions.** The computation is
  identical except for the single question of whether the climatology is fitted
  or applied. This is the fit/transform split that scikit-learn expresses
  through two methods; here it is one function with a mode flag because it also
  needs to return the fitted state for persistence.
- **Why returning `t_norm_reference` is load-bearing.** This is a *fitted
  parameter*. If the serving path recomputed it from whatever data it had, the
  served model would use a different seasonal baseline than the trained one.
  Returning it is what allows the cascade to store it in the artifact.
- **Alternative.** Make this a proper scikit-learn transformer with
  `fit`/`transform`. Cleaner, composable into a `Pipeline`, and would get
  persistence for free. The reason it is not: it produces *multiple named
  columns* from *multiple named inputs*, which sklearn's array-in/array-out
  transformer contract handles awkwardly. `ColumnTransformer` plus
  `set_output(transform="pandas")` makes it possible but noisy.

#### `build_static_features(df, config) -> pd.DataFrame`

- **Performs** calls `engineer_calendar_features` then
  `engineer_residual_demand`.
- **Why this two-line wrapper exists.** It is the single documented answer to
  "what is safe to compute once over all history?". Without it, the pipeline
  would call the two functions directly and the boundary between static and
  fold-local features would live only in a comment.

#### `drop_incomplete_rows(df, config) -> DataFrame[ProcessedDataSchema]`

- **Performs** drops rows with nulls in the calendar, weather, residual-demand,
  market and target columns; reports the count.
- **Why it checks only *those* columns.** The thermal and simulated-demand
  features do not exist yet at this point — they are produced per fold. Checking
  for them here would drop every row. This narrow column list is the
  static/dynamic boundary made explicit a second time.

---

## 5. The cascade

### `src/models/simulate_exogenous.py` (445 lines)

**Function flow**

```
FITTING — once per fold in the backtest, once for the served model

ExogenousCascade.fit(train_data)
│
├─ STAGE 1  WeatherForecaster(region_cols).fit(train_frame)
│             ├─ _remove_leap_days()          keep strict 365-day cycles
│             ├─ t = 1..n ; cos_1 sin_1 cos_2 sin_2   Fourier, 2 harmonics
│             ├─ per region: lag_1, lag_2
│             └─ LinearRegression per region ─► 8 coefficients each
│                    a, b, alpha_1, beta_1, alpha_2, beta_2, phi_1, phi_2
│
├─ STAGE 2  _add_thermal_features(train_frame, is_train=True)
│             ├─ engineer_national_temperature()  ─► T_lisse, Delta_T
│             │     └─ captures t_norm_reference_        ◄── ORDER IS
│             └─ engineer_degree_days(hdd_cdd_reference)     A CORRECTNESS
│                   ─► T_lisse_HDD, T_lisse_CDD             REQUIREMENT
│
├─ STAGE 4  engineer_residual_demand()   on observed demand
│             (done before stage 3 only because the load model ignores it)
│
├─ STAGE 3  LoadForecaster(calendar, hdd_cdd).fit(enriched, demand_col)
│             └─ StandardScaler + Ridge(alpha=1.0)
│
└─ retain fitted state
     warmup_frame_   = last 14 rows  (AR(2) lags + warm EWM)
     train_end_      = last observed date
     enriched_train_ = frame with region columns dropped
                       ▲
                       └─ dropped so the price model can never train on raw
                          temperatures that are SIMULATED at inference time


SIMULATION — every forecast, training or served

ExogenousCascade.simulate(future_frame)
│
├─ _check_is_fitted()
├─ guards ── any failure ──► raises, surfaced by the API as HTTP 422
│    ├─ nuclear column present?
│    ├─ nuclear non-null?               NaN here ─► NaN price
│    ├─ calendar features present?
│    └─ forecast_start > train_end_?    overlap breaks the warm-up stitch
│
├─ STAGE 1  weather_model_.predict(start, horizon, warmup_frame_)
│             for i, date in enumerate(horizon):
│               deterministic = a + b·t + Σ Fourier(t)
│               lags:  i==0 ─► two observed values
│                      i==1 ─► one predicted + one observed
│                      i>=2 ─► two predicted        recursive
│               pred = deterministic + phi_1·lag_1 + phi_2·lag_2
│             └─ AR term decays to climatology within ~1 week
│                  └─ this is why max_horizon_days = 31
│
├─ STAGE 2  concat([warmup_frame_, simulated])      stitch 14 real days in front
│             ├─ _add_thermal_features(is_train=False)   reuse stored t_norm
│             └─ .loc[start:end]                    then discard the warm-up
│                  ▲
│                  └─ pandas ewm has no "initial state"; stitch-then-slice is
│                     how the EWM enters the horizon already converged
│
├─ STAGE 3  load_model_.predict(simulated)  ─► DEMAND (MW)
│
├─ STAGE 4  engineer_residual_demand()      recomputed, NOT reused
│             ▲
│             └─ its DEMAND input was just replaced by a simulated value
│
└─ drop region columns ─► design frame for the price model


RobustArcSinTransformer   (target transform, used by the price models)
  fit(y)        ─► median_, mad_       robust: 3 outliers move MAD ~1.6%
  transform     ─► arcsinh((y - median_) / (mad_ / 0.6745))
  inverse       ─► 0.6745·sinh(x)·mad_ + median_
  ▲
  └─ a scikit-learn transformer so TransformedTargetRegressor refits it
     INSIDE each fold — that is where leakage protection comes from,
     not from the functional form
```

**Why this file exists.** It answers the defining question of the project: given
only nuclear availability, produce the other 20 features. Four classes, in
dependency order.

#### `class RobustArcSinTransformer(BaseEstimator, TransformerMixin)`

- **`fit(X)`** — stores the median and median absolute deviation of the target;
  substitutes `1e-6` if MAD is zero to avoid division by zero on a constant
  series.
- **`transform(X)`** — `arcsinh((X - median) / (MAD / 0.6745))`.
- **`inverse_transform(X)`** — the exact algebraic inverse, so predictions return
  to EUR/MWh.
- **Why arcsinh rather than log.** Prices here range from **-10 to +126
  EUR/MWh**. `log` is undefined on non-positive values. The usual workarounds —
  clipping at zero, adding a constant, dropping negatives — destroy or distort
  precisely the extreme observations that matter most in a power market.
  `arcsinh` is defined on the whole real line, behaves like the identity near
  zero and logarithmically in the tails, so it compresses spikes without
  touching the ordinary range.
- **Why MAD scaling and the 0.6745 constant.** MAD is robust to the outliers
  that would dominate a standard deviation. Dividing by 0.6745 (the 75th
  percentile of the standard normal) rescales MAD to be comparable to a standard
  deviation for normally-distributed data, so the arcsinh operates on a
  predictable scale.
- **Why implement `inverse_transform` at all.** It is what
  `TransformedTargetRegressor` calls to return predictions to original units.
  Without it the model would emit arcsinh-space numbers.
- **Why a scikit-learn transformer rather than two functions.** Being an
  estimator means `TransformedTargetRegressor` will clone and fit it *inside*
  each cross-validation fold, so the median and MAD are computed on training
  data only. Implementing it as free functions applied before splitting is the
  standard way this leaks.
- **Alternative.** `QuantileTransformer` or a Box-Cox/Yeo-Johnson power
  transform. Yeo-Johnson also handles negatives and is fitted rather than
  fixed-form, so it is a reasonable competitor; arcsinh was chosen for its
  closed-form invertibility and interpretability.

#### `class WeatherForecaster`

Stage 1. Forecasts four regional temperatures with no external weather data.

- **`fit(train_df)`**
  - **Requires** a daily frame containing the four regional columns.
  - **Performs** strips 29 February to keep strict 365-day cycles; builds a
    time index `t`; constructs two Fourier harmonic pairs at annual frequency;
    creates lag-1 and lag-2 columns per region; fits one OLS per region on
    `[t, cos1, sin1, cos2, sin2, lag1, lag2]`; stores eight coefficients per
    region.
  - **Returns** `self`.
  - **Why Fourier plus AR(2).** Temperature is dominated by a smooth annual
    cycle (the Fourier terms), a weak trend (`t`), and short-range persistence —
    tomorrow resembles today (the AR terms). Two harmonics capture the annual
    cycle plus its first asymmetry; more would start fitting noise at daily
    resolution.
  - **Why store bare coefficients rather than the fitted sklearn objects.** The
    `predict` step is recursive and needs to evaluate the equation term by term
    while feeding its own output back as the next lag. A plain coefficient dict
    makes that loop explicit and keeps the persisted artifact small.
  - **Why leap days are stripped.** With 365 hard-coded as the Fourier period,
    keeping 29 February would slowly desynchronise the harmonic from the
    calendar.

- **`predict(forecast_start_date, horizon, last_two_days_actuals)`**
  - **Requires** at least two rows of observed regional temperature to seed the
    lags; raises otherwise with an explicit message.
  - **Performs** iterates day by day; for each region evaluates the
    deterministic Fourier/trend part, then adds the AR contribution using
    observed values for the first two steps and its own predictions thereafter.
  - **Returns** a `DataFrame` of predicted temperatures indexed by forecast date.
  - **Why recursive rather than direct multi-step.** Only one model per region
    needs fitting, and the AR structure is used as intended. The cost is
    error accumulation: the AR term decays toward the deterministic seasonal
    mean within roughly a week, which is precisely why `max_horizon_days` is
    capped at 31 — beyond that the forecast is a climatology with extra steps.
  - **Alternative, and the right one in production.** Use an actual numerical
    weather prediction forecast (ECMWF, GFS) for the first ~10 days. No
    statistical model competes with NWP at short horizons. This class exists
    because the dataset provides no weather forecast, and it is honest about
    being a stand-in.
  - **Known defect.** `t` is built over leap-day-stripped data but the forecast
    offset is computed in calendar days, so `t` drifts by one or two days across
    a two-year window. Preserved deliberately from the notebook, since fixing it
    moves every published number.

#### `class LoadForecaster`

Stage 3. Predicts electricity demand from calendar and thermal features.

- **`__init__(calendar_features, hdd_cdd_columns, alpha=1.0, random_state=42)`**
  builds a `StandardScaler` + `Ridge` pipeline and stores the feature list.
- **`fit(train_df, target_col)`** drops rows with nulls in its features, then
  fits.
- **`predict(forecasted_features_df)`** returns a named `Series` indexed to
  match its input.
- **Why Ridge rather than something stronger.** Demand is a genuinely smooth,
  well-behaved function of calendar and temperature — the relationship is close
  to linear once degree days encode the thermal non-linearity. Ridge handles the
  collinearity between HDD/CDD and month, trains in milliseconds inside a
  12-fold loop, and cannot overfit a 731-row window the way a boosted ensemble
  could. Its errors are also *smooth*, which matters because they propagate into
  the price model.
- **Why scaling.** Ridge's penalty is scale-dependent; unscaled, `day_of_year`
  (0-366) and `is_weekend` (0-1) would be penalised incomparably.
- **Why it returns a `Series` with the input's index.** The caller assigns it
  straight back onto the simulated frame; a bare array would risk silent
  misalignment.
- **Alternative.** Gradient boosting on demand, or a dedicated load-forecasting
  model with weekly seasonality and holiday effects. Load forecasting is a
  mature field in its own right and this is a deliberately simple stand-in.

#### `class ExogenousCascade`

The orchestrator, and the reason this file is more than three loose classes.

- **`__init__(config, calendar_features, hdd_cdd_columns)`** reads the region
  columns, degree-day reference, demand/nuclear column names and warm-up length
  from config; initialises all fitted attributes to `None`.
- **`_add_thermal_features(df, is_train)`** calls
  `engineer_national_temperature` then `engineer_degree_days`, **in that order**.
  - **Why this tiny helper exists.** The ordering is a correctness requirement,
    not a preference: degree days are derived from `T_lisse`, which national
    temperature produces. Both `fit` and `simulate` need the same sequence, so
    encoding it once removes the possibility of one path getting it wrong. This
    is the fix for the project's most serious defect, expressed structurally
    rather than as a comment.

- **`fit(train_data) -> self`**
  - **Requires** a clean training window with regional temperatures, demand,
    nuclear and the calendar features.
  - **Performs** fits the weather model; builds thermal features and captures
    the climatology; computes residual demand; fits the load model on observed
    demand; retains the last `warmup_days` rows as `warmup_frame_`; records
    `train_end_`; stores the enriched training frame with region columns dropped.
  - **Returns** `self`, holding every fitted component.
  - **Why the warm-up frame is retained.** Two independent reasons. The AR(2)
    lags need the last two observed temperatures. And `T_lisse` is an
    exponentially-weighted mean, which if started at the first forecast day
    would begin from a cold state and be badly wrong for roughly a week. Keeping
    14 real days lets the EWM enter the forecast already converged.
  - **Why `train_end_` is stored.** It is what allows the served model to refuse
    a horizon that starts inside or before the training window, where the
    warm-up stitch would be invalid.
  - **Why region columns are dropped from `enriched_train_`.** They have served
    their purpose — they are inputs to the thermal block, not model features.
    Dropping them guarantees the price model cannot accidentally be trained on
    raw temperatures that will be *simulated* at inference time, which would
    create a train/serve skew.

- **`simulate(future_frame) -> pd.DataFrame`**
  - **Requires** a fitted cascade; a gap-free daily frame carrying calendar
    features and non-null nuclear availability, starting after `train_end_`.
  - **Performs** four guard checks (nuclear present, nuclear non-null, calendar
    features present, horizon after training window), then runs stages 1-4:
    forecast weather; concatenate the warm-up frame in front and recompute
    thermal features across the join before slicing the horizon back out;
    forecast demand; recompute residual demand; drop region columns.
  - **Returns** the simulated design frame indexed to the forecast horizon.
  - **Why the four guards.** Each corresponds to a way a caller can produce
    silently wrong output rather than an error. Missing nuclear values would
    yield `NaN` prices; a missing calendar feature would `KeyError` deep inside
    the load model; an overlapping horizon would produce a duplicated,
    non-monotonic index that breaks the `.loc` slice. Failing early with a
    specific message is the difference between a bug report and a debugging
    session.
  - **Why stitch-then-slice rather than seeding the EWM directly.** Pandas'
    `ewm` has no "initial state" parameter. Concatenating real history,
    recomputing over the join, and discarding the warm-up rows is the
    straightforward way to get a continuous smoothing state.
  - **Why residual demand is recomputed rather than reused.** Its `DEMAND`
    input was just replaced by a simulated value. Reusing the stale
    residual-demand column is exactly the kind of error that produces a model
    that scores well and is wrong.

- **`enriched_train` (property)** exposes the training frame, raising
  `NotFittedError` if unfitted — following the scikit-learn convention so
  misuse fails recognisably.

#### `simulate_exogenous_pipeline(...)`

A thin functional wrapper: fit a cascade, simulate, return both frames.

- **Why it still exists.** It preserves the notebook's original call shape, so
  the migration can be diffed against the source and anyone reading the notebook
  can find the equivalent code. Nothing in the current pipeline calls it.
- **Why the class is preferred.** The function conflates fitting and predicting.
  That is fine for a backtest, which refits per fold by design, but useless for
  serving, where the fitted state must outlive the call. Splitting `fit` from
  `simulate` is the single change that made the API possible.

---

## 6. Validation layer

### `src/models/backtest.py` (208 lines)

**Function flow**

```
Preparation and evaluation are split because the cascade is invariant to the
price model's hyperparameters. 12 cascade fits instead of 600.

prepare_folds(df, target, features, schedule, calendar, hdd_cdd, config)
│
└─ for idx, fold in enumerate(schedule, 1):
     ├─ test_start, test_end   from the schedule entry
     ├─ train_start = test_start - train_window_days   (731)
     ├─ train_end   = test_start - 1 day
     │
     ├─ slice train_data / test_data
     │    └─ either empty? ─► ValueError naming the fold and its date ranges
     │
     ├─ ExogenousCascade(config, ...).fit(train_data)     FRESH per fold
     │    ▲
     │    └─ fold k's test features are simulated by models that have never
     │       seen fold k's data — including the t_norm climatology, the
     │       subtlest available leak
     │
     ├─ simulated_test = cascade.simulate(test_data)
     │
     └─ PreparedFold(index, regime, dates,
                     X_train=enriched_train[features], y_train=...,
                     X_test =simulated_test[features],  y_test =...)
              │
              ▼
        list[PreparedFold]   ── reused by every model AND every Optuna trial
                                so leaderboard comparisons cannot be
                                confounded by fold construction

evaluate_folds(folds, model, model_name, verbose)
│
└─ for fold in folds:
     ├─ fold_model = clone(model)      fresh clone: folds stay independent
     ├─ fold_model.fit(X_train, y_train)
     ├─ preds = fold_model.predict(X_test)
     ├─ mae  = mean_absolute_error()       operational number, spike-robust
     ├─ rmse = root_mean_squared_error()   quadratic penalty, used for tuning
     └─ collect metric row + per-day prediction row
              │
              ▼
     (metrics_df, predictions_df)
        └─ predictions returned too, so residual diagnostics and the
           per-regime table need no refit

run_backtest(...)   convenience wrapper = prepare_folds() then evaluate_folds()
                    kept for one-off runs; the pipeline calls the two directly
```

**Why this file exists.** To evaluate models under conditions matching
production, and to do so efficiently.

#### `class ScikitLearnModel(Protocol)`

A structural type declaring `fit`, `predict`, `get_params`, `set_params`.

- **Why a `Protocol` rather than a base class.** The models passed here are
  heterogeneous — a `TransformedTargetRegressor` wrapping a `Pipeline`, and a
  hand-written `NaiveForecaster`. A `Protocol` types them by shape without
  forcing a shared ancestor. `get_params`/`set_params` are included because
  `clone` and Optuna both need them.

#### `class PreparedFold` (dataclass)

Holds one fold's identity, date bounds, and four ready arrays: `X_train`,
`y_train`, `X_test`, `y_test`.

- **Why a dataclass rather than a tuple or dict.** These objects are passed
  through Optuna trials and evaluation loops; named attributes make the call
  sites readable and typo-proof, and the dates travel with the data for metric
  reporting.

#### `prepare_folds(...) -> list[PreparedFold]`

- **Requires** the static-featured frame, the target and feature names, a
  schedule, the calendar and degree-day column lists, and config.
- **Performs** for each scheduled fold: computes the training window as
  `test_start - train_window_days` to `test_start - 1 day`; slices train and
  test; raises if either is empty; fits a fresh `ExogenousCascade` on the
  training window; simulates the test month; stores the four arrays.
- **Returns** a list of prepared folds.
- **Why this is separated from evaluation — the key optimisation.** The cascade
  depends only on the training window, never on the price model's
  hyperparameters. The notebook refits it inside every Optuna trial: 50 trials ×
  12 folds of weather OLS and Ridge fitting, producing byte-identical results
  every time. Separating preparation from evaluation means the cascade is fitted
  **12 times instead of 600**, with no change to the numbers.
- **Why a fresh cascade per fold rather than one fitted on all history.** This is
  the whole point of the exercise. Fold 3's test month must be simulated from a
  weather model that has never seen fold 3's weather. One globally-fitted cascade
  would leak future information into every fold and inflate every score.
- **Why the empty-fold check raises.** A schedule entry outside the data range
  would otherwise produce a zero-row fold, and `mean()` over nothing is `NaN`,
  which quietly contaminates the leaderboard.
- **Alternative.** `sklearn.model_selection.TimeSeriesSplit`. It generates
  expanding-window splits automatically but cannot express "one calendar month
  per fold with a fixed 731-day rolling window and a human-readable regime
  label" — and the regime labels are what make the per-regime table possible.

#### `evaluate_folds(folds, model, model_name, verbose=True)`

- **Requires** prepared folds and an unfitted estimator.
- **Performs** for each fold: clones the estimator, fits on the fold's training
  arrays, predicts, computes MAE and RMSE; accumulates per-fold metric rows and
  per-day prediction rows.
- **Returns** `(metrics_df, predictions_df)`.
- **Why clone per fold.** Without it, fold 2 would begin from fold 1's fitted
  state — the folds would stop being independent and the results would depend on
  evaluation order.
- **Why both MAE and RMSE.** MAE is the operational number, in EUR/MWh, robust
  to spikes. RMSE penalises large errors quadratically, which is what tuning
  optimises because a trader cares disproportionately about being very wrong.
  Reporting both makes the difference visible.
- **Why predictions are returned and not just metrics.** Residual diagnostics,
  the per-regime table and the actual-vs-predicted plot all need day-level
  output. Recomputing it later would mean refitting.

#### `run_backtest(...)`

Convenience wrapper: `prepare_folds` then `evaluate_folds`. Retained for one-off
interactive runs and because it matches the notebook's original API. The pipeline
does not use it, because it prepares folds once and reuses them across models.

---

## 7. Model layer

### `src/models/registry.py` (213 lines)

**Function flow**

```
build_model_registry(config)
│
├─ _build_baseline_seasonal(config)
│    └─ NaiveForecaster(strategy="seasonal", shift_days=364)
│         ├─ fit    ─► store y history
│         └─ predict ─► y.reindex(X.index - 364d, method="nearest")
│              ▲
│              └─ 364 = 52 weeks, so weekday aligns; 365 would compare
│                 a Tuesday to a Monday
│
├─ _build_elastic_net(config) ─► (estimator, param_space)
│    │
│    └─ TransformedTargetRegressor
│         ├─ regressor = Pipeline[ StandardScaler, ElasticNet ]
│         └─ transformer = RobustArcSinTransformer()
│              ▲
│              └─ refitted inside every fold: no target leakage possible
│
├─ _build_xgboost(config) ─► (estimator, param_space)
│    │
│    └─ TransformedTargetRegressor
│         ├─ regressor = Pipeline[ StandardScaler, XGBRegressor(hist, n_jobs=-1) ]
│         └─ transformer = RobustArcSinTransformer()
│              ▲
│              └─ scaler kept even though trees don't need it, so BOTH models
│                 expose the identical regressor__model__* namespace and the
│                 tuning / SHAP / metadata code needs no branching
│
├─ validate config["models"]["enabled"]
│    └─ unknown name? ─► ValueError listing what is available
│         ▲
│         └─ a typo must fail the run, not silently shrink the benchmark
│
└─ return {name: ModelSpec(name, estimator, requires_tuning, param_space)}
                            in config order

param_space is a CALLABLE, not a dict of ranges
  └─ Optuna's API is imperative (trial.suggest_*), which is what allows
     conditional and nested search spaces later
```

**Why this file exists.** So that adding a candidate model is a local, declarative
change rather than an edit to the training loop. This is the file the project
brief's "trivial to swap in a neural network later" claim actually rests on.

#### `class NaiveForecaster(BaseEstimator, RegressorMixin)`

- **`fit(X, y)`** stores a learned constant for `last`/`mean`/`median`, or the
  full target history for `seasonal`; raises on an unknown strategy.
- **`predict(X)`** returns the constant repeated, or for `seasonal`, the value
  from `shift_days` before each requested date via
  `reindex(..., method="nearest")`.
- **Why a naive baseline is in the registry at all.** It is the honesty check.
  Without it, "MAE 8.68" is a number with no scale. With it, the claim becomes
  "49% better than assuming last year repeats" — and, just as importantly, the
  backtest reveals **two folds where the baseline wins**. A project that cannot
  discover that fact about itself is not measuring anything.
- **Why `shift_days=364` and not 365.** 364 is exactly 52 weeks, so it lands on
  the same weekday. In a market with a strong weekday/weekend split, comparing a
  Tuesday to a Tuesday is the fair naive forecast; 365 would compare a Tuesday to
  a Monday.
- **Why it subclasses sklearn's base classes.** So `clone` works and it can be
  passed through the identical evaluation path as every other model. A special
  case in the loop for the baseline would be a place for the comparison to become
  unfair.
- **Why fitted attributes use a trailing underscore and are set in `fit`, not
  `__init__`.** The sklearn convention: `__init__` takes only hyperparameters,
  which is what makes `get_params`/`clone` behave correctly.

#### `class ModelSpec` (dataclass)

Bundles a name, an unfitted estimator, a `requires_tuning` flag and an optional
Optuna search space.

- **Why the search space is a callable rather than a dict of ranges.** Optuna's
  API is imperative — the space is defined by calling `trial.suggest_*` during a
  trial, which is what permits conditional and nested spaces later. Storing a
  lambda keeps that flexibility.

#### `_build_elastic_net(config)` / `_build_xgboost(config)`

Each returns `(estimator, param_space)` where the estimator is
`TransformedTargetRegressor(regressor=Pipeline([scaler, model]), transformer=RobustArcSinTransformer())`.

- **Why the `TransformedTargetRegressor` wrapper.** It applies arcsinh to `y`
  before fitting and the inverse to predictions, and — critically — fits the
  transformer inside each fold. Doing the transform manually outside the model
  is how target leakage happens.
- **Why scaling inside the pipeline even for XGBoost, which does not need it.**
  Uniformity. Both models then expose the identical
  `regressor__model__*` parameter namespace, so the tuning code, the SHAP
  unwrapping helper and the metadata extraction all work without branching on
  model type. The cost is one redundant transform.
- **Why bounds come from config.** Tuning ranges are an experiment knob. Hard-
  coding them means a search-space change is a code change.
- **Why `tree_method="hist"`.** Substantially faster on small dense data, and
  the pathway that supports GPU if ever needed.
- **Why ElasticNet rather than plain Lasso or Ridge.** It spans both penalties
  through `l1_ratio`, so the tuner can select the behaviour rather than the
  developer. The tuned result — `l1_ratio = 0.98` — is informative: the model
  wants almost pure Lasso, i.e. aggressive feature selection, which is a hint
  that many of the 22 features are not pulling their weight.

#### `build_model_registry(config) -> dict[str, ModelSpec]`

- **Performs** constructs all available specs, validates every name in
  `models.enabled`, returns them in config order.
- **Why it raises on an unknown name rather than skipping it.** A typo in
  `models.enabled` should fail the run, not silently shrink the benchmark and
  produce a leaderboard missing a model nobody notices is absent.

---

### `src/models/train_tune.py` (103 lines)

**Function flow**

```
tune_model(folds, base_estimator, param_space, model_name, config)
│
├─ read models.optuna: n_trials, timeout_seconds, early_stopping_rounds
│
├─ create_study(direction="minimize",
│               sampler=TPESampler(seed=random_state))
│                        ▲
│                        └─ Bayesian and seeded: sample-efficient and
│                           reproducible. GridSearchCV/RandomizedSearchCV
│                           cannot be used at all — neither understands
│                           this fold structure
│
├─ study.optimize(objective, n_trials, timeout,
│                 callbacks=[StudyEarlyStoppingCallback(rounds)])
│    │
│    ├─ objective(trial):
│    │    ├─ params    = param_space(trial)
│    │    ├─ candidate = clone(base_estimator).set_params(**params)
│    │    ├─ evaluate_folds(folds, candidate, verbose=False)   ◄── CACHED folds,
│    │    │                                                        no cascade refit
│    │    └─ return metrics_df["rmse"].mean()
│    │              ▲
│    │              └─ the MEAN across all 12 folds: a compromise, not the
│    │                 best per fold. Measured: the winner is worse than a
│    │                 shallower config on 3 of 12 folds
│    │
│    └─ StudyEarlyStoppingCallback.__call__(study, trial)
│         ├─ best trial changed? ─► reset stagnation counter
│         └─ stagnant >= rounds? ─► study.stop()
│              ▲
│              └─ each trial costs 12 fits; XGBoost stopped at 19/50 trials
│
└─ return ( clone(base_estimator).set_params(**study.best_params),  study )
             ▲                                                      ▲
             │                                                      └─ returned so
             └─ UNFITTED on purpose: tuning picks hyperparameters,      the pipeline
                PriceForecaster.fit decides the training window         can log it
```

#### `class StudyEarlyStoppingCallback`

Tracks the best trial number; increments a stagnation counter when it does not
change; calls `study.stop()` after `early_stopping_rounds` unproductive trials.

- **Why early stopping on the *study*.** Each trial costs 12 model fits. Optuna
  has no built-in study-level patience, and the observed benefit is concrete: the
  XGBoost study stopped at 19 trials instead of 50, cutting tuning time by ~60%
  with no loss in the selected configuration.

#### `tune_model(folds, base_estimator, param_space, model_name, config)`

- **Requires** prepared folds, an unfitted estimator, a search space, config.
- **Performs** defines an objective that clones the estimator, applies the
  trial's parameters, evaluates across the cached folds and returns **mean fold
  RMSE**; runs a TPE-sampled study with a fixed seed, an optional timeout and the
  stagnation callback; prints the best value and parameters.
- **Returns** `(unfitted estimator carrying the best parameters, the study)`.
- **Why the objective is mean fold RMSE and not a single split.** Tuning must
  optimise the same quantity the leaderboard reports, otherwise the selected
  hyperparameters are optimal for something nobody measures.
- **Why RMSE for tuning but MAE for selection.** Deliberate. RMSE's quadratic
  penalty pushes the optimiser away from large errors during search; MAE is the
  more interpretable number for choosing between finished models. Both are
  reported so the choice is auditable.
- **Why it returns an *unfitted* estimator.** Separation of concerns: tuning
  chooses hyperparameters, `PriceForecaster.fit` decides the final training
  window. Returning a fitted model would force tuning to also decide what data
  to fit on.
- **Why the study is returned.** So the pipeline can log the best value, trial
  count and parameters to MLflow without tuning needing to know MLflow exists.
- **Why a seeded `TPESampler`.** Reproducibility. An unseeded sampler makes two
  runs of the same code produce different models.
- **Alternative.** `GridSearchCV`/`RandomizedSearchCV` cannot be used here at
  all — neither understands this custom fold structure. Optuna's TPE is also
  more sample-efficient than random search, which matters when each trial costs
  12 cascade-free fits. Successive halving (`HyperbandPruner`) would help more if
  each trial were expensive enough to prune mid-way.

---

## 8. The served artifact

### `src/models/forecaster.py` (316 lines)

**Function flow**

```
BUILD — called once by the training pipeline, stage 6

PriceForecaster.fit(train_data, price_model, model_name, config,
                    feature_names, calendar_features, hdd_cdd_cols,
                    backtest_mae, backtest_rmse, best_params)
│
├─ ExogenousCascade(config, calendar, hdd_cdd).fit(train_data)
│
├─ fitted_model = clone(price_model)                  clone: the registry's
│    └─ .fit(enriched_train[feature_names], y)        estimator stays reusable
│
└─ ForecasterMetadata(
     model_name, target_col, feature_names,
     train_start, train_end, n_train_days,
     max_horizon_days, backtest_mae, backtest_rmse,
     best_params ◄── passed IN from the Optuna study, because
                     get_params(deep=False) on a TransformedTargetRegressor
                     returns only {'check_inverse': True}
   )

The bundle now holds six pieces of fitted state — all six required:
   1 weather coefficients      4 price model + arcsinh median_/mad_
   2 t_norm climatology        5 feature ORDERING
   3 Ridge load model          6 warm-up tail + window bounds
   └─ 4 of the 6 fail SILENTLY if not persisted (2, 4, 5, 6)


FORECAST — every prediction, served or offline

PriceForecaster.forecast(future_exog)
│
├─ normalise index: date_col -> index, to_datetime, sort, name "Date"
│
├─ FutureExogenousSchema.validate()
│
├─ _validate_horizon(index)                    ── raises ──► HTTP 422
│    ├─ len(index) > max_horizon_days ?
│    │    └─ "AR(2) decays to its seasonal mean well before that"
│    └─ index[0] < earliest_forecast_date ?
│         └─ earliest_forecast_date = train_end + 1 day
│              ▲
│              └─ refusing beats returning a plausible number built on a
│                 cold EWM, because nobody would know to distrust it
│
├─ engineer_calendar_features(validated, config)
│    ▲
│    └─ built HERE, not required of the caller: pure function of the dates,
│       and one implementation means train/serve skew is impossible
│
├─ cascade.simulate(with_calendar)             stages 1-4
│
├─ price_model.predict(simulated[feature_names])
│    ▲
│    └─ feature_names, in the stored order
│
├─ attach DIAGNOSTIC_COLUMNS + demand + nuclear
│    T_lisse, Delta_T, T_lisse_HDD, T_lisse_CDD, Residual_Demand
│
└─ target column present in the input?  ─► attach actual_price
     ▲
     └─ lets the same code path serve live forecasts and score historical ones

design_matrix(future_exog)        same path, stops before predict  (for SHAP)
training_design_matrix (property) enriched_train[feature_names]    (SHAP background)


MLFLOW WRAPPER

PriceForecasterModel(mlflow.pyfunc.PythonModel)
│
├─ predict(context, model_input, params)
│    ├─ _to_exog_frame():  "date" -> "Date",  "nuclear_avail" -> "NUCLEAR AVAIL. (MW)"
│    │     ▲
│    │     └─ the internal name has spaces, a period and parentheses; forcing
│    │        that through a JSON contract would be hostile
│    ├─ forecaster.forecast(exog)
│    └─ .reset_index()      pyfunc is DataFrame-in / DataFrame-out
│
├─ load_context(context) ─► None    the forecaster travels inside the pickle
└─ model_info()          ─► metadata dict, becomes GET /model-info
```

**Why this file exists.** Because the model artifact cannot be a pickled
regressor. Prediction requires the entire fitted cascade, so "the model" is a
composite object, and this file defines it.

#### `class ForecasterMetadata` (dataclass)

Model name, target, feature names, training window bounds, day count, max
horizon, backtest metrics, best parameters, plus `to_dict()`.

- **Why metadata is a first-class part of the artifact.** It answers "which model
  produced this number, trained on what data, scoring what in backtest" from the
  artifact itself. Provenance in a wiki drifts from reality; provenance in the
  artifact cannot. It is what `/model-info` returns.

#### `class PriceForecaster`

- **`fit(train_data, price_model, model_name, config, feature_names, calendar_features, hdd_cdd_cols, backtest_mae=None, backtest_rmse=None, best_params=None)`** (classmethod)
  - **Performs** fits an `ExogenousCascade` on the window; clones the price model
    and fits it on the enriched training frame restricted to `feature_names`;
    assembles metadata.
  - **Returns** a fitted `PriceForecaster`.
  - **Why a classmethod constructor.** It expresses that an instance is only ever
    meaningful when fitted. There is no valid partially-constructed state.
  - **Why the price model is cloned.** The estimator handed in came from the
    registry and may be reused; fitting it in place would mutate shared state.
  - **Why `best_params` is passed in rather than read from the model.**
    `get_params(deep=False)` on a `TransformedTargetRegressor` returns only
    wrapper arguments — it reported `{'check_inverse': True}` and none of the
    tuned values. The tuned parameters come from the Optuna study, so the caller
    supplies them; the fallback filters `get_params(deep=True)` for the
    `regressor__model__` namespace.

- **`train_end` / `earliest_forecast_date` (properties)** — the last observed day
  and the first forecastable day. Trivial, but they give the horizon guard a
  vocabulary and the API a value to report.

- **`_validate_horizon(index)`** raises if the horizon exceeds
  `max_horizon_days` or starts before `earliest_forecast_date`.
  - **Why refusing service is the correct behaviour.** Beyond ~31 days the
    weather AR term has decayed to climatology, so the forecast carries no real
    information; before `train_end + 1` the warm-up stitch is invalid. Returning
    a plausible-looking number in either case is worse than an error, because
    nobody would know to distrust it. The error text explains *why*, not just
    that.

- **`forecast(future_exog) -> pd.DataFrame`**
  - **Requires** a frame with a date column or `DatetimeIndex` and nuclear
    availability.
  - **Performs** normalises the index, validates against
    `FutureExogenousSchema`, checks the horizon, builds calendar features, runs
    the cascade, predicts, then attaches the simulated drivers and — when the
    period happens to be historical — the actuals.
  - **Returns** a frame with `predicted_price` plus `T_lisse`, `Delta_T`,
    degree days, residual demand, demand and nuclear.
  - **Why calendar features are built here rather than required of the caller.**
    They are a pure function of the dates. Making an API client compute 13
    calendar flags — including French bridge-day logic — would be an absurd
    contract and a guaranteed source of train/serve skew.
  - **Why the simulated drivers are returned.** A forecast that arrives with its
    assumed temperature and demand can be interrogated; a bare number can only be
    trusted or ignored. It also makes the weekend demand drop visible in the
    output, which is free evidence the cascade is behaving.
  - **Why actuals are attached opportunistically.** It lets the same code path
    both serve live forecasts and score historical ones, without a separate
    evaluation mode.

- **`design_matrix(future_exog)` / `training_design_matrix` (property)** expose
  the simulated and training feature matrices for SHAP, which must explain the
  features the model actually saw. Without these the explainability layer would
  have to re-derive them and could drift.

#### `class PriceForecasterModel(mlflow.pyfunc.PythonModel)`

- **`__init__(forecaster=None)`** holds the fitted forecaster.
- **`load_context(context)`** returns `None` — the forecaster travels inside the
  pickled object, so there is nothing extra to load. Defined for interface
  completeness and to document that fact.
- **`_to_exog_frame(model_input)`** renames the JSON-friendly `date` and
  `nuclear_avail` columns to the internal `Date` and `NUCLEAR AVAIL. (MW)`.
  - **Why an external naming convention at all.** The internal column name
    contains spaces, a period and parentheses. Forcing that through a JSON API
    contract would be hostile; this adapter is the translation boundary.
- **`predict(context, model_input, params=None)`** delegates to
  `forecaster.forecast` and returns the result with the date as a column.
  - **Why `reset_index()`.** MLflow's pyfunc contract is DataFrame-in,
    DataFrame-out and does not preserve a meaningful index across serialisation.
- **`model_info()`** returns the metadata dict.
- **Why a pyfunc wrapper rather than logging the `PriceForecaster` directly.**
  `pyfunc` is MLflow's universal interface. Anything logged as pyfunc can be
  loaded by `mlflow.pyfunc.load_model`, served by `mlflow models serve`,
  scored in Spark, or deployed to SageMaker — without the consumer knowing what
  is inside. `mlflow.sklearn` would not work here because the object is not a
  scikit-learn estimator.

---

## 9. Analysis layer

### `src/analysis/evaluate.py` (261 lines)

**Function flow**

```
matplotlib.use("Agg")  at import, BEFORE pyplot is imported
  └─ no display in a container; every plot writes a file and returns its path,
     because the consumer is mlflow.log_artifact()

build_leaderboard(metrics_df, selection_metric)
  └─ groupby("model").agg( mae, rmse, mae_std, rmse_std, worst_fold_mae, n_folds )
       └─ sort_values(selection_metric)
            ▲
            └─ std and worst-fold included on purpose: a model that is good on
               average and catastrophic once is not deployable, and the mean
               hides that

select_best_model(leaderboard, metric) ─► leaderboard.iloc[0]["model"]
  └─ deliberately trivial: selection should be one auditable line
     ⚠ there is no GATE — nothing can reject a model (MLOPS.md step 7)

summarize_by_regime(metrics_df)
  └─ pivot_table(index="regime", columns="model", values="mae")
       ▲
       └─ this is what revealed BOTH that XGBoost fixes the COVID fold (5.35)
          and that the naive baseline beats it on Lockdown Easing (5.63)

plot_residual_diagnostics(results_df, model_name, output_dir) ─► path
  ├─ [0,0] predicted vs actual + y=x line      calibration
  ├─ [0,1] residuals over time                 regime drift
  ├─ [1,0] residual histogram + KDE            bias, fat tails
  └─ [1,1] residuals by day of week            unmodelled weekly cycle

plot_backtest_predictions(results_df, model_name, output_dir) ─► path
  └─ groupby("fold") and plot each SEPARATELY
       ▲
       └─ folds are non-contiguous months; one continuous line would draw a
          misleading segment across the gaps

plot_forecast(forecast_df, output_dir, filename) ─► path
  └─ overlays actual_price only if the column exists and has values

metrics_to_mlflow_dict(row, prefix)         ─► flat scalars for log_metrics
per_regime_metrics_to_mlflow_dict(df, name) ─► {"fold_01_mae": ...}
  ▲
  └─ keyed by fold INDEX, not regime name: MLflow rejects metric names with
     spaces, slashes and parentheses — which every regime label has. The
     labels live in the logged CSV instead

print_leaderboard(leaderboard, metric)      ─► stdout, notebook format
```

**Why `matplotlib.use("Agg")` at import time.** There is no display in a
container. Setting the non-interactive backend before `pyplot` is imported is the
only reliable way to prevent a backend error, and it must therefore precede the
`pyplot` import in the file.

**Why every plot function writes a file and returns its path** rather than
calling `plt.show()`. The consumer is MLflow's `log_artifact`, which needs a
path. It also makes the functions testable and usable from a scheduled job.

- **`build_leaderboard(metrics_df, selection_metric="mae")`** aggregates per-fold
  rows into one row per model with mean MAE/RMSE, their standard deviations, the
  worst fold and a fold count. Standard deviation and worst-fold are included
  because a model that is good on average and catastrophic once is not a model
  you deploy — the mean alone hides that.
- **`select_best_model(leaderboard, selection_metric)`** returns the leading
  model's name. Trivial by design: selection should be one auditable line, not a
  judgement embedded in the pipeline.
- **`summarize_by_regime(metrics_df)`** pivots fold MAE into regime × model.
  **Why this exists:** it is what revealed both that XGBoost fixes the COVID
  collapse fold and that the naive baseline wins Lockdown Easing. A single mean
  would have hidden both.
- **`plot_residual_diagnostics(...)`** writes four panels — predicted vs actual,
  residuals over time, residual distribution, residuals by weekday. Each answers
  a distinct question: calibration, regime drift, bias and fat tails, and
  unmodelled weekly structure.
- **`plot_backtest_predictions(...)`** plots actual and predicted per fold
  *separately*. **Why:** the folds are non-contiguous months, so a single
  continuous line would draw a misleading segment across the gaps between them.
- **`metrics_to_mlflow_dict` / `per_regime_metrics_to_mlflow_dict`** flatten
  results into scalar metrics MLflow accepts. **Why the second one keys folds by
  index rather than regime name:** MLflow rejects metric names containing spaces,
  slashes and parentheses, which every regime label has. The labels live in the
  logged CSV instead.

### `src/analysis/explainability.py` (259 lines)

**Function flow**

```
explain_predictions_shap(model, X_train, X_test, output_dir,
                         explainer_type, background_samples, random_state)
│
├─ explainer_type valid?  ── no ──► ValueError
│
├─ "model_agnostic" or "both" ─► explain_model_agnostic(...)
│    ├─ background = X_train.sample(n, random_state)
│    │     ▲
│    │     └─ pandas .sample, NOT shap.sample — the latter seeds NumPy's
│    │        global RNG and trips a FutureWarning
│    ├─ with _suppress_shap_rng_warning():
│    │     ├─ shap.Explainer(model.predict, background)   permutation explainer
│    │     └─ shap_values = explainer(X_test)
│    │          ▲
│    │          └─ explains the FULL pipeline including the inverse arcsinh,
│    │             so contributions are in EUR/MWh — readable by a trader.
│    │             Slow. This is the view that surfaced the `year` problem
│    ├─ _save_summary_plot()    beeswarm
│    └─ _save_waterfall_plot()  single prediction breakdown
│
└─ "model_specific" or "both" ─► explain_model_specific(...)
     ├─ _unwrap_pipeline(model)
     │    └─ TransformedTargetRegressor.regressor_ -> Pipeline.named_steps
     │         └─ returns (scaler, algorithm)  or (None, model) if no match
     │              └─ None ─► skip, don't crash    (the naive baseline)
     ├─ scale X_train / X_test with the model's own scaler
     ├─ route by algorithm type
     │    ├─ XGBRegressor                     ─► TreeExplainer     exact, fast
     │    ├─ ElasticNet/LinearRegression/...  ─► LinearExplainer   exact, fast
     │    └─ anything else                    ─► generic Explainer
     │         ▲
     │         └─ arcsinh space: not economically meaningful, but this is the
     │            view you debug with
     └─ save summary + waterfall

_suppress_shap_rng_warning()      context manager
  └─ filters ONE third-party FutureWarning by message. shap's
     PermutationExplainer calls np.random.seed unconditionally in its
     constructor and the warning re-emits at plot time; no argument avoids it.
     Filtered narrowly so our own FutureWarnings stay visible

global_importance_table(shap_values, feature_names)
  └─ |values|.mean(axis=0) ─► sorted DataFrame     plot-free ranking, cheap to log
```

- **`_suppress_shap_rng_warning()`** a context manager filtering one specific
  third-party `FutureWarning` by message. **Why narrowly and not globally:**
  shap's `PermutationExplainer` calls `np.random.seed` unconditionally in its
  constructor and the warning re-emits later at plot time; no argument avoids it.
  Filtering by message keeps every *other* `FutureWarning` — including ones from
  our own code — visible.
- **`_unwrap_pipeline(model)`** pierces `TransformedTargetRegressor` → `Pipeline`
  to return `(scaler, algorithm)`, or `(None, model)` when the shape does not
  match. **Why:** the model-specific explainers need the bare algorithm, and the
  naive baseline has no such structure — returning `None` lets the caller skip
  rather than crash.
- **`explain_model_agnostic(...)`** explains the full pipeline through a
  permutation explainer, so contributions are in **EUR/MWh** and include the
  inverse arcsinh. Slow but interpretable by a non-specialist. Background data is
  subsampled with pandas rather than `shap.sample`, which seeds NumPy's global
  RNG.
- **`explain_model_specific(...)`** routes to `TreeExplainer` for XGBoost and
  `LinearExplainer` for linear models, on scaled features in arcsinh space. Exact
  and fast, but the units are not economically meaningful.
- **Why both views.** They answer different questions: the agnostic one is what
  you show a trader, the specific one is what you use to debug the model. The
  `year` finding came from the agnostic view, where 11.53 is directly readable as
  EUR/MWh.
- **`global_importance_table(...)`** reduces a SHAP matrix to mean absolute
  contribution per feature — a plot-free ranking, cheap to log and easy to render
  in a UI.

---

## 10. Orchestration

### `src/pipelines/train_pipeline.py` (473 lines)

**Function flow**

```
run_training_pipeline(config, skip_tuning, skip_shap, register)
│
├─ load_config() ; derive feature_names, hdd_cdd_cols, calendar, schedule
├─ mlflow.set_tracking_uri()
├─ _ensure_experiment(config)
│    └─ experiment absent? ─► create_experiment(name, artifact_location)
│         ▲
│         └─ pinning the location stops a SQLite-backed store writing
│            artifacts relative to whatever cwd launched the process
│
├─ STAGE 1  load_raw_dataset("train") ─► preprocess_data()
├─ STAGE 2  build_static_features() ─► drop_incomplete_rows()
│
├─ STAGE 3  prepare_folds()                       12 cascade fits, ONCE
│
└─ with mlflow.start_run(parent):
     ├─ log_params: schedule, n_folds, window, warmup, feature_set,
     │              n_features, selection_metric, target, hdd_cdd_reference
     │
     ├─ STAGE 4  for name, spec in build_model_registry(config).items():
     │             └─ with mlflow.start_run(nested=True):
     │                  ├─ spec.requires_tuning and not skip_tuning ?
     │                  │    └─ tune_model(folds, ...) ─► (estimator, study)
     │                  │         ├─ log_params(study.best_params)
     │                  │         └─ log_metrics(tuning_best_rmse, n_trials)
     │                  ├─ evaluate_folds(folds, estimator, name)
     │                  ├─ log_metrics: mae, rmse, mae_std, worst_fold_mae
     │                  ├─ log_metrics: fold_01_mae .. fold_12_mae
     │                  └─ log_artifact(fold_metrics_{name}.csv)
     │                       ▲
     │                       └─ nested so each model gets a clean parameter
     │                          namespace — `alpha` means different things
     │                          to different models
     │
     ├─ STAGE 5  build_leaderboard() ─► print_leaderboard()
     │             ├─ summarize_by_regime()
     │             ├─ select_best_model()          ⚠ no gate: leader is taken
     │             ├─ log_param(best_model) ; log_metrics(best_* prefix)
     │             └─ log_artifact: leaderboard.csv, metrics_by_regime.csv,
     │                              backtest_predictions.csv,
     │                              residuals_*.png, backtest_predictions_*.png
     │
     └─ STAGE 6  final fit + register
                 ├─ window = last train_window_days of the data
                 │     ▲
                 │     └─ the backtest establishes that the CONFIG generalises;
                 │        the served model should be as current as the data allows
                 ├─ PriceForecaster.fit(final_train, best_estimator, ...)
                 ├─ log_artifact(model_metadata.json)
                 │
                 ├─ try: holdout forecast
                 │    ├─ load_raw_dataset("predict") ─► prepare_future_exogenous()
                 │    ├─ _slice_holdout(config)        explicit boolean mask,
                 │    │                                not .loc[None:None]
                 │    ├─ forecaster.forecast(future_exog)
                 │    │     ▲
                 │    │     └─ exercises the exact code path the API will use,
                 │    │        on real data, BEFORE anything is registered
                 │    └─ log metric + holdout_forecast.csv/.png
                 │    except (FileNotFoundError, ValueError, KeyError):
                 │         warn only — a missing holdout file must not fail a
                 │         run whose model is otherwise sound
                 │
                 ├─ if not skip_shap and explainability.enabled:
                 │    ├─ explain_predictions_shap(price_model, X_train, X_test)
                 │    ├─ log the 4 plots + shap_importance.csv
                 │    └─ except Exception: warn + print_exc()
                 │         ▲
                 │         └─ bare Exception here because shap is a large
                 │            third-party surface; the traceback is printed so
                 │            the cause stays actionable
                 │
                 └─ mlflow.pyfunc.log_model(
                      python_model=PriceForecasterModel(forecaster),
                      code_paths=["src"],       ships source with the artifact
                      signature, input_example,
                      registered_model_name=... if register else None )

src/train.py   11-line shim ─► train_pipeline.main()
               exists because the Dockerfile CMD references it
```

**Why a separate pipelines package.** Every other module is a library: importable,
side-effect free, independently testable. This is the only module that *does*
things in an order and talks to MLflow. Keeping orchestration in one place means
the libraries stay reusable — the API imports `forecaster` without dragging in
Optuna or MLflow logging.

- **`_ensure_experiment(config)`** creates the experiment with an explicit
  artifact location if absent, then selects it. **Why:** with a SQLite backend and
  no pinned location, MLflow writes artifacts relative to the launching process's
  working directory, scattering them once training and the API run from different
  places.
- **`_slice_holdout(future_exog, config)`** restricts the holdout to the
  configured window, with both bounds optional. Built with an explicit boolean
  mask rather than `.loc[start:end]` because `None` bounds in `.loc` are
  unreliable. **Why bounds are optional:** with neither set the whole prediction
  file is forecast, which is what a live daily feed should do.
- **`_log_dataframe_artifact(df, output_dir, filename)`** writes a CSV and
  returns its path, for `mlflow.log_artifact`.

#### `run_training_pipeline(config=None, skip_tuning=False, skip_shap=False, register=True)`

Six stages, as traced in §1.

- **Why folds are prepared once, before the model loop.** The optimisation from
  §6, and it also guarantees every model is scored on *identical* data — otherwise
  a leaderboard comparison would confound model quality with fold construction.
- **Why the final fit uses the most recent window rather than the fold data.**
  The backtest establishes *how well this configuration generalises*; the served
  model should then be as current as the data allows. It uses the same 731-day
  window length the backtest validated, so the deployed model matches the
  measured one in structure.
- **Why the holdout forecast runs inside training.** It exercises the exact code
  path the API will use, on real data, before anything is registered. A model
  that cannot forecast should not reach the registry.
- **Why the holdout block catches `FileNotFoundError`/`ValueError`/`KeyError`
  but SHAP catches bare `Exception`.** Both are non-essential to a valid model,
  so neither should fail the run — but the holdout's failure modes are known and
  enumerable, whereas SHAP is a large third-party surface with unpredictable
  failures. The SHAP handler prints a full traceback so the cause stays
  actionable.
- **Why the three CLI flags exist.** `--skip-tuning` gives a fast smoke test of
  the whole pipeline (minutes, not an hour); `--skip-shap` skips the slowest
  stage; `--no-register` allows experimentation without polluting the registry.
  Each corresponds to a real development loop.

### `src/train.py`

An eleven-line shim onto `train_pipeline.main`. It exists because the Dockerfile's
`CMD` and any existing habit reference `python src/train.py`. Previously it was a
stale stub reading config keys that no longer existed.

---

## 11. The serving layer

### `src/api/schemas.py` (128 lines)

**Function flow**

```
STARTUP — once per process

lifespan(app)
│
├─ try:
│    ├─ load_config()                             ─► STATE["config"]
│    └─ load_model(config)                        ─► STATE["model"]
│         ├─ mlflow.set_tracking_uri()
│         ├─ resolve_model_uri(config)
│         │    ├─ 1. $MODEL_URI                        operator override
│         │    ├─ 2. models:/<name>@champion           intended production path
│         │    └─ 3. highest registered version        fallback, no alias yet
│         │         └─ none found? ─► ModelLoadError naming the fix
│         ├─ mlflow.pyfunc.load_model(uri)
│         └─ unwrap_python_model().forecaster
│              ▲
│              └─ the inner typed object, not the pyfunc wrapper: inside this
│                 process forecast() returning a real DataFrame is more useful
│
└─ except Exception:  STATE["load_error"] = str(exc)
     ▲
     └─ captured, NOT raised. Crashing gives a restart loop with no way to ask
        what went wrong; starting unhealthy keeps /health able to answer


REQUEST — per call

GET /health                     GET /model-info              GET /backtest-metrics
│                               │                            │
├─ STATE["model"] is None?      ├─ _require_model()          ├─ _require_model()
│    └─ 503 + load_error        ├─ metadata.to_dict()        ├─ read leaderboard.csv
└─ 200 {status, model_loaded,   └─ 200 ModelInfoResponse     ├─ read metrics_by_regime.csv
        model_uri}                                           └─ neither exists? ─► 404

POST /predict
│
├─ ForecastRequest validation                                        api/schemas.py
│    ├─ start_date parses as a date?         ── no ──► 422
│    ├─ nuclear_avail non-empty?             ── no ──► 422
│    └─ _check_nuclear_range()
│         ├─ NaN?                            ── yes ─► 422
│         └─ outside [1_000, 80_000] MW?     ── yes ─► 422 "MW, not GW"
│              ▲
│              └─ the realistic failure is a unit error, which a null check
│                 would not catch: 29 GW as 29 MW ─► nonsense residual demand
│
├─ _require_model()                          ── none ─► 503
│
├─ build frame: DataFrame({nuclear_col: values}, index=date_range(start, horizon))
│    ▲
│    └─ horizon is len(nuclear_avail); carrying it separately would be a
│       redundancy the server must police
│
├─ try: forecaster.forecast(frame)
│    except (ValueError, KeyError) ─► 422 with the forecaster's own message
│         ▲
│         └─ horizon guards are NOT duplicated here: they depend on the loaded
│            model's train_end, which Pydantic cannot know. One source of truth
│
└─ 200 ForecastResponse
     ├─ model_name, train_end, n_days, mean_predicted_price
     └─ forecast: [ DailyForecast per day ]
          date, predicted_price,
          t_lisse, delta_t, t_lisse_hdd, t_lisse_cdd,
          demand_mw, nuclear_avail_mw, residual_demand
               ▲
               └─ the drivers are returned so a forecast can be interrogated;
                  the Saturday demand drop is visible in the response
```

**Why this file exists.** To make the HTTP contract executable. This is the same
philosophy as `data/schema.py` one layer out: pandera validates DataFrames,
Pydantic validates JSON.

Validation is load-bearing here rather than decorative. Nuclear availability is
the *single* genuine input to the exogenous cascade, so a null or a mis-scaled
value would propagate into NaN temperatures, NaN demand and a NaN price that the
service would return with full confidence.

#### `class ForecastRequest`

- **Fields** `start_date: date`, `nuclear_avail: list[float]` (min length 1).
- **Why the horizon is not a field.** It is implied by `len(nuclear_avail)`.
  Carrying both would create a redundancy the server must police, and a mismatch
  between them has no sensible interpretation. One source of truth.
- **Alternative considered.** `horizon: int` plus a scalar
  `nuclear_avail_constant`, which would be convenient for a UI wanting "7 days at
  30 GW". Rejected because it doubles the request shapes to validate; the
  frontend can expand a constant into a list in one line.
- **Alternative deferred.** An optional `temperature_overrides` field, so a caller
  holding a real NWP forecast could bypass cascade stage 1. Genuinely valuable —
  no statistical model beats NWP at short range — but `ExogenousCascade.simulate`
  unconditionally overwrites the region columns, so it needs a cascade change
  first.

#### `_check_nuclear_range` (field validator)

- **Requires** the parsed list of floats.
- **Performs** rejects NaN, and rejects any value outside
  `[NUCLEAR_MIN_MW, NUCLEAR_MAX_MW]` = `[1_000, 80_000]`.
- **Why a *range* check and not just a null check.** The realistic failure is a
  unit error, not a missing value: `29` (GW) instead of `29000` (MW) passes any
  null or positivity check, produces a wildly negative residual demand, and
  yields a confident nonsense price. The observed range across 2015-2020 is
  27.7-61.8 GW against an installed fleet of roughly 61 GW, so a 1 GW floor is
  physically unreachable and therefore a clean tripwire.
- **Why the bounds are wider than observed.** So a plausible future value is not
  rejected. The floor exists to catch unit errors, not to enforce history.
- **Why the message names the units.** `"Check the units - values are expected in
  MW, not GW"` tells the caller how to fix it. An error that only says
  "validation failed" costs someone an hour.
- **Known trade-off.** A total-outage scenario (0 MW) is now unrepresentable. The
  constant is documented so it can be widened if such a stress test is ever
  genuinely wanted; the extrapolation would be far outside training range anyway.

#### `class DailyForecast` / `class ForecastResponse`

- **Performs** shapes one forecast day as the price *plus* the exogenous state
  the cascade assumed: `t_lisse`, `delta_t`, `t_lisse_hdd`, `t_lisse_cdd`,
  `demand_mw`, `nuclear_avail_mw`, `residual_demand`.
- **Why the drivers are returned.** A forecast that arrives with its assumptions
  can be interrogated; a bare number can only be trusted or ignored. It is also
  free evidence the cascade is behaving — the Saturday demand drop is visible in
  the response, and the price follows it.
- **Why `model_config = ConfigDict(protected_namespaces=())`.** Pydantic v2
  reserves the `model_` prefix and warns on fields like `model_name`. The
  namespace is cleared rather than renaming a field the API consumer needs; the
  alternative (`name`) would be less clear at the call site.

#### `class ModelInfoResponse` / `class HealthResponse`

`ModelInfoResponse` mirrors `ForecasterMetadata.to_dict()` plus the resolved
`model_uri` and `earliest_forecast_date`. `HealthResponse` carries
`model_loaded` explicitly, because that is the field that distinguishes liveness
from readiness.

---

### `src/api/model_loader.py` (104 lines)

**Why this file exists.** To separate *which* model to serve from *how* to serve
it. Resolution has three fallbacks and one failure mode, and none of that belongs
in a route handler.

#### `resolve_model_uri(config) -> str`

- **Requires** config; reads the `MODEL_URI` environment variable.
- **Performs** resolution in descending order of explicitness:
  1. `MODEL_URI` env var - an operator override, for pinning a version or serving
     a run-scoped model while debugging.
  2. `models:/<name>@champion` - the intended production path.
  3. The highest registered version number - a fallback so a freshly trained
     project serves something before any alias exists.
- **Returns** a model URI string.
- **Why an alias is preferred over a version number.** Loading
  `@champion` makes promotion a registry operation plus a restart, instead of a
  code change and a rebuild. Rollback is the same operation pointing back.
- **Why the version fallback exists at all.** No alias is set on this project yet
  (`CORRECTIONS.md` / `MLOPS.md` step 9). Without the fallback a correctly
  trained model would be unservable for a purely operational reason. The
  fallback is a convenience; the alias is the path to use in deployment.
- **Why it raises when nothing is found.** An API that boots without a model is
  worse than one that refuses, because it looks healthy. The message names the
  command that fixes it.
- **Alternative.** Read the URI from `config.yaml` rather than the environment.
  Rejected: which model is live is a *deployment* decision, and baking it into a
  committed file means promotion is a commit.

#### `load_model(config) -> LoadedModel`

- **Performs** sets the tracking URI, resolves the model URI, loads the pyfunc
  model, and unwraps the inner `PriceForecaster`.
- **Returns** a `LoadedModel` holding the forecaster and the URI it came from.
- **Why it returns the inner `PriceForecaster`, not the pyfunc wrapper.** The
  wrapper exists to give MLflow a DataFrame-in/DataFrame-out interface. Inside
  this process the typed object is more useful: `forecast()` returns a real
  DataFrame, `metadata` is a dataclass, and `earliest_forecast_date` is a
  property. The API does its own request translation anyway, so the wrapper's
  column renaming would be redundant work.
- **Why the URI is carried alongside.** So `/health` and `/model-info` can report
  exactly what is loaded. "The model is fine" is not an answer; "serving
  `models:/french_spot_price_forecaster/1`" is.

---

### `src/api/main.py` (219 lines)

**Why this file exists.** It is the HTTP boundary and nothing more: translate a
request, call the forecaster, shape a response, map errors to status codes. It
contains no modelling logic, which is why the same forecaster is reachable from a
notebook, a test, or a scheduled job without going through HTTP.

#### `lifespan(app)` (async context manager)

- **Performs** loads config and the model bundle once, before the server accepts
  traffic; captures any failure into `STATE["load_error"]`.
- **Why loading happens at startup, not per request.** This is the structural
  change the whole refactor turns on. Loading per request would add seconds of
  latency, and refitting per request - as the notebook's `forecast_holdout` does -
  would additionally make two identical requests return different numbers.
- **The consequence worth naming.** MLflow becomes a **startup dependency, not a
  runtime dependency**. A running API survives the tracking server being down; a
  *restarting* one does not. That is much better than runtime coupling, but it is
  not zero coupling - containers restart on deploys, crashes, host maintenance
  and OOM kills. Mitigation is to bake the artifact into the image or cache it on
  a volume.
- **Why a load failure is captured rather than raised.** Crashing on boot produces
  a container restart loop with no way to ask the service what went wrong.
  Starting unhealthy keeps `/health` available to report the cause, while
  `/predict` refuses with 503 so no traffic is served from a modelless process.
  **Verified:** with `MODEL_URI=models:/does_not_exist/99` the service starts,
  `/health` returns 503 naming the missing model, and `/predict` returns 503.
- **Why `lifespan` rather than `@app.on_event("startup")`.** The event decorators
  are deprecated in current FastAPI; `lifespan` also gives a shutdown branch in
  the same function.
- **Alternative.** Lazy loading on first request. Rejected: it moves a multi-second
  cost onto an unlucky user and makes the first request after every restart slow
  and unpredictable.

#### `_require_model() -> LoadedModel`

A one-line guard raising 503 with the captured cause. **Why it is a helper rather
than repeated per route:** three endpoints need it, and the failure message
should be identical in all three.

#### `GET /health`

- **Returns** `status`, `model_loaded`, `model_uri`; **503** when no model is
  loaded.
- **Why it asserts the model loaded rather than returning a bare 200.** A service
  that reports healthy without a model is worse than one plainly down, because a
  load balancer or orchestrator will route traffic to it. This endpoint is the
  difference between liveness ("the process is up") and readiness ("the process
  can do its job").

#### `GET /model-info`

- **Returns** the artifact's own metadata plus the resolved URI and
  `earliest_forecast_date`.
- **Why provenance is an endpoint.** It answers "which model produced this number,
  trained on what, scoring what" from the artifact itself. A wiki page drifts; the
  metadata is pickled inside the object that made the prediction and cannot.

#### `POST /predict`

- **Requires** a valid `ForecastRequest`.
- **Performs** builds a single-column DataFrame indexed by a daily
  `date_range`, calls `forecaster.forecast()`, maps the result rows into
  `DailyForecast` objects.
- **Returns** a `ForecastResponse`.
- **Why the horizon and start-date checks are *not* duplicated here.** They depend
  on the loaded model's `train_end` and `max_horizon_days`, which Pydantic cannot
  know at class-definition time. The forecaster already owns those guards and its
  messages explain *why* a request was refused, so they are caught and passed
  through rather than reimplemented. Duplicating them would create two sources of
  truth that could disagree.
- **Why `ValueError` and `KeyError` map to 422 and not 400 or 500.** The request
  parsed correctly - it is well-formed JSON with valid types - but is not one
  this model can answer. That is precisely what 422 Unprocessable Entity means.
  A 400 would suggest malformed syntax; a 500 would suggest a server bug and
  would page someone unnecessarily.
- **Verified error mapping:**

| Request | Status | Message source |
|---|---|---|
| 45-day horizon | 422 | forecaster horizon guard |
| start date inside training window | 422 | forecaster horizon guard |
| `nuclear_avail: [29.0]` | 422 | Pydantic range validator |
| missing `nuclear_avail` | 422 | Pydantic required field |
| `start_date: "not-a-date"` | 422 | Pydantic date parser |
| no model loaded | 503 | `_require_model` |

#### `GET /backtest-metrics`

- **Performs** reads `leaderboard.csv` and `metrics_by_regime.csv` from the
  configured `output_dir`.
- **Why it is served from artifacts rather than recomputed.** A backtest is a
  training-time concern taking minutes; recomputing one inside a request handler
  would be the same category of mistake as refitting per request.
- **Why it is exposed at all.** So the frontend can show *how the model was
  validated*, not only what it predicts - the leaderboard, the naive baseline it
  beats, and the per-regime table. That is the analytical story, and it is the
  difference between a demo that outputs a number and one that makes a case.
- **Known gap.** It reads from the local filesystem, so it assumes the API
  container shares a volume with the training run's output. On separate hosts this
  should read the artifacts from the MLflow run instead.

---

### What the serving layer deliberately does not do

| Not done | Why |
|---|---|
| Authentication | Nginx and the security group are the boundary; the service is not internet-facing on its own port |
| Rate limiting | Single-tenant, single-analyst workload |
| Async handlers | The work is CPU-bound numpy/XGBoost, which `async` does not help; a threadpool worker is the right lever if concurrency ever matters |
| Caching responses | Forecasts are cheap (single-digit ms) and inputs vary |
| Batch prediction endpoint | One request already covers up to 31 days |
| `/explain` (SHAP per request) | Attribution is computed at training time and logged; per-request SHAP would add seconds. Listed as future work in `MLOPS.md` step 15 |
---

## 12. MLflow integration in detail

MLflow appears in exactly two files — `train_pipeline.py` (logging) and
`forecaster.py` (packaging). Every other module is MLflow-agnostic, which is
deliberate: the libraries should not know how their results are recorded.

### 11.1 Backend configuration

```yaml
mlflow:
  tracking_uri: "sqlite:///mlflow.db"
  artifact_location: "mlartifacts"
  experiment_name: "french_spot_price_forecasting"
  registered_model_name: "french_spot_price_forecaster"
```

**Why SQLite and not the file store.** Two reasons, one fatal. MLflow 3.x puts
the filesystem tracking backend in maintenance mode and raises unless an opt-out
env var is set. More importantly, **the file store has never supported the model
registry** — no `registered_model_name`, no versions, no aliases. A database
backend is a hard requirement for the registry, not a preference.

**Why the URI is resolved to an absolute path at config load.** So training, the
MLflow UI and the API share one store regardless of working directory.
`MLFLOW_TRACKING_URI` overrides it for deployment, where it becomes a server URL.

**Production upgrade:** RDS Postgres plus `--default-artifact-root s3://…`.
SQLite is fine while one writer exists; concurrent writers from the API, the UI
and a training job will contend.

### 11.2 Run structure — parent and nested

```
run: training_20260828_154210          ← parent
  ├─ params:  active_schedule, n_folds, train_window_days, warmup_days,
  │           feature_set, n_features, selection_metric, target,
  │           hdd_cdd_reference, best_model
  ├─ metrics: best_mae, best_rmse, best_mae_std, best_worst_fold_mae,
  │           holdout_mean_predicted_price
  ├─ artifacts: leaderboard.csv, metrics_by_regime.csv,
  │             backtest_predictions.csv, model_metadata.json,
  │             residuals_XGBoost.png, backtest_predictions_XGBoost.png,
  │             holdout_forecast.csv, holdout_forecast.png,
  │             shap_summary_agnostic.png, shap_waterfall_agnostic.png,
  │             shap_summary_specific.png, shap_waterfall_specific.png,
  │             shap_importance.csv
  ├─ model:   price_forecaster  (pyfunc)  → registry v1
  │
  ├─ nested run: Baseline_Seasonal
  │    └─ metrics: mae, rmse, mae_std, worst_fold_mae, fold_01_mae … fold_12_mae
  │       artifacts: fold_metrics_Baseline_Seasonal.csv
  ├─ nested run: ElasticNet
  │    └─ params: regressor__model__alpha, regressor__model__l1_ratio
  │       metrics: + tuning_best_rmse, tuning_n_trials
  └─ nested run: XGBoost
       └─ params: max_depth, n_estimators, learning_rate, subsample,
                  colsample_bytree
          metrics: + tuning_best_rmse, tuning_n_trials
```

**Why nested runs rather than one flat run.** Each model has its own
hyperparameters and its own twelve fold metrics. Flattened into one run, the
parameter names would collide (`alpha` means different things to different
models) and the metric namespace would need manual prefixing. Nesting gives each
model a clean namespace while keeping the comparison grouped, and the MLflow UI
renders the parent-child relationship natively.

**Why the leaderboard metrics are re-logged on the parent with a `best_` prefix.**
So the parent run alone answers "how good was this training run", without
expanding children. It makes runs sortable in the UI.

**Why per-fold metrics are logged individually.** They make regime-level
regression visible across training runs — if a future run degrades only on the
August fold, that is discoverable in the UI rather than buried in a CSV.

### 11.3 Model packaging

```python
logged = mlflow.pyfunc.log_model(
    name="price_forecaster",
    python_model=PriceForecasterModel(forecaster),
    code_paths=["src"],
    signature=signature,
    input_example=example_input,
    registered_model_name="french_spot_price_forecaster",
)
```

| Argument | Why |
|---|---|
| `python_model` as an *instance* | The fitted cascade is state, not code. MLflow cloudpickles the instance, so the weather coefficients, climatology, Ridge model, price model and warm-up frame all travel with it. |
| `code_paths=["src"]` | Unpickling needs the class definitions — `ExogenousCascade`, `RobustArcSinTransformer`, `PriceForecaster`. Shipping `src/` inside the artifact means the loading process needs nothing on its `PYTHONPATH`. This is what makes the artifact genuinely self-contained. |
| `signature` | Inferred from a real input/output pair. MLflow then validates request schemas at serving time, turning a malformed request into a clear error rather than a confusing traceback. |
| `input_example` | Stored with the artifact as executable documentation, and silences MLflow's warning that a signature without an example may be wrong. |
| `registered_model_name` | Creates or increments a registry version in one call. |

**The trap `code_paths` creates.** It prepends the artifact's copy of `src/` to
`sys.path`, so a served process importing `src.utils.config_loader` gets the
*artifact's* module, whose `__file__` is inside the artifact directory. That is
what broke `PROJECT_ROOT` (§2). Two mitigations are in place: root discovery no
longer depends on `__file__` alone, and the serving path never calls
`load_config()` because the resolved config dict is pickled into the forecaster.

**Verified round-trip.** A fresh interpreter, launched from an unrelated working
directory, loads the registered model and predicts — including the full simulated
exogenous trajectory. This is the check that the artifact is actually portable
rather than merely logged.

### 11.4 What the registry buys

- **Versioning.** `models:/french_spot_price_forecaster/1` is immutable. A
  retrain produces version 2; version 1 remains loadable, which is what makes
  rollback a one-line change.
- **Deployment indirection.** The API should load
  `models:/french_spot_price_forecaster@champion` — an alias — so promoting a
  model is a registry operation plus a restart, not a rebuild.
- **Lineage.** Every version links to the run that produced it, and therefore to
  its parameters, metrics, SHAP plots and fold-level results.

### 11.5 What is deliberately not done

- **No automatic promotion.** The pipeline registers a version but does not alias
  it as champion. A model should not reach production because its MAE improved on
  a backtest; regime-level results and SHAP attribution deserve a human look —
  the `year` finding is precisely why.
- **No model signature enforcement mode.** Signatures are logged but not enforced
  as strict, so an extra column in a request is tolerated.
- **No dataset logging.** `mlflow.log_input` with a dataset digest would tie each
  run to the exact data version. Worth adding when the data starts changing
  daily; currently the input is static.
- **No nested run per fold.** Considered and rejected: 3 models × 12 folds × 50
  trials would create thousands of runs and make the UI unusable. Fold metrics
  are logged as metrics on the model's run instead.

---

## 13. Cross-cutting decisions

| Decision | Rationale | Alternative not taken |
|---|---|---|
| Config-driven everything | Experiments become config diffs, not code diffs; the same code runs locally and in a container | Hard-coded constants, or CLI arguments for everything |
| Three pandera schemas | Training and inference have genuinely different completeness obligations | One schema (made the predict path impossible) |
| `fit`/`simulate` split on the cascade | Fitted state must outlive the call to be servable | The notebook's single function that fits and predicts together |
| Folds prepared once, reused | The cascade does not depend on hyperparameters; 12 fits instead of 600 | Refit per trial (the notebook's approach) |
| Naive baseline retained in the registry | Gives every metric a scale, and exposes folds where the models lose | Drop it once the real models work |
| Composite artifact via pyfunc | Prediction needs the whole cascade, not just the regressor | `mlflow.sklearn` on the price model alone — would not run |
| Fitting moved out of the request path | Determinism and latency; two identical requests must agree | Refit on request, as the notebook does |
| Horizon guards that raise | A plausible-looking wrong number is worse than an error | Clamp silently to the maximum horizon |
| Plots written to disk, never shown | Headless containers; MLflow needs paths | `plt.show()` |
| `year` left in place | Removing it changes results; it is the user's modelling call | Silently drop it |

### The three most defensible criticisms of these internals

1. **No tests.** Every "why" above is an assertion about behaviour, and none of
   them is currently enforced. The stage-ordering requirement, the horizon
   guards, and a fold-leakage assertion are the three that would pay for
   themselves immediately.
2. **Config is untyped.** A typo in a config key surfaces as a `KeyError` deep
   inside a stage rather than at load time. A Pydantic settings model would move
   that failure to startup.
3. **The cascade's sub-models are not sklearn transformers.** Making
   `WeatherForecaster` and the thermal block conform to the transformer API would
   let the entire cascade be a `Pipeline`, which would give persistence,
   composability and `clone` for free — at the cost of fighting the
   array-in/array-out contract for multi-column named output.
