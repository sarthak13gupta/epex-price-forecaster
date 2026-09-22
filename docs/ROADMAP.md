# Roadmap — what is done, what is left

A single status page. Every other doc describes how something works; this one
describes **what does not exist yet**, why it is ordered the way it is, and
which items are blocked on a decision rather than on effort.

Last reviewed: **2026-09-23**.

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
| Docker | ✅ Service images plus self-contained bundled inference target built |
| Tests | ✅ 53, ~2 s |
| CI | ✅ 3 jobs on every push |
| Image publish (ECR) | ✅ Hardened bundled image published; digest and scan review recorded |
| **Deployment to EC2** | 🟡 Automation ready; provisioning permission pending |
| Monitoring / drift | ⬜ Not started |
| Continuous training | ⬜ Not started |

The honest one-line summary: **the release image exists in ECR and passed a
clean-runner contract, but no EC2 host serves it yet.** That gap is now the top
of this list.

## Blocked on you, not on effort

These cannot be done by anyone but the account owner. Each is minutes of work,
and everything in the next section waits on them.

| # | Action | Why it needs you |
|---|---|---|
| B0 | 🟡 Verify temporary `AdministratorAccess` is absent | Admin-level capability is absent; the current identity cannot inspect its attachments |
| B1 | `sudo ./infra/host/install-docker-engine.sh` | No passwordless sudo here |
| B2 | ✅ `./infra/iam/apply-github-oidc.sh` | OIDC provider, Tokyo ECR repository and push role created |
| B3 | ✅ Set three repository variables | Tokyo values set; GitHub role assumption verified |
| B4 | 🟡 Apply the prepared ECR-pull-only EC2 role | Automation exists; current CLI identity lacks IAM/EC2 provisioning permission |

B1 is independent and can be done any time. B2–B3 and image publication are
complete. The temporary admin policy was removed. The active deployment chain
is now **grant narrow Phase-5 bootstrap permission → B4 → deploy → remove that
bootstrap permission.**

### B0. An admin AWS profile

The AWS CLI **does not read `.env`.** It has its own credential chain —
environment variables, then `~/.aws/credentials`, then an instance role — so
the project's `.env` is invisible to it.

More importantly, IAM bootstrap actions need an identity authorized to manage
the exact IAM and ECR resources. The preferred design is a separate bootstrap
administrator, not the long-lived pipeline identity.

```bash
aws configure --profile admin        # admin access key, secret, region
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
export AWS_PROFILE=admin
aws sts get-caller-identity          # confirm the exact identity and account
```

The `unset` matters: **environment variables beat `AWS_PROFILE`**, so leftover
pipeline keys would silently override the profile.

Verify, then continue:

```bash
aws iam list-roles --max-items 1 >/dev/null && echo "admin OK"
```

**2026-09-22 preflight:** a profile named `admin` was configured in Tokyo, but
STS showed that its key belongs to `quantitative-pipeline-user`. IAM role/OIDC
and ECR reads were denied. A profile name is not a permission boundary; its
underlying IAM identity must be replaced with a separate administrator identity
before B2 can run.

**Resolution later that day:** temporary `AdministratorAccess` was attached to
that identity. IAM/OIDC reads, Tokyo ECR reads and simulation of the required
create/push actions now pass. B0 is operationally unblocked, but the policy must
be removed again after provisioning because a pipeline user should not retain
administrator privileges.

### B1. A native Docker engine

Independent of the AWS chain. Currently `/usr/bin/docker` is a symlink into
`/mnt/wsl/docker-desktop/cli-tools/`, so the CLI *disappears* when Desktop
stops rather than failing to connect.

1. Docker Desktop → Settings → Resources → WSL Integration → **turn off** for
   this distro. Two engines competing for `/var/run/docker.sock` is worse than
   either alone.
2. `sudo ./infra/host/install-docker-engine.sh`
3. `newgrp docker` (or `wsl --shutdown` from PowerShell, then reopen)
4. `ls -la $(which docker)` — expect a **regular file**, not a symlink.

The script checks systemd is PID 1 first, because `systemctl enable --now
docker` otherwise fails cryptically and aborts mid-install.

### B2. The OIDC role and ECR repository

```bash
REPO=sarthak13gupta/epex-price-forecaster ./infra/iam/apply-github-oidc.sh
```

Creates the OIDC identity provider, the ECR repository (scan-on-push enabled)
and the push role, then prints the three values for B3. Idempotent — re-running
updates the policies rather than failing.

**Complete 2026-09-22:** these resources now exist in `ap-northeast-1` where
regional, and the role trust uses GitHub's immutable numeric owner/repository
subject. Read-only workflow run
[35761192958](https://github.com/sarthak13gupta/epex-price-forecaster/actions/runs/35761192958)
successfully assumed the role and read the ECR repository.

The region comes from `.env` and is **not** guessed: creating the ECR
repository in the wrong region surfaces much later as an image the instance
cannot find.

### B3. Three repository variables

At `https://github.com/sarthak13gupta/epex-price-forecaster/settings/variables/actions`,
using the values B2 printed:

| Variable | Value |
|---|---|
| `AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/github-actions-ecr-push` |
| `AWS_REGION` | `ap-northeast-1` |
| `ECR_REPOSITORY` | `epex-forecaster` |

**Variables, not secrets** — a role ARN is not a credential, and keeping it
visible makes the wiring auditable. Until all three exist, `publish.yml` skips
with an explanation instead of failing.

**Complete 2026-09-22:** all three variables are set. D1 subsequently published
the verified image.

Then publish:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

### B4. The EC2 instance role

The self-contained bundled image needs only `policy-ecr-pull.json`: it does not
need S3 model access or forecast-write permissions. The legacy `apply.sh`
always builds an S3 policy and must not be used for this deployment. Phase 5
now has a separate idempotent `apply-ec2-ecr-pull.sh` path that creates the
EC2 role/profile and substitutes the Tokyo repository ARN.

**2026-09-23 preflight:** no resources were created. The configured profile was
denied on `ec2:DescribeVpcs` and IAM-policy inspection. Grant the temporary,
narrow policy in `policy-phase5-provisioner.json` through a separate privileged
console identity, complete the documented deployment, then remove it.

## Next: finish the deployment story

This is the only remaining gap that changes what the project *is* rather than
how good it is.

### ✅ D1. Publish an image to ECR

Unblocked by B2 and B3. Then tag a release and the workflow runs:

The model-side prerequisite and local image packaging are now complete:
registered version 1 was exported as an immutable, checksum-recorded release
candidate, bundled into the serving image, and verified in a read-only
container with no network, volume, database or training process. See
`MODEL_RELEASE.md` and `DEPLOYMENT_PHASES_2_3.md`. It has deliberately **not**
been assigned `@champion`; model-quality approval remains a separate decision.

The release mechanism is now implemented and locally validated. A deterministic
GitHub Release asset supplies the gitignored model; the workflow verifies its
archive and model-tree hashes, builds and tests Linux/amd64, then pushes the same
tested image and records its ECR digest. The amd64 image passed the complete
Phase-3 contract locally. See `DEPLOYMENT_PHASE_4.md`.

**Complete 2026-09-22.** Workflow run
[35762796347](https://github.com/sarthak13gupta/epex-price-forecaster/actions/runs/35762796347)
published commit `8ab83d0`. The deployment identity is
`sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0`.
The initial scan's avoidable curl findings were removed; the two remaining
unfixed and unreachable base-OS findings are reviewed in
`DEPLOYMENT_PHASE_4.md`.

### D2. Deploy inference-only to EC2

The decision already taken: **inference only for now**, with the architecture
left able to train on EC2 later. Train locally, register, deploy the artifact.

Implemented Phase-5 steps:

1. `t3.micro`, current Amazon Linux 2023 x86_64 AMI, with the B4 instance
   profile attached at launch. Do not assume this account receives free tier.
2. A dedicated security group with **no ingress**, no SSH key pair and an API
   bound only to host loopback. Public HTTPS is a later phase.
3. No model-store configuration and no AWS access keys in the container. The
   model is already under `/app/model`; the host role exists only to pull ECR.
4. IMDSv2 required, encrypted delete-on-termination root storage and a
   read-only/non-root container with dropped capabilities.
5. Pull and run the full ECR URI pinned to the Phase-4 digest, never `:bundled`.
6. Require healthy model loading and a real prediction before writing the
   `PHASE5_OK` console marker.

`t3.micro` has **1 GiB RAM** — appropriate for trying the single API, not for
three services or a training run. EC2, EBS and public IPv4 can all be billable;
the design does not rely on free-tier eligibility.

### D3. Capture evidence, then tear down

The point of deploying is the evidence, not merely the EC2 state. Capture the
console-recorded image identity, `/health` and `/predict` responses, plus the
role, security group and IMDS configuration. Streamlit is not in this first
serving slice.

`infra/ec2/terminate-inference.sh` exists and requires both the exact instance
ID and a confirmation word. Use it when the learning host is no longer needed;
its encrypted root volume is deleted with the instance.

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
