# Porting the Forecaster to Japan (JEPX)

How to reuse this project's core for Japanese wholesale power prices, on a zero
budget, and turn it into a live daily forecast.

Companion to `DESIGN.md`, `ASSESSMENT.md` and `AWS_S3_EC2.md`.

---

## 0. First, the name

It is **JEPX** — Japan Electric Power Exchange (日本卸電力取引所). Not JPEX.
Worth fixing before any interview in Japan; JPEX reads the way "EPX" instead of
"EPEX" would to a European desk.

---

## 1. What actually changes

The French model and the Japanese market differ in four structural ways. Only
the first two require real design work.

| | France (EPEX, current) | Japan (JEPX, target) |
|---|---|---|
| **Granularity** | 1 price per day | **48 half-hourly periods per day** |
| **Geography** | 1 national price | **System Price + 9 area prices** |
| Currency / unit | EUR/MWh | **JPY/kWh** (÷1000 scale; 10 JPY/kWh ≈ 62 EUR/MWh) |
| Price floor | negative allowed (−10 observed) | **floored at 0.01 JPY/kWh** — no negatives |
| Dominant fuel | nuclear (≈70% of generation) | **LNG + coal**, nuclear partially restarted |
| Renewables | modest | **very large solar PV**, with curtailment |
| Gate closure | — | **10:00 JST, day-ahead** |

### The two that matter

**48 periods per day.** This is the biggest change and the most interesting
modelling decision. Three approaches, and the literature supports comparing
them:

| Approach | Pros | Cons |
|---|---|---|
| One model, `period_id` as a feature | Simple, one artifact, shares data across periods | Assumes one functional form fits 03:00 and 18:00 |
| **48 separate models** (one per period) | Standard in electricity price forecasting; each period has its own dynamics | 48 artifacts to manage; less data per model |
| Multi-output (predict all 48 at once) | Captures the intraday shape jointly | Harder to tune; scikit-learn support is thin |

Start with the first (cheapest port), then benchmark against the second. "I
tried both and here is the trade-off" is a stronger interview answer than
either alone.

**A floored, spiky target.** JEPX has a minimum price of 0.01 JPY/kWh and, in
Kyushu especially, prices **sit at that floor during midday solar oversupply**.
Meanwhile the January 2021 crisis saw prices reach ~251 JPY/kWh.

So the target is **floor-censored and extremely right-skewed** — a different
problem from France's mildly negative, mildly skewed prices. That opens a
genuinely sophisticated option:

> **A two-stage model:** a classifier for `P(price = floor)`, and a regression
> for the price given that it is above the floor.

That is a defensible, Japan-specific modelling choice with a clear physical
story (solar oversupply drives the floor), and it is the kind of thing that
distinguishes a candidate who understands the market from one who ran XGBoost on
a new CSV.

---

## 2. What you can reuse — roughly 70% of the codebase

This is the point of having built it the way it is built.

| Module | Verdict | What changes |
|---|---|---|
| `utils/config_loader.py` | ✅ **Reuse as-is** | New values in `config.yaml`, no code change |
| `data/data_loader.py` | 🔧 **Extend** | Add an HTTP-fetch branch for the Japanese sources |
| `data/schema.py` | 🔄 **Rewrite contents** | Same three-schema *pattern*, new columns |
| `data/preprocess.py` | 🔧 **Small change** | `freq: "D"` → `"30min"`; the dedupe/reindex/interpolate logic is unchanged |
| `features/build_features.py` | 🔧 **Extend** | `holidays.Japan` already exists in the same library; degree days unchanged; add period-of-day features |
| `models/simulate_exogenous.py` | 🔄 **Cascade shrinks** | **`WeatherForecaster` is deleted** — JMA gives you a real 7-day forecast. `LoadForecaster` reused. Residual demand gains solar and wind |
| `models/backtest.py` | ✅ **Reuse as-is** | Walk-forward is market-agnostic. Fold schedule moves to config |
| `models/registry.py` | ✅ **Reuse as-is** | Same models, new bounds |
| `models/train_tune.py` | ✅ **Reuse as-is** | Nothing market-specific |
| `models/forecaster.py` | 🔧 **Adapt** | Same six-component bundle; fewer components (no weather model) |
| `analysis/evaluate.py` | ✅ **Reuse as-is** | Add pinball loss if you go probabilistic |
| `analysis/explainability.py` | ✅ **Reuse as-is** | Nothing market-specific |
| `api/` | 🔧 **Adapt schemas** | Same four endpoints, Japanese request fields |
| MLflow integration | ✅ **Reuse as-is** | — |

**The cascade gets simpler and better.** Stage 1 — the Fourier+AR(2)
`WeatherForecaster` with its measured 3.5 °C error — is replaced by an actual
numerical weather prediction from JMA. That was listed as the single biggest
improvement in `ASSESSMENT.md` §3.4, and in Japan it is free.

**Residual demand becomes physically correct.** France gave you only
`load − nuclear`. Japan gives you the full generation mix, so:

```
Residual_Demand = Area Demand
                − Nuclear
                − Solar PV Output
                − Wind Power Output
                − Hydroelectric
                − Geothermal − Biomass
```

That is the actual quantity that must be met by price-setting thermal plant —
the definition `ASSESSMENT.md` said was missing.

---

## 3. Free data — all verified live

Everything below was fetched and confirmed working. No API keys, no registration.

### 3.1 Prices, demand, generation mix — `japanesepower.org`

A free Japanese electricity market data hub. Direct CSV URLs, usable straight
from pandas.

| Dataset | URL | Granularity | Coverage | Size |
|---|---|---|---|---|
| **JEPX spot, all history** | `japanesepower.org/jepxSpot.csv` | 30-min | 2011–present | 38.6 MB |
| JEPX spot, per year | `japanesepower.org/spot_2025.csv` | 30-min | one year | 2.6 MB |
| **Regional demand + generation mix** | `japanesepower.org/{Region}HalfHourlyData.csv` | 30-min | 2024-04 → | ~4.3 MB |
| Historical demand | `japanesepower.org/demand.csv` | hourly | → 2024-03 | 4.5 MB |
| **Daily weather, 9 cities** | `japanesepower.org/weatherData.csv` | daily | **1995 →** | 1.1 MB |
| Intraday spot | `japanesepower.org/intradaySpotPrice.csv` | 30-min | historical | — |
| Interconnector flows | `japanesepower.org/interconnector_flows_5min_From_1Apr2025.csv` | 5-min | 2025-04 → | — |
| **JEPX bid/offer curves** | `bidOfferCurveJEPX_2025H1.parquet` | 30-min | 2025 → | Parquet |

**Verified schemas:**

```
spot_2025.csv
  datetime, Date, PeriodID, System Price Yen/kWh,
  Hokkaido, Tohoku, Tokyo, Chuubu, Hokuriku, Kansai, Chuugoku, Kyushu, Shikoku (Yen/kWh),
  Sell Bid Volume kWh, Buy Bid Volume kWh, Contracted Total Volume kWh,
  Sell/Buy Block Bid + Contracted Volumes

TokyoHalfHourlyData.csv
  Datetime, Date, PeriodID(1 to 48), Area Demand (MW),
  Nuclear, Thermal (LNG), Thermal (Coal), Thermal (Oil), Thermal (Other),
  Hydroelectric, Geothermal, Biomass,
  Solar PV Output, Solar PV Curtailment, Wind Power Output, Wind Power Curtailment,
  Pumped Storage, Battery Storage, Interconnected Lines, Others, Total

weatherData.csv
  DateTime, then TMax/TMin per city with JMA station IDs:
  Sapporo_47412, Sendai_47590, Tokyo_47662, Nagoya_47636, Kanazawa_47605,
  Osaka_47772, Hiroshima_47765, Matsuyama_47887, Fukuoka_47807
```

**The nine weather cities map one-to-one onto the nine price areas.** The
existing weighted-national-temperature code ports directly, one weighting per
area instead of one national weighting.

**Two caveats.**

1. **Licence:** the site states the data is "for informational purposes only and
   not intended for commercial use." Fine for a portfolio project; cite it.
   Cross-check anything load-bearing against the official
   [JEPX download page](https://www.jepx.jp/en/electricpower/market-data/spot/).
2. **Two data regimes.** Prices go back to 2011 and weather to 1995, but the
   **generation mix only starts 2024-04**. So you have ~18 months of rich
   features and 14 years of price + weather. Design decision:
   - **Model A (long history):** price ~ weather + calendar, 2011–present.
     Tests regime robustness including the 2021 spike crisis.
   - **Model B (rich features):** price ~ full residual demand + solar + wind,
     2024-04 onward. Better physics, ~18 months of data.
   Building both and comparing is itself a good piece of analysis.

### 3.2 Weather forecast — JMA undocumented JSON API

This is the piece that makes live forecasting possible, and it is free with no
key:

```
https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json
```

Verified response for Tokyo (`130000`), fetched live:

```
element[0]  3-day detail : weatherCodes, weathers, winds, pops, temps
element[1]  7-day outlook: tempsMax, tempsMin
                         + tempsMaxUpper, tempsMaxLower
                         + tempsMinUpper, tempsMinLower   ◄── uncertainty bounds
                         + reliabilities
```

Two things follow, and both are upgrades over the French project:

1. **Real NWP replaces the statistical weather model** for the first 7 days.
   No statistical model beats NWP at short range — the French project's 3.5 °C
   Fourier+AR error simply disappears.
2. **`tempsMaxUpper` / `tempsMaxLower` give you a free uncertainty input.**
   You can propagate weather uncertainty into price quantiles instead of
   inventing an error distribution. That is a genuinely sophisticated route into
   the probabilistic forecasting that `ASSESSMENT.md` ranks as the top
   differentiator.

Area codes are prefecture-level (`130000` Tokyo, `270000` Osaka, `016000`
Sapporo, …). Map each price area to its representative prefecture.

There is no official JMA API, so treat these endpoints as best-effort: cache
every response to disk on fetch, and never let a failed call break the pipeline.

### 3.3 Official and supplementary sources

| Source | Use |
|---|---|
| [JEPX official](https://www.jepx.jp/en/electricpower/market-data/spot/) | Authoritative prices. **Shift-JIS encoded** — pass `encoding="shift_jis"` |
| [OCCTO](https://www.occto.or.jp/en/) | Official supply-demand data for the 10 areas |
| [ISEP Energy Chart](https://isep-energychart.com/en/) | Renewable share, curated visualisations |
| [Renewable Energy Institute](https://www.renewable-ei.org/en/statistics/electricitymarket/) | Market statistics |
| [Kaggle JEPX dataset](https://www.kaggle.com/datasets/mitsuyasuhoshino/jepx-dayaheadmarket) | 2005–2025 mirror, convenient for a quick start |

---

## 4. What the research says

The JEPX forecasting literature is real and small enough to read properly —
which is an advantage, because you can credibly cite it.

| Paper | Contribution worth knowing |
|---|---|
| [Machine Learning-Based Japanese Spot Market Price Forecasting](https://ieeexplore.ieee.org/document/10496100/) (IEEE, 2024) | ANN vs SVR for JEPX, framed around **PV suppliers making production decisions**. Narrow ANN and linear-kernel SVR performed best |
| [Forecasting Wholesale Electricity Market Prices Considering Bidding Conditions Using Price Sensitivity](https://advanced.onlinelibrary.wiley.com/doi/10.1002/aesr.202400192) (2024) | Uses **JEPX price-sensitivity / bid-curve data** to predict volatility driven by growing solar PV. Directly relevant: the bid/offer curve Parquet files are free |
| [JEPX spot prices forecasting system using GIS](https://www.sciencedirect.com/science/article/pii/S235248472202354X) (CPESE 2022) | Derives explanatory variables from **geospatial** supply-demand information |
| [Day-Ahead Electricity Price Forecasting Using an Adaptive Combination Method in the Japanese Spot Market](https://www.researchgate.net/publication/347266981_Day-Ahead_Electricity_Price_Forecasting_Using_an_Adaptive_Combination_Method_in_the_Japanese_Spot_Market) | **Sparse models** to identify which factors drive dynamic market behaviour |
| [Price Forecasting of JEPX using Time-varying AR Model](https://ieeexplore.ieee.org/document/4441658) (IEEE) | The early baseline: time-varying AR |
| [Analyzing the influence of web search behavior on electricity market prices](https://link.springer.com/article/10.1007/s42001-024-00259-6) (2024) | Search-volume data as a demand proxy — an unusual free feature source |
| [Matsumoto Lab, Institute of Science Tokyo](https://www.matsumoto-lab.isc.ens.isct.ac.jp/en/electricity_spot_price_forecast) | An active Japanese academic group on exactly this problem — worth following, and a name to know |

**The three recurring themes**, and how your project already addresses two:

1. **Solar PV is the dominant driver of JEPX volatility.** Your residual-demand
   framing handles this directly once solar output is subtracted — and the free
   data includes solar *curtailment*, which is the explicit oversupply signal.
2. **Bid-curve / price-sensitivity data materially improves accuracy.** Free
   from 2025 onward as Parquet. This is a differentiating feature almost no
   portfolio project would have.
3. **The general benchmark reference** remains
   [Lago, De Ridder & De Schutter (2021)](https://arxiv.org/pdf/2406.00326) for
   day-ahead EPF methodology — LEAR and DNN as reference models, and the
   **Diebold–Mariano test** for significance, which you have already implemented.

### On production systems

There is no public documentation of the internal forecasting systems used by
Japanese utilities, retailers or trading desks — these are proprietary. What is
visible: JEPX itself publishes price-sensitivity data to help participants
forecast, several Japanese vendors sell forecasting services, and the academic
groups above publish methods. **Do not claim to know what production systems
do.** The honest and stronger framing is: *"here is the published literature,
here is what it identifies as the dominant drivers, and here is how my
architecture handles them."*

---

## 5. Free compute for a live daily product

The forecast must be published **before 10:00 JST gate closure** for next-day
delivery. That is the product: *tomorrow's 48-period price curve, before you have
to bid.*

### The zero-budget stack

| Need | Free option | Limits to respect |
|---|---|---|
| Daily scheduled job | **GitHub Actions** `schedule: cron` | Free and unlimited on **public** repos. ⚠️ see timing warning below |
| Model + data storage | **The git repo itself** | The model bundle is ~800 KB; a year of 30-min prices is a few MB. Git gives you free versioning |
| Experiment tracking | **MLflow, local SQLite, artifacts committed** | Run MLflow inside the Actions job; commit `leaderboard.csv` and the metadata back |
| UI | **Streamlit Community Cloud** | Unlimited public apps, free |
| API | **Hugging Face Spaces** (free CPU: 2 vCPU / 16 GB) | Must be public; sleeps when idle |
| Weather forecast | **JMA JSON** | No key. Cache every response |
| Market data | **japanesepower.org CSV** | Non-commercial use |

**Total cost: zero.** No AWS account needed. Note this is a *different* stack
from the AWS one in `AWS_S3_EC2.md` — that document remains the answer to "how
would you deploy this properly at a company", and this is the answer to "how do
I run it for free". Being able to give both answers is a strength.

### ⚠️ The GitHub Actions timing warning

Free-tier scheduled workflows are **routinely delayed**, commonly 2–4 hours and
occasionally much longer, and **schedules are disabled automatically after 60
days of repository inactivity.**

For a 10:00 JST deadline that matters. Mitigations:

1. **Schedule with a wide margin** — run at 04:00 JST for a 10:00 deadline.
2. **Make the job idempotent** and re-runnable, so a late run still produces a
   valid dated forecast.
3. **Add a second trigger** (`workflow_dispatch`) so you can fire it manually.
4. **If timing genuinely matters**, an always-free VM with real `cron` is more
   reliable — Oracle Cloud's Always Free tier is the usual choice. Verify current
   terms before depending on it.

Be honest about this in an interview: *"free cron is best-effort, so I built the
job to be idempotent and scheduled it six hours early."* That reads as
engineering judgement.

### The daily loop

```
04:00 JST  GitHub Actions wakes
    │
    ├─ fetch JEPX spot up to yesterday        japanesepower.org
    ├─ fetch demand + generation mix          japanesepower.org
    ├─ fetch JMA 7-day forecast per area      jma.go.jp  (cache raw JSON)
    │
    ├─ append to the stored history, validate (pandera)
    │
    ├─ IF a retrain is due (weekly):
    │     └─ run the walk-forward pipeline, log to MLflow, gate, register
    │
    ├─ forecast tomorrow's 48 periods
    │     └─ using the JMA forecast instead of a simulated one
    │
    ├─ score yesterday's forecast against actuals   ◄── the product's credibility
    │     └─ append to a rolling accuracy log
    │
    └─ commit: forecast_YYYY-MM-DD.csv, accuracy_log.csv, plots
              │
              ▼
        Streamlit Cloud redeploys on push ─► the dashboard is current
```

**The scoring step is what makes it a product rather than a demo.** A dashboard
showing *"yesterday we predicted 12.3, actual was 11.8; rolling 30-day MAE
1.9 JPY/kWh"* is worth more than any backtest number, because it is live,
falsifiable, and accumulating. Nobody can dismiss it as overfitting.

---

## 6. Phased plan

### Phase 1 — Port the core (about a week)

1. **Pick one target to start:** the **System Price**, single series, 48 periods
   per day. Area prices come later. This keeps the port to one dimension of
   change.
2. Add an HTTP-fetch branch to `data_loader.py`; cache raw files locally.
3. Rewrite the three pandera schemas for the JEPX columns.
4. `dataset.freq: "D"` → `"30min"`; add `period_id` and its cyclical encodings.
5. Swap `holidays.France` → `holidays.Japan` (same library). Replace the French
   vacation flags with Japanese equivalents: **Golden Week** (late Apr–early
   May), **Obon** (mid-Aug), **New Year** (Dec 29–Jan 3) — the direct analogues
   of the industrial-trough features you already have.
6. Delete `WeatherForecaster`; add a JMA client.
7. Rebuild residual demand with solar and wind subtracted.
8. Run the existing walk-forward backtest unchanged.

### Phase 2 — Make it Japanese, not just ported (about a week)

9. **Two-stage floor model:** classifier for `P(price = 0.01)` + regression
   above it. Report both classification and regression metrics.
10. **Use the 2021 spike crisis as a regime fold.** This is a far better stress
    test than France's COVID fold, and every Japanese energy person knows it.
11. **Add the bid-curve features** from the free Parquet files — the 2024 paper's
    central finding.
12. Compare one-model-with-`period_id` against 48-separate-models.

### Phase 3 — Live product (a few days)

13. GitHub Actions daily workflow, idempotent, scheduled early.
14. Yesterday-vs-actual scoring and a rolling accuracy log.
15. Streamlit Cloud dashboard: tomorrow's 48-period curve, the simulated
    drivers, and the live accuracy record.
16. FastAPI on Hugging Face Spaces if you want a callable endpoint.

### Phase 4 — Depth (ongoing)

17. Probabilistic forecasts, using JMA's `tempsUpper`/`tempsLower` as the
    weather-uncertainty input.
18. Extend to the 9 area prices; model the inter-area spreads, which is what
    interconnector-constraint trading is actually about.
19. Diebold–Mariano against a naive baseline and against LEAR.

---

## 7. Why this is a stronger project than the French version

Worth being explicit, because it is the argument for doing it:

| | France version | Japan version |
|---|---|---|
| Weather input | Simulated, 3.5 °C error | **Real NWP, free, with uncertainty bounds** |
| Residual demand | `load − nuclear` only | **Full generation mix incl. solar and wind** |
| Data recency | ends June 2020 | **live, today** |
| Regime test | COVID | **the 2021 price-spike crisis, and daily solar-driven floor events** |
| Bid-side information | none | **free bid/offer curves** |
| Granularity | 1 value/day | **48 values/day** — real market structure |
| Output | a backtest | **a running daily forecast with a live accuracy record** |
| Local relevance | none in Japan | **the market your interviewer trades** |

The single biggest gain is the last row but one: a project that has been
publishing a forecast every morning for two months, with its own scorecard, is
categorically different from one that reports a backtest number.

---

---

## 8. The research-paper spine (decided, parked until EPEX is finished)

The JEPX work will be structured as a **paper replication plus extension**,
rather than an unanchored port. This section records the decision and the
evidence behind it.

### The paper

**Lago, J., Marcjasz, G., De Schutter, B., Weron, R. (2021).** *Forecasting
day-ahead electricity prices: A review of state-of-the-art algorithms, best
practices and an open-access benchmark.* Applied Energy.
`doi:10.1016/j.apenergy.2021.116983`

Toolbox: [`jeslago/epftoolbox`](https://github.com/jeslago/epftoolbox)

Verified contents of the toolbox:

| Component | Detail |
|---|---|
| Datasets | **5 markets x 6 years**: EPEX-BE, EPEX-FR, EPEX-DE, NordPool, PJM |
| Reference models | **LEAR** (Lasso-Estimated AutoRegressive) and **DNN** |
| Metrics | MAE, sMAPE, MASE |
| Statistical tests | **Diebold-Mariano** and **Giacomini-White** |
| Install | pip-installable; README specifies **Python 3.9-3.11** (TensorFlow constraint) |
| Licence | **AGPL-3.0** |

### Why this paper

1. **It is the field's benchmark, and it ships everything needed to replicate**
   — code, data, reference models and the significance-test protocol. Most EPF
   papers ship none of that.
2. **EPEX-FR is one of its five markets.** The current project already targets
   that market, so the implementation can be validated against *published*
   numbers before Japan is touched. That is a credibility check almost no
   portfolio project has.
3. **No Japanese market is in the benchmark** — nor in the recent
   foundation-model EPF benchmark ([arXiv 2506.08113](https://arxiv.org/pdf/2506.08113),
   which covers DE/LU, AT, BE, FR, NL). **Extending the field-standard benchmark
   to JEPX is therefore a genuine, unpublished contribution.**
4. **The paper explicitly invites the extension.** Its stated rationale for LEAR
   and DNN is that they are *"the best benchmarks to compare new complex EPF
   forecasting methods with."* Adding recurrent and foundation models is the
   intended use of the artifact.

### Alternatives considered and rejected as the spine

| Candidate | Verdict |
|---|---|
| **Weron (2014)**, *Electricity price forecasting: A review of the state-of-the-art* | Almost certainly the most-cited work in EPF, but it is a **survey** — nothing to replicate. Cite for framing only |
| **Lago, De Ridder, De Schutter (2018)**, deep-learning comparison | Highly cited, but **superseded** by the 2021 paper, which adds the benchmark, the code and the test protocol |
| [arXiv 2506.08113](https://arxiv.org/pdf/2506.08113), pre-trained models for EPF | Excellent **secondary** reference for the foundation-model act, but European-only and **no code released** |

*Citation counts above are stated qualitatively on purpose — exact figures were
not verified and should not be quoted without checking.*

### Three-act structure

**Act 1 — Replicate.** Run LEAR and DNN on EPEX-FR through `epftoolbox` and
reproduce the published MAE. *"My implementation matches the reference"* is what
earns trust for everything that follows.

**Act 2 — Extend the benchmark to JEPX.** Add Japan as a sixth market using the
free sources in section 3, following the paper's protocol exactly: same
recalibration scheme, same metrics, same DM/GW tests. This is the contribution.

**Act 3 — Add challengers, judged by the paper's own rules.**

| Tier | Models | Library |
|---|---|---|
| Recurrent | LSTM, GRU | PyTorch, or `neuralforecast` |
| Modern deep | N-BEATS, NHITS, TFT | `neuralforecast` |
| **Foundation (zero-shot)** | Chronos-Bolt / Chronos-2, Moirai-2, TimesFM, TinyTimeMixer | Hugging Face |

### The research question for Act 3

Not "I added foundation models", but:

> **Do time-series foundation models transfer zero-shot to a market
> structurally unlike their training data?**

JEPX is a good test case precisely because it is unusual: a hard price floor at
0.01 JPY/kWh, solar-driven clustering of prices *at* that floor, 48 half-hourly
periods, and Japanese market data almost certainly under-represented in those
models' training corpora. Chronos-2 and Moirai-2 now support covariates, so they
can be compared fairly against LEAR's feature set.

**Expect the foundation models to lose to LEAR.** That is the likely outcome on
feature-rich single-market data, and it is the better story: *"I tested whether
the hype holds on an unseen market with a censored target, and the DM test says
it does not"* is a stronger result than an uncritical win.

### Two practical blockers

1. **Python version.** epftoolbox specifies 3.9-3.11 (TensorFlow); this project
   runs 3.12. Plan: a **separate 3.11 venv for Act 1** to preserve replication
   fidelity, and **reimplement LEAR** (Lasso on a lagged-price feature set —
   genuinely small) for Acts 2-3 so the JEPX pipeline runs on modern Python.
2. **AGPL-3.0.** Vendoring epftoolbox source into this repo would make the repo
   AGPL. Keep it as a **pinned dependency used only by the Act 1 replication
   script** and the question does not arise.

### AWS architecture — unchanged

The topology in `AWS_S3_EC2.md` survives this pivot intact:

| Component | Change |
|---|---|
| S3 prefixes | add `raw/jepx/`, `raw/jma/`, `benchmark/epftoolbox/` |
| EC2 `t3.large` | unchanged for serving |
| MLflow | unchanged; now one run per (market x model) |
| FastAPI / Streamlit | unchanged endpoints; dashboard gains a leaderboard tab |
| **New** | LSTM and foundation-model training wants more CPU -> scheduled **spot `c7i`** that terminates on completion |

**Two free-tier cautions.** The 12-month AWS free tier is `t2.micro`/`t3.micro`
with 1 GiB RAM — **too small for LSTM training or loading a foundation model**.
Those belong on a start-stop spot instance or Colab. And free-tier egress is
limited, so cache downloaded market data in S3 rather than re-fetching it.

### Entry point when this resumes

1. Create a Python 3.11 venv and reproduce **one** epftoolbox result on EPEX-FR.
   A day's work; it either validates the plan or surfaces problems immediately.
2. Port the JEPX loaders (section 6, Phase 1).
3. Get LEAR running on JEPX — the first novel number.
4. Only then start the model ladder.

## Sources

- [JEPX Day-Ahead Market data](https://www.jepx.jp/en/electricpower/market-data/spot/)
- [Japanese Electricity Market Data Hub](https://japanesepower.org/) · [download index](https://japanesepower.org/downloadJapanesePowerMarketData.html)
- [JMA forecast JSON](https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json) (undocumented)
- [OCCTO](https://www.occto.or.jp/en/) · [ISEP Energy Chart](https://isep-energychart.com/en/1084/)
- [CME: Introduction to the Japanese power market](https://www.cmegroup.com/education/articles-and-reports/introduction-to-the-japanese-power-market)
- [A Deep Dive into the JEPX Spot Market](https://note.com/dsharing/n/nb596d7c88b3e?hl=en)
- Papers: [IEEE 10496100](https://ieeexplore.ieee.org/document/10496100/) · [Wiley AESR](https://advanced.onlinelibrary.wiley.com/doi/10.1002/aesr.202400192) · [ScienceDirect CPESE](https://www.sciencedirect.com/science/article/pii/S235248472202354X) · [Adaptive Combination](https://www.researchgate.net/publication/347266981_Day-Ahead_Electricity_Price_Forecasting_Using_an_Adaptive_Combination_Method_in_the_Japanese_Spot_Market) · [Time-varying AR](https://ieeexplore.ieee.org/document/4441658) · [Web search behaviour](https://link.springer.com/article/10.1007/s42001-024-00259-6) · [Matsumoto Lab](https://www.matsumoto-lab.isc.ens.isct.ac.jp/en/electricity_spot_price_forecast) · [Lago et al. review](https://arxiv.org/pdf/2406.00326)
- [GitHub Actions free tier](https://cicdcalculator.com/github-actions-free-tier) · [Streamlit/HF free hosting](https://www.kdnuggets.com/7-best-free-platforms-to-host-machine-learning-models)
