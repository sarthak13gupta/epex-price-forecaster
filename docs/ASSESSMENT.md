# Project Assessment and Roadmap

**An honest appraisal of this codebase against production time-series practice,
and what to build next.**

Companion to `DESIGN.md` (what the system is) and `INTERNALS.md` (how it works).
This document is the opinion layer: where the project stands, where it falls
short of industry practice, and how to prioritise the remaining work.

---

## 1. The headline judgement

**This is well above the median portfolio project, and materially below a
production system.** Both halves of that sentence matter.

The modelling instincts on display are better than the engineering
completeness. The single biggest risk to how this project is received is that it
reads as *config-heavy scaffolding around a small, stale dataset*. Two moves
neutralise that risk, and they matter more than everything else on the roadmap
combined:

1. Finish the service layer, so it is a system rather than a pipeline.
2. Add probabilistic output and crisis-regime data, so it has quantitative
   depth a specialist will respect.

---

## 2. What is genuinely strong

### The exogenous cascade is the real asset

The overwhelming majority of time-series portfolio projects follow one shape:
load a CSV, build lag features, fit XGBoost, report RMSE. They never encounter
the problem that defines applied forecasting — **at forecast time you do not
have your features.**

This project confronts it directly. Twenty of twenty-two features are
unavailable on the forecast date, so a four-stage simulation chain manufactures
them, and every backtest fold refits that chain so the test inputs stay blind.
That is what an energy analytics team actually does, and most candidates have
never had to think about it.

### The domain vocabulary is authentic

`T_lisse` is the term French utilities (RTE, EDF) use for smoothed temperature.
Heating and cooling degree days at 15 and 22 °C. Exponentially-weighted thermal
inertia for building thermal mass. Weighted national temperature with
population-like regional weights. Residual demand as the merit-order driver.
Bastille Day bridge days and the August industrial trough.

Anyone who has worked in European power recognises this immediately as
domain-informed rather than generic. That signal is difficult to fake and rare
in portfolios.

### Validation is done correctly

Rolling-origin cross-validation with 12 folds, per-regime error stratification,
and — critically — a retained naive baseline whose two winning folds are
reported rather than buried. Most projects have no baseline at all, so they
cannot say whether the model is worth anything. Using `train_test_split` on a
time series remains one of the most common errors in the genre, and this project
avoids it.

### The `year` finding is the most valuable thing here

SHAP attribution caught the model leaning on a structural artefact. The finding
was quantified by ablation (9.08 to 13.85 MAE without it), correctly diagnosed
as a generalisation failure rather than leakage, and *not* silently patched.

That sequence — instrument, detect, quantify, diagnose, disclose — is what
separates a modeller from someone who runs scikit-learn. It is the strongest
interview material in the project.

### The arcsinh target transform

Correct and non-obvious. Prices range from -10 to +126 EUR/MWh; a log transform
is undefined on the negative values, so most people clip or drop them, which
destroys exactly the observations that matter. MAD-scaled arcsinh handles the
full range and is fitted inside each fold, so no statistic leaks across the
boundary.

---

## 3. Where it falls short of production practice

These are what an interviewer at a trading desk or TSO would probe.

### 3.1 No price history — the biggest technical gap

In electricity price forecasting, the autoregressive term is the strongest
single predictor. The canonical benchmark models in the field — LEAR, Lasso
Estimated AutoRegressive, per Lago, De Ridder and De Schutter (2021) — are built
almost entirely on lagged prices at t-1, t-2 and t-7.

**This model has none.** Verified: zero price-history features across all 22
inputs.

This is *defensible* at a 31-day horizon, since yesterday's price is genuinely
unavailable when forecasting 30 days out. It is not defensible unstated. And it
is the direct cause of the `year` problem: `year` is the only level information
the model has access to.

### 3.2 A horizon framing problem

The *target* is the day-ahead market's clearing price, which is the correct
product name. The *forecast horizon* is 1 to 31 days. Those are different
claims, and the project currently conflates them.

| | Day-ahead (h=1) | Month-ahead (h=1..31) |
|---|---|---|
| Price lags available | Yes — dominant predictor | No |
| Weather | Real NWP forecast (ECMWF/GFS) | Must be simulated |
| Use case | Trading | Hedging, valuation, capacity planning |
| Expected MAE | Much lower | What this project measures |

What is built is the right-hand column: a fundamental/structural forecast. An
interviewer will ask which one it is, and the answer needs to be immediate.

### 3.3 No probabilistic output

Nobody trades on a point forecast. Production energy forecasting emits quantiles
or full predictive densities; the Global Energy Forecasting Competition has been
probabilistic since 2014, and pinball loss and CRPS are the standard metrics.

"What is the probability of a spike above 100 EUR/MWh" matters more to a trader
than the conditional mean. This is the largest single gap between this project
and the field.

### 3.4 Missing the drivers that set the price level

| Missing driver | Why it matters |
|---|---|
| Gas (TTF), coal (API2), carbon (EUA) | Set the marginal cost of the price-setting plant, and therefore the price *level* between years |
| Wind and solar generation / forecast | The largest short-run drivers of residual demand in modern European markets |
| Cross-border prices and interconnector capacity | France is tightly coupled to DE, BE, ES, IT, CH and UK through market coupling |
| Hydro reservoir levels | Significant in the French and Alpine merit order |

Residual demand here is `load - nuclear` only. The physically correct definition
is `load - wind - solar - nuclear - must-run hydro`.

**Crucially: `year` is proxying for the fuel and carbon complex.** That
reframing is the key to fixing it properly rather than merely deleting it.

### 3.5 Resolution and scale

Daily average price is a research simplification. Real day-ahead markets clear
24 hourly products simultaneously, and the EU is moving to 15-minute settlement.
The intraday shape — morning ramp, evening peak, solar duck curve — is where
most of the interesting structure lives, and averaging it away discards it.

2,007 daily rows is tiny. Hourly multi-year data for a single market is 50,000+
rows.

### 3.6 The data ends in June 2020

The model has never seen the 2021-23 European energy crisis or the 2022 French
nuclear availability collapse, when prices exceeded 500 EUR/MWh. That period is
the single best available regime stress test for exactly this market, and its
absence means robustness is untested where it counts.

### 3.7 Engineering gaps

No automated tests. No CI/CD. No orchestration beyond a cron trigger. No drift
monitoring or retraining triggers. No data or feature versioning. A single model
rather than an ensemble, when forecast averaging is one of the most reliably
beneficial techniques in the electricity-price-forecasting literature.

---

## 4. Roadmap, ranked by signal per unit effort

### Tier 1 — do these, in this order

> **Status, 2026-09-08.** Items 1 and 4 are done. Items 2 and 3 are the live
> priorities and are the two that change the numbers. Full current status in
> `ROADMAP.md`.

**1. Finish the service layer.** FastAPI, Streamlit, Docker Compose. — ✅ **DONE**

An unfinished system is worth far less than a finished smaller one. This was the
right first call: FastAPI (4 endpoints), Streamlit (3 tabs) and three Docker
image targets are all built and verified. It is the difference between "notebook
with good hygiene" and "system".

What remains of this item is not code but *deployment* — none of it has run
anywhere but one laptop. See `ROADMAP.md` §D.

**2. Kill `year` by replacing what it proxies.**

Add a trailing 30-day median price, plus TTF gas and EUA carbon if the data can
be sourced. This converts the project's best finding from "here is a flaw I
found" into "here is a flaw I found, diagnosed, and fixed — with before and
after numbers". A complete story arc, and the most impressive single item
available.

**3. Add quantile forecasts.**

Either LightGBM/XGBoost quantile objectives at P10/P50/P90, or conformal
prediction layered on the existing model. Conformal is cheaper and the
walk-forward folds already provide the calibration set. Report pinball loss and
empirical coverage. Highest differentiation per hour spent, and immediately
legible to anyone in energy.

**4. Tests and CI.** — ✅ **DONE**

All three tests named here exist, plus two more that turned out to matter as
much. 53 tests, ~2 s, no data or credentials required; CI runs them against
**two dependency sets** plus an image build and container smoke test.

- The cascade's stage ordering — `test_stage_ordering.py`
- The horizon guards — `test_horizon_guards.py`
- **A leakage assertion** — `test_fold_leakage.py`, against the real
  `prepare_folds`
- Request unit validation — the 29-GW-as-29 error, which passes every null and
  type check
- Graceful degradation — 503 rather than a bare 200 with no model loaded

Worth noting for the interview version of this story: **the CI design caught a
real latent bug**. Running the suite against the slim serving dependency set
revealed that `registry.py` imported `optuna` at module scope, so a
`Baseline_Seasonal` champion could not have been loaded by the serving image at
all. Details in `CI.md`.

### Tier 2 — genuine quantitative depth

**5. Extend the data to the present and re-run the backtest.**

Sources: ENTSO-E Transparency Platform (free API — load, generation by type,
prices, cross-border flows), RTE éCO2mix, Open Power System Data.

The narrative this unlocks — *"MAE went from 8.7 to X through the 2022 crisis;
here is what broke and here is what I changed"* — is the most senior-sounding
story available from this codebase.

**6. Build both horizons and compare.**

h=1 with price lags and real weather forecasts, against h=1..31 with the
cascade. Cheap to implement, and it demonstrates that *horizon determines
feature availability* — a genuinely senior insight most candidates lack.

**7. Add renewables and interconnector features**, and correct residual demand
to `load - wind - solar - nuclear`.

**8. Move to hourly resolution.** A larger lift, but it transforms realism.

### Tier 3 — polish, and one trap

**9. A neural baseline** — N-BEATS, NHITS or TFT via `neuralforecast`. The
original brief anticipated this.

Frame it honestly. *"I tried it, it lost to tuned XGBoost on 2,000 rows, and
here is why"* is **more** impressive than deploying deep learning uncritically.
Gradient boosting beating deep learning on tabular, small-N problems is the
expected result; knowing that is the signal.

**10. Orchestration** — Dagster or Airflow. Real, but frequently over-claimed on
resumes. Do it last.

---

## 5. How to position this

Avoid *"built an ML model to forecast electricity prices"*. Every candidate
writes that. Write bullets that each carry a number and a judgement:

> - Built a French day-ahead power price forecaster in which **20 of 22 features
>   are unavailable at prediction time**; designed a four-stage exogenous
>   simulation cascade (Fourier+AR(2) weather → thermal inertia → Ridge load →
>   residual demand) to manufacture them, validated blind across 12
>   rolling-origin folds.
> - Tuned XGBoost to **8.68 EUR/MWh MAE, 49% below a seasonal-naive baseline**;
>   independently reproduced a prior ElasticNet result to within 0.04 MAE,
>   confirming the migration preserved model behaviour.
> - Used SHAP to find that `year` carried more attribution than all physical
>   fundamentals combined; diagnosed it as a structural level-proxy that fails
>   across year boundaries and replaced it with a trailing price-level feature.
> - Packaged the full cascade as a versioned MLflow `pyfunc` artifact, moving
>   model fitting out of the request path; served through decoupled
>   FastAPI/Streamlit containers.

The second and third bullets are what earn the interview. The third especially —
it says *I audit my own work.*

### On skill claims

Be careful not to claim MLOps breadth the repository does not support.

| Claimable now | Not yet claimable |
|---|---|
| Experiment tracking (MLflow) | CI/CD |
| Model registry and versioning | Orchestration (Airflow/Dagster) |
| Artifact packaging (`pyfunc`) | Monitoring and drift detection |
| Config-driven pipelines | Feature stores |
| Containerisation | Distributed or streaming data |
| Data contracts (pandera) | Probabilistic forecasting |
| Rolling-origin time-series validation | Deep learning for time series |
| Hyperparameter optimisation (Optuna) | Multi-market / hourly scale |
| Model explainability (SHAP) | A/B testing or shadow deployment |

Tier 1 item 4 and Tier 3 item 10 close two of the right-hand entries. Claiming
them before then is the kind of thing that unravels in a technical screen.

---

## 6. Reference points worth knowing

If asked "how does your approach compare to the literature", these are the
anchors:

- **Weron, R. (2014)** — *Electricity price forecasting: A review of the
  state-of-the-art with a look into the future.* The canonical survey.
- **Lago, De Ridder, De Schutter (2021)** — *Forecasting day-ahead electricity
  prices: A review of state-of-the-art algorithms, best practices and an
  open-access benchmark.* Establishes LEAR and DNN as the reference models
  across six markets, and the Diebold-Mariano test as the standard for
  significance between forecasts.
- **Nowotarski and Weron (2018)** — probabilistic electricity price
  forecasting; why point forecasts are insufficient.
- **GEFCom 2014 / 2017** — the competitions that made probabilistic energy
  forecasting standard practice.

Two concrete gaps against that literature, both already noted above: no
autoregressive price terms, and no significance testing between competing
forecasts. Adding a Diebold-Mariano test between XGBoost and ElasticNet is a
half-day of work and directly addresses the second.
