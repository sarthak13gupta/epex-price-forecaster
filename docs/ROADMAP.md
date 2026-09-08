# Roadmap — what is done, what is left

A single status page. Every other doc describes how something works; this one
describes **what does not exist yet**, why it is ordered the way it is, and
which items are blocked on a decision rather than on effort.

Last reviewed: **2026-09-08**, at commit `e38cc63`.

- [Where the project stands](#where-the-project-stands)
- [Blocked on you, not on effort](#blocked-on-you-not-on-effort)
- [Next: finish the deployment story](#next-finish-the-deployment-story)
- [Then: the modelling work that changes the numbers](#then-the-modelling-work-that-changes-the-numbers)
- [Engineering cleanups](#engineering-cleanups)
- [Measured findings not yet acted on](#measured-findings-not-yet-acted-on)
- [Deliberately parked](#deliberately-parked)
- [What "done" would mean](#what-done-would-mean)

---

## Where the project stands

| Layer | State |
|---|---|
| Data ingestion (local + S3) | ✅ Both paths verified |
| Data contracts | ✅ 3 pandera schemas |
| Features + exogenous cascade | ✅ Built, notebook-faithful |
| Walk-forward validation | ✅ 12 folds, cascade refit per fold |
| Tuning + model selection | ✅ Optuna, fold caching (12 fits, not 600) |
| Served artifact | ✅ pyfunc bundle, loads in a fresh interpreter |
| MLflow tracking + registry | ✅ Model v1 registered |
| FastAPI | ✅ 4 endpoints |
| Streamlit | ✅ 3 tabs |
| Docker | ✅ 3 image targets, all built |
| Tests | ✅ 53, ~2 s |
| CI | ✅ 3 jobs on every push |
| Image publish (ECR) | 🟡 Workflow written, role not created |
| **Deployment to EC2** | ⬜ **Nothing runs on a server yet** |
| Monitoring / drift | ⬜ Not started |
| Continuous training | ⬜ Not started |

The honest one-line summary: **the system is complete and tested, but it has
never run anywhere except this machine.** That gap is the top of this list.

## Blocked on you, not on effort

These four cannot be done by anyone but the account owner. Each is minutes of
work, and everything in the next section waits on them.

| # | Action | Why it needs you |
|---|---|---|
| B1 | `sudo ./infra/host/install-docker-engine.sh` | No passwordless sudo here. Turn off Docker Desktop's WSL integration first |
| B2 | `REPO=sarthak13gupta/epex-price-forecaster ./infra/iam/apply-github-oidc.sh` | Creates an IAM role and OIDC provider. Your pipeline user is S3-scoped and gets `AccessDenied` on all IAM calls — this needs your admin identity |
| B3 | Set repo variables `AWS_ROLE_ARN`, `AWS_REGION`, `ECR_REPOSITORY` | Repository settings. B2 prints the exact values |
| B4 | `POLICY=inference-only ./infra/iam/apply.sh` | Same reason as B2: creating the EC2 instance role is a deliberate admin action |

B1 is independent. B2 → B3 → the publish workflow → B4 → deployment is a chain.

## Next: finish the deployment story

This is the only remaining gap that changes what the project *is* rather than
how good it is.

### D1. Publish an image to ECR

Unblocked by B2 and B3. Then tag a release and the workflow runs:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Verifies the OIDC path end to end and produces the immutable `:serve-<sha>` tag
a deployment should pin to.

### D2. Deploy inference-only to EC2

The decision already taken: **inference only for now**, with the architecture
left able to train on EC2 later. Train locally, register, deploy the artifact.

Steps, roughly half a day:

1. `t3.micro` (free tier), Amazon Linux 2023, with the B4 instance profile
   attached at launch.
2. Security group: 443 open, 22 restricted to your IP. **Never 5000** — MLflow
   has no authentication.
3. `.env` on the instance with `ENV=production` and the bucket name, and
   **without** `AWS_ACCESS_KEY_ID` — boto3's chain checks environment variables
   first, so a stray key means the instance role is never reached.
4. `./infra/iam/verify-on-instance.sh` — proves the role resolves and that no
   static keys shadow it, before anything else is debugged.
5. `docker compose pull && docker compose up -d` against the ECR tag.
6. Nginx terminating TLS on 443, proxying to the loopback-bound containers.

⚠️ **The free-tier constraint is real.** `t3.micro` has **1 GiB RAM** — enough
for the API plus Nginx, not for three containers and a training run. That is
the actual reason the inference-only split was chosen, and it is worth saying
out loud rather than presenting the split as pure design.

### D3. Capture evidence, then tear down

The point of deploying is the evidence, not the uptime. Capture a live
`/health`, a `/predict` response, the Streamlit UI, and `docker compose ps`
from the instance — then destroy it, so an idle instance does not consume
credits.

**`teardown.sh` and `redeploy.sh` do not exist yet and should be written before
D2, not after.** A deployment you cannot cheaply recreate is one you will leave
running.

### D4. Close the delivery loop

`publish.yml` pushes; nothing pulls. An SSM Run Command step — or a
`docker compose pull && up -d` triggered by the workflow — is what makes this
CD rather than "CI plus a manual step".

## Then: the modelling work that changes the numbers

Ordered by signal per hour, and consistent with `ASSESSMENT.md` Tier 1–2.

### M1. Kill `year` by replacing what it proxies

**The highest-value change available.** `year` carries 11.53 EUR/MWh of mean
absolute SHAP — more than every physical fundamental combined — because each
fold trains on ~2 calendar years and tests in the later one, making it a
near-perfect "recent price level" indicator that cannot extrapolate across a
January boundary.

A trailing 30-day median price captures the same signal honestly. `year` lives
in `features.calendar`, so the removal is a one-line config edit; the
replacement feature is the real work.

This converts the project's best finding from *"here is a flaw I found"* into
*"here is a flaw I found, diagnosed and fixed, with before-and-after numbers"* —
which is a materially better story.

**It is deliberately not done yet because it moves every number in `DESIGN.md`.**

### M2. Add quantile forecasts

P10/P50/P90 via XGBoost quantile objectives, or conformal prediction layered on
the existing model. Conformal is cheaper and the walk-forward folds already
provide the calibration set. Report pinball loss and empirical coverage.

A point forecast with no uncertainty is the single most obvious omission to
anyone who trades power.

### M3. Extend the data past June 2020

ENTSO-E Transparency Platform (free API), RTE éCO2mix, Open Power System Data.

Unlocks the most senior-sounding narrative available here: *"MAE went from 8.7
to X through the 2022 energy crisis; here is what broke and here is what I
changed."* Also the only way to know whether this model generalises at all —
every number currently ends before the crisis.

### M4. Fix the August trough

Every model posts its worst fold on the French industrial shutdown (18.98 MAE
against an 8.68 average). One `is_august_vacation` flag is too blunt for a
shutdown whose depth varies year to year; residual demand during that month is
where to look.

### M5. A release gate

Fail the training pipeline if the champion candidate is not better than the
incumbent by a Diebold-Mariano-significant margin. **The DM machinery already
exists** (`CORRECTIONS.md` finding 10) — it is simply not wired into the
pipeline. Small change, and it is what makes the registry promotion meaningful.

### M6. Registry aliases and a promotion rule

The API resolves `@champion` but nothing sets it, so promotion is currently
manual. Pairs naturally with M5.

## Engineering cleanups

Small, independent, none of them blocking.

| # | Item | Effort |
|---|---|---|
| E1 | `ruff` + `mypy` jobs in CI — the code is already annotated | small |
| E2 | Move the `:ui` image off full `mlflow` (`mlflow-skinny`); nothing under `src/ui/` imports it, yet it drags in `matplotlib` and `fastapi`. ~300 MB | small |
| E3 | Streamlit `AppTest` coverage — CI currently only builds the image and imports the module | small |
| E4 | A scheduled smoke run of `--skip-tuning --skip-shap --no-register` to catch pipeline rot unit tests cannot see | small |
| E5 | S3 lifecycle rules (Standard-IA at 90 days) — pointless at today's ~100 KB, worth it once the forecast archive accumulates | 15 min |
| E6 | Data and feature versioning — the last `🔴 Absent` in `MLOPS.md` that is cheap. Content-hash the input CSVs and log the hash per run | small |
| E7 | Fix the `WeatherForecaster` leap-day index drift — it builds its training index over leap-day-stripped data but computes offsets in calendar days, so `t` drifts 1–2 days across a two-year window. **Behaviour is preserved from the notebook deliberately; correcting it silently moves every number** | small, but not free |

## Measured findings not yet acted on

From `CORRECTIONS.md`. These are *measurements*, so they need deciding, not
investigating.

| # | Finding | Action |
|---|---|---|
| 1 | `is_lockdown` is dead in 10 of 12 folds (zero variance) | Drop it, or make the window fold-aware |
| 3 | A third feature-set variant (`residual + NUCLEAR`, condition number 45) beats both existing options on conditioning | Add and benchmark all four |
| 5 | Cascade error was only measured on warm folds | Run the same propagation check on a winter fold |
| 6 | `INTERNALS.md` overstates the arcsinh rationale — the real advantage is parameter robustness (1.6% drift vs λ's 122%), not a closed form | Rewrite the passage |
| 7 | Seven cascade hyperparameters are frozen and untested | Two-tier tuning |
| 8 | The Optuna objective is hardcoded to mean RMSE | Make it configurable (mean vs risk-aware) |
| 9 | Hyperparameters are tuned and reported on the same folds (+0.47 MAE of optimism, measured) | Nested CV, or keep reporting it as a caveat |
| 12 | 8.68 MAE is a **warm-season** figure — `extended_summer` is deployment-matched | Restate it as such everywhere it appears |

Items 2, 4, 11, 13–17 are already resolved or are documentation fixes already
applied.

## Deliberately parked

Not gaps. Decisions.

| Parked | Why, and what would unpark it |
|---|---|
| **JEPX (Japan) migration** | Fully researched — Lago et al. 2021 + `epftoolbox`, a three-act plan in `JEPX_MIGRATION.md` §8. Parked to finish EPEX first. Unpark when the EPEX story is complete enough to be worth porting |
| **Research-paper replication as the spine** | Considered and set aside; the cascade is a stronger differentiator than a replication |
| **RNN / LSTM / foundation models** | Gradient boosting beating deep learning on 2,000 tabular rows is the *expected* result. Worth doing as an honest negative — *"I tried it, it lost, here is why"* — which is more impressive than deploying deep learning uncritically. Not worth doing to look modern |
| **Hourly resolution** | Transforms realism, but it is a large lift and would invalidate every current number |
| **Airflow / Dagster** | Real, and frequently over-claimed on resumes. Do it last, if at all |
| **Live daily forecasting** | Explicitly out of scope: the stated aim is a complete, runnable production system, not a service that consumes credits indefinitely |

## What "done" would mean

A useful stopping point, so this does not expand forever:

1. **B1–B4** cleared.
2. **D1–D3**: an image in ECR, a deployed instance, evidence captured, torn down
   cleanly.
3. **M1**: `year` replaced, with before-and-after numbers.
4. **M2**: quantile forecasts, with coverage reported.

That set turns "a well-engineered notebook port" into "a deployed system with a
diagnosed and fixed modelling flaw and honest uncertainty" — which is a
different claim, and the one worth making.

Everything below that line is optional polish. **D1–D3 alone would close the
only gap between what this project is and what it says it is.**
