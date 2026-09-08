# S3 and EC2 — A Working Introduction

Written for a first encounter with AWS. Covers what these two services actually
are, how they apply to this project, where else you meet them, their real
trade-offs, and the subset worth mastering.

Companion to `MLOPS.md` (the lifecycle), `DESIGN.md` section 9 (the target
topology) and `DOCKER.md` (the container setup in detail).

---

## Before anything else: what "scale" actually means here

You asked how S3 and EC2 help scale a project like this. The honest answer is
worth stating first, because it changes what you should learn:

> **They do not scale this project. They make it reachable, durable and
> reproducible.**

This dataset is 2,007 rows and about 150 KB. A forecast is a Ridge predict plus
an XGBoost predict over 31 rows — single-digit milliseconds. Nothing here is near
any computational limit. Reaching for AWS to "scale" it would be theatre.

What AWS genuinely buys this project is different, and more valuable:

| Not this | But this |
|---|---|
| Handle more data | The data survives your laptop dying |
| Handle more traffic | Someone else can open a URL and use it |
| Faster training | Training can run on a schedule without you present |
| Distributed compute | The environment is defined, not "works on my machine" |

Those are **availability, durability and reproducibility** — not scale. Say it
that way in an interview and you will sound like someone who has thought about
it rather than someone reciting service names.

**Where scale would genuinely start to matter for this project**, so you know the
threshold:

- Moving from daily to **hourly** resolution: ~50,000 rows per market instead of
  2,000. Still one machine.
- Adding **multiple markets** (DE, BE, ES, IT): a few hundred thousand rows,
  and now the 12-fold backtest across 4 markets and 3 models is worth
  parallelising across machines.
- **Probabilistic forecasting with many quantiles** across many folds: this is
  the first thing here that would genuinely want parallel compute.

Even then the answer is "a few EC2 instances", not Spark.

---

# Part 1 — S3

## What it is

**S3 (Simple Storage Service) is object storage.** You give it a name and some
bytes; it stores them durably and hands them back when you ask.

The most useful thing to understand early is what it is *not*:

```
FILE SYSTEM (your laptop, or EBS)      S3 (object storage)
─────────────────────────────────      ────────────────────────────────────
directories exist                    no directories — "folders" are an
                                        illusion created by "/" in key names
can append to a file                   objects are immutable: you replace
                                        the whole object, never edit it
can seek/read a byte range fast        can range-read, but it is an HTTP call
mounted, accessed as a path            accessed over HTTPS via an API
fails when the disk fails              11 nines of durability, replicated
size limited by the disk               effectively unlimited
```

```
DATABASE (RDS, Postgres)               S3
─────────────────────────────────      ────────────────────────────────────
query rows with SQL                    fetch whole objects by exact name
enforces schema, transactions          stores bytes, knows nothing about them
expensive per GB                       very cheap per GB
good for many small reads/writes       good for large blobs, written once
```

**The mental model that works:** S3 is a giant, extremely reliable dictionary.
The key is a string; the value is a blob of bytes. `bucket + key -> bytes`.

## The core vocabulary

| Term | What it is | Example |
|---|---|---|
| **Bucket** | The top-level container. Name is **globally unique across all of AWS** and 3-63 lowercase characters | `my-epex-forecasting-data` |
| **Key** | The full name of an object inside the bucket. Slashes are just characters | `raw/epex/dt=2020-07-01/prices.csv` |
| **Object** | The bytes plus metadata (size, content type, ETag) | the CSV itself |
| **Prefix** | A leading portion of a key. What the console renders as a "folder" | `raw/epex/` |
| **Region** | The physical location. Affects latency, cost and legal residency | `eu-west-1` (Ireland) |
| **ARN** | The globally unique identifier used in permissions | `arn:aws:s3:::my-bucket/raw/*` |

**The illusion worth internalising:** there is no `raw/` folder. There are
objects whose keys happen to begin with `raw/`. Creating an "empty folder" in the
console creates a zero-byte object named `raw/`. This is why `list_objects_v2`
takes a `Prefix` rather than a path, and why renaming a "folder" means copying
every object under it.

## How it is used in this project

Currently: only in `src/data/data_loader.py`, on a branch that has never run.

```
load_raw_dataset("train")
  └─ ENV == "production" ?
       └─ _load_from_s3(key)
            ├─ boto3.client("s3", aws_access_key_id=..., ...)
            ├─ get_object(Bucket=$S3_BUCKET_NAME, Key=$S3_TRAIN_FILE_KEY)
            └─ pd.read_csv(io.BytesIO(response["Body"].read()))
```

Note the pattern: **stream the body straight into pandas** rather than
downloading to a temp file. On an ephemeral container there is no reason to
touch the filesystem, and nothing is left behind to clean up.

### The four prefixes this project should use

```
s3://<bucket>/
├── raw/epex/dt=YYYY-MM-DD/          incoming daily feed, append-only
├── processed/clean/                 the cleaned parquet cache
├── mlflow-artifacts/                model bundles, SHAP plots, leaderboards
└── forecasts/dt=YYYY-MM-DD/         every forecast the API serves
```

**Why `dt=YYYY-MM-DD` partitioning matters** — this is the single most useful S3
habit to learn:

- A daily feed becomes **append-only**. A bad file damages one partition instead
  of overwriting history.
- You can reprocess a single day without touching the rest.
- `Athena` and `Glue` recognise `key=value` directory naming as partitions
  automatically, so you can later run SQL over the raw data without loading it.
- Listing is cheap and scoped: `Prefix="raw/epex/dt=2024-01"` fetches one month.

**Why `forecasts/` matters more than it looks.** It is the enabling step for
everything in `MLOPS.md` step 11 (monitoring). Once forecasts are stored, you can
later join them to settled prices and compute realised accuracy. **It cannot be
reconstructed retrospectively** — if you do not save today's forecast today, that
observation is gone forever. It costs almost nothing and should be turned on
before any monitoring exists.

## The S3 components to master, in priority order

**1. Key design and prefixes** — the highest-leverage thing on this list. Get
your key structure right and everything downstream is easy; get it wrong and you
will be copying millions of objects later. Rules of thumb: put the *partition
dimension* you filter on most into the prefix, use `key=value` naming, never put
a timestamp at the *front* of a key if you will write at high volume.

**2. IAM — who is allowed to do what.** There are three overlapping mechanisms
and confusing them is the most common S3 mistake:

| Mechanism | Attached to | Use it for |
|---|---|---|
| **IAM policy** | A user or a role | "this application may read this bucket" |
| **Bucket policy** | The bucket | "this bucket is readable by that account" |
| **Block Public Access** | Account or bucket | The safety net. Leave it **on** |

For an ML project, the one you actually need is an **IAM role attached to the EC2
instance** — covered under EC2 below.

**3. `boto3` basics.** Five calls cover almost everything:

```python
s3 = boto3.client("s3")
s3.get_object(Bucket=b, Key=k)["Body"].read()      # read bytes
s3.put_object(Bucket=b, Key=k, Body=data)          # write bytes
s3.upload_file("local.csv", b, k)                  # write a file (multipart-aware)
s3.download_file(b, k, "local.csv")                # read to a file
s3.list_objects_v2(Bucket=b, Prefix="raw/")        # enumerate (paginated!)
```

The one that bites beginners: `list_objects_v2` returns **at most 1,000 keys**.
Use `boto3.client("s3").get_paginator("list_objects_v2")` or you will silently
process only the first thousand.

Also worth knowing: **pandas reads S3 directly** via `s3fs`
(`pd.read_csv("s3://bucket/key.csv")`). Convenient, but the explicit `boto3`
version in this project is better for production because the error handling and
credential source are visible rather than hidden in a dependency.

**4. Storage classes and lifecycle rules** — this is where cost control lives:

| Class | Cost per GB/month | Retrieval | Use for |
|---|---|---|---|
| Standard | ~$0.023 | instant, free | active data |
| Standard-IA | ~$0.0125 | instant, small fee | data older than ~90 days |
| Glacier Instant | ~$0.004 | instant, higher fee | archive you might audit |
| Glacier Deep Archive | ~$0.001 | hours | legal retention |

A **lifecycle rule** moves objects between these automatically by age. For this
project: raw and forecast partitions to Standard-IA at 90 days, Glacier Instant
at one year.

**5. Versioning.** Keeps every version of an object, so an accidental overwrite
or delete is recoverable. **Turn it on before you need it** — it cannot be
applied retroactively to objects already clobbered. The cost is that you now pay
for old versions, so pair it with a lifecycle rule that expires them.

**6. Encryption.** `SSE-S3` (AWS-managed keys) is one checkbox and enough for
most work. `SSE-KMS` gives you a key you control and an audit trail of every
decryption, at slightly more cost and complexity.

**7. Presigned URLs.** A time-limited URL that grants access to one object
without credentials. The standard way to let a browser upload or download
directly without proxying bytes through your API. Very useful once a frontend
needs to hand users a file.

Lower priority until you need them: multipart upload (handled for you by
`upload_file`), S3 Select, Transfer Acceleration, replication, Object Lock.

## Pros and cons

| Pros | Cons |
|---|---|
| Effectively unlimited, 11 nines durability | Not a file system — no append, no partial edit |
| Very cheap: this project's data costs cents/month | Per-request charges add up with many small objects |
| Decouples storage from compute — any instance can read it | Every read is an HTTPS round trip (~tens of ms) |
| Versioning, lifecycle, encryption are configuration, not code | Egress out of AWS is expensive (~$0.09/GB) |
| Integrates with everything (Athena, Glue, SageMaker, Lambda) | Easy to misconfigure permissions and expose data |
| Bucket is a natural boundary for access control | Global bucket namespace — good names are taken |

**The cost trap to know:** storage is cheap, *requests* are not free
(~$0.0004 per 1,000 GETs). A job that reads 10 million tiny objects costs more in
requests than in storage. This is why you prefer fewer, larger files — and why
Parquet (columnar, compressed, one file per partition) beats thousands of small
CSVs.

## Where else you meet S3

- **Data lake foundation** — raw zone / clean zone / curated zone, queried by
  Athena or Spark
- **ML artifacts** — every managed MLflow, SageMaker, or Kubeflow setup stores
  models here
- **Static website and asset hosting** — behind CloudFront
- **Backups and database snapshots**
- **Data exchange between teams or companies** — a bucket with a cross-account
  policy
- **Event source** — an object landing can trigger a Lambda, which is how
  event-driven pipelines start

---

# Part 2 — EC2

## What it is

**EC2 (Elastic Compute Cloud) is a rented computer in a data centre.** You pick
the CPU/RAM, pick an operating system image, and get a machine you SSH into and
administer exactly like a Linux box under your desk.

That is genuinely all it is. Everything else is options.

## The anatomy of an instance

```
                    ┌─────────────────────────────────────────┐
   SSH (port 22) ───┤  EC2 INSTANCE                           │
   HTTPS (443)  ────┤                                         │
        ▲           │  AMI          the OS image it booted    │
        │           │  Instance     vCPU + RAM shape          │
   SECURITY GROUP   │    type       (t3.large = 2 vCPU/8 GiB) │
   the firewall:    │                                         │
   which ports,     │  EBS volume   the disk. Persists across │
   from which IPs   │               stop/start. Snapshot-able │
        │           │                                         │
        │           │  IAM role     credentials, injected     │
        │           │               automatically — no keys   │
        │           │               in files                  │
        │           │  User data    a script that runs once    │
        │           │               on first boot              │
        │           └─────────────────────────────────────────┘
        │                       │
   PUBLIC IP / ELASTIC IP       └──► S3, RDS, CloudWatch (via the IAM role)
   how the world reaches it
```

## How it is used in this project

One `t3.large` running three containers via docker compose:

```
EC2 t3.large  (2 vCPU, 8 GiB, 30 GiB gp3)
│
├── Nginx        :443 ──► the only port open to the internet, terminates TLS
├── Streamlit    :8501    docker network only
├── FastAPI      :8000    docker network only
├── MLflow       :5000    docker network only  ◄── has NO auth of its own;
│                                                  anything reaching this port
│                                                  can delete your registry
└── Training container    run to completion on a schedule, then exits
```

**Why one instance is the right answer here.** The binding constraint is not
serving — a forecast is milliseconds. It is the Optuna sweep, which is CPU-bound
across 12 folds. One model, one analyst audience, a daily retrain measured in
minutes. Reaching for ECS or Kubernetes would add operational surface without
removing a real problem.

**Why `t3.large` specifically.** 2 vCPU and 8 GiB comfortably holds three
containers plus an XGBoost training run on 2,000 rows. If a nightly retrain runs
long, the right move is not a bigger permanent instance — it is a *scheduled*
`c7i.xlarge` that terminates on completion, so you stop paying for idle vCPUs the
other 23 hours.

## The EC2 components to master, in priority order

**1. Security groups** — learn these first, because they cause the majority of
"why can't I connect" and the majority of accidental exposures. A security group
is a stateful firewall attached to the instance:

- **Inbound** rules say who may reach which port. Default: nothing.
- **Outbound** rules say where the instance may connect. Default: everywhere.
- **Stateful** means a reply to an allowed outbound request is automatically
  allowed back in; you do not write a rule for it.

For this project the correct configuration is exactly two inbound rules:

| Port | Source | Why |
|---|---|---|
| 443 | `0.0.0.0/0` | the public HTTPS endpoint |
| 22 | **your IP only** (`x.x.x.x/32`) | SSH |

**Never** open 22 to `0.0.0.0/0`, and never expose 8000/8501/5000 at all — Nginx
reverse-proxies to them over the Docker network. An open MLflow port is a
delete-my-model-registry button.

**2. IAM instance roles** — the most important thing on this list for an ML
engineer, and the fix for a real problem in this repo.

Right now `data_loader.py` passes credentials explicitly:

```python
boto3.client("s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),        # from .env
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"))
```

Those are long-lived keys (`AKIA…`) sitting in a file. If that file leaks, the
keys work from anywhere in the world until someone revokes them.

An **instance role** removes them entirely. You attach a role to the instance;
AWS injects short-lived, auto-rotating credentials; `boto3` finds them with no
arguments:

```python
boto3.client("s3")     # that is all. Credentials come from the instance role.
```

The credentials are temporary, rotate automatically, and are useless off the
instance. Scope the role narrowly — `s3:GetObject` and `s3:PutObject` on one
bucket, nothing else.

**How boto3 finds credentials**, in order — worth memorising, because it explains
every "it works locally but not on the server" puzzle:

```
1. explicit arguments to the client        ◄── what this project does now
2. environment variables (AWS_ACCESS_KEY_ID, ...)
3. ~/.aws/credentials  (the shared credentials file)
4. IAM instance role via the metadata service   ◄── what it should do on EC2
```

**3. Instance types and families.** The naming is `family + generation + size`:

| Family | Optimised for | When you want it |
|---|---|---|
| `t3`, `t4g` | **Burstable** — cheap, but CPU is credit-limited | Dev boxes, light web services |
| `m5`, `m7i` | General purpose, balanced | Steady mixed workloads |
| `c5`, `c7i` | **Compute** — best CPU per dollar | Training, Optuna sweeps, backtests |
| `r5`, `r7i` | **Memory** — lots of RAM per vCPU | Large in-memory DataFrames |
| `g4`, `p4` | **GPU** | Deep learning |

The `t3` burstable trap is worth knowing: you accumulate CPU credits while idle
and spend them under load. A long training job **exhausts its credits and gets
throttled** to a fraction of the nominal CPU. That is why a heavy retrain belongs
on a `c7i`, not a `t3`.

**4. EBS volumes.** The disk. Two things to know:

- **EBS persists** across stop/start and can be snapshotted to S3. `gp3` is the
  sensible default.
- **Instance store** (on some instance types) is physically attached, fast, and
  **wiped when the instance stops.** People lose data to this.

**5. Public IP vs Elastic IP.** A default public IP **changes every time you stop
and start** the instance. An **Elastic IP** is a static address you allocate and
attach. Without one, your DNS record and any bookmarked URL break after a
restart. (Free while attached to a running instance; charged when idle.)

**6. Key pairs and SSH.** You create a key pair, AWS keeps the public half, you
keep the `.pem`. `chmod 400` it, then
`ssh -i key.pem ec2-user@<ip>`. If you lose the private key you cannot get in —
there is no password reset. **Consider Session Manager** (in AWS Systems Manager)
instead: browser-based shell, no port 22 open at all, no key to lose. It is
strictly better practice and skips the most common exposure.

**7. Pricing models** — where the real money decisions are:

| Model | Discount | Trade-off |
|---|---|---|
| **On-demand** | baseline | Pay by the second, no commitment |
| **Spot** | **up to 90% off** | AWS can reclaim it with 2 minutes' notice |
| **Reserved / Savings Plan** | ~40-70% off | 1-3 year commitment |

**Spot instances are the single biggest cost lever in ML**, and the reason is
structural: training is *interruptible*. If a spot instance dies mid-sweep you
restart the job; nothing is lost if you checkpoint to S3. Serving is *not*
interruptible, so keep the API on-demand and run training on spot.

**8. User data.** A shell script that runs once on first boot — install Docker,
pull your image, start compose. This is how an instance becomes reproducible
rather than hand-configured. Anything you do over SSH that you would have to
redo on a new instance belongs in user data (or, better, in an AMI or a container
image).

Lower priority until needed: VPC and subnet design, load balancers, auto scaling
groups, placement groups.

## Pros and cons

| Pros | Cons |
|---|---|
| Full control — it is a real Linux machine | Full responsibility: patching, monitoring, backups |
| Any software, any runtime, any language | You are the sysadmin |
| Predictable performance and pricing | Idle instances cost money 24/7 |
| Spot pricing makes batch compute very cheap | Scaling is manual unless you configure it |
| Skills transfer directly to any Linux server | Slower to provision than serverless (~1 min vs ms) |
| No vendor-specific application code | Easy to misconfigure the firewall |

**The classic surprise bill:** leaving an instance running. `t3.large` is ~$60/
month whether or not you use it. Stop it when you are not working; you keep the
EBS volume (a few dollars) and pay nothing for compute.

**The alternatives, and when to prefer them:**

| Option | Prefer when |
|---|---|
| **EC2** | You want control, or a long-running process, or spot pricing for batch |
| **ECS / Fargate** | You have containers and do not want to manage instances |
| **Lambda** | Short, spiky, event-driven work under 15 minutes |
| **SageMaker Endpoint** | You want managed autoscaling and blue/green for a model |
| **App Runner / Elastic Beanstalk** | You want "here is my container, give me a URL" |

For this project EC2 is the honest choice: one small always-on service, one
scheduled batch job, and a desire to understand what is actually happening.

## Where else you meet EC2

- **Any self-hosted service** — MLflow tracking server, Airflow, Postgres, a
  Jupyter box
- **Batch training** on spot, especially GPU work
- **The substrate under other services** — ECS, EKS and EMR all run on EC2 under
  the hood, so understanding it explains their behaviour
- **Legacy lift-and-shift** — moving an on-premise server to the cloud unchanged

---

# Part 3 — How they work together

The division of labour is the thing to internalise:

```
   S3                                        EC2
   ──────────────────────────────            ──────────────────────────────
   STATE                                     COMPUTE
   durable, cheap, permanent                 ephemeral, expensive, disposable
   survives every instance dying             can be destroyed and recreated
   the source of truth                       holds nothing that matters
```

> **The principle: keep all state in S3 so that any instance is disposable.**

That single rule is what makes cloud architecture work, and it is what makes this
project's design already close to correct:

- Raw data in S3, so a new instance can train from scratch.
- MLflow artifacts in S3, so a new API container can load the model.
- Forecasts in S3, so the accuracy record outlives any instance.
- Nothing important on the instance's disk.

Test your own design with this question: **"if this instance were terminated
right now, what would I lose?"** If the answer is anything other than "nothing",
that thing belongs in S3.

## Where this project's data flows

```
   raw/epex/dt=.../          ──read──►  EC2: training container
                                          │
   mlflow-artifacts/     ◄──write────────┘  (model bundle, SHAP plots, leaderboard)
        │
        └──read──►  EC2: FastAPI container   (once, at startup)
                       │
   forecasts/dt=.../  ◄─┘ write every served forecast
        │
        └──► later joined to settled prices for realised accuracy
```

## The honest scaling path, in order

If this project grew, this is the order things would actually change:

1. **Nothing changes for a long time.** One instance handles daily/hourly, one
   market, one model.
2. **Hourly resolution, multiple markets** → still one instance, but training
   moves to a scheduled spot `c7i` and data moves to Parquet in S3.
3. **Many quantiles × many folds × many markets** → parallelise the backtest
   across a handful of spot instances, each writing results to S3.
4. **Real-time intraday feed** → Kinesis into S3, and the API needs autoscaling
   (ECS or a SageMaker endpoint).
5. **Only then** would anything resembling Spark or Kubernetes be justified.

Most projects never get past step 2. Knowing that is itself a signal of judgement.

---

## How the model interacts with S3 — implemented and verified

This is the part worth understanding concretely, because it is now built rather
than planned. Low-level function detail is in `INTERNALS.md` §3
(`data_loader.py` for reads, `s3_store.py` for writes); the architectural
summary is `DESIGN.md` §9.

![The model's interaction with S3: four prefixes hold raw data, processed data, MLflow artifacts and the date-partitioned forecast archive. The training container reads raw data and writes artifacts plus forecasts; the FastAPI container reads the model bundle once at startup; the forecast archive is later joined to settled prices to produce a live accuracy record.](images/07-s3-lifecycle.png)

**Read the diagram by who touches what.** The training container is the only
thing that *writes*. FastAPI reads exactly once, at boot. And critically —
**a forecast request touches S3 zero times**, because the model is already in
RAM.

### The read and write paths in code

```
READ PATH — data into the model                       code: src/data/data_loader.py
                                                            src/utils/config_loader.py
  load_raw_dataset("train")
    │
    ├─ $ENV == "local" ?  ──► pd.read_csv(paths.raw_train_csv)     [dev]
    │
    └─ $ENV == "production" ?
         ├─ boto3.client("s3", region_name=...)   ✓ no keys: boto3 chain
         ├─ get_object(Bucket=$S3_BUCKET_NAME, Key=$S3_TRAIN_FILE_KEY)
         └─ pd.read_csv(io.BytesIO(body))     stream, never touches the disk
              │
              ▼
     ┌──────────────────────────────────────────────────────────────┐
     │  THE MODEL PIPELINE — knows nothing about S3                 │
     │    preprocess ─► features ─► folds ─► cascade ─► tune        │
     │    ─► leaderboard ─► PriceForecaster.fit ─► pyfunc bundle    │
     └──────────────────────────────────────────────────────────────┘
              │
WRITE PATH — model output into S3                     code: src/data/s3_store.py
              │
              ├─ S3Store(config).enabled ?
              │    └─ $ENV=="production" AND bucket set     ── no ──► return None
              │         ▲                                              (silent no-op,
              │         └─ both conditions: a stray credential          callers need no
              │            must not make a local run write             ENV branches)
              │
              ├─ save_forecast(forecast_df, run_date)
              │    ├─ forecast_key() ─► forecasts/dt=YYYY-MM-DD/forecast.parquet
              │    │                              ▲
              │    │                              └─ dt= is the VINTAGE: the date the
              │    │                                 forecast was MADE, not covered.
              │    │                                 Athena/Glue read it as a partition
              │    ├─ df.to_parquet(BytesIO)   preserves index + dtypes, compresses
              │    └─ put_object()             ── error ──► warn, return None
              │                                     ▲
              │                                     └─ writes FAIL SOFT: losing an
              │                                        archive entry must not kill a
              │                                        run that produced a good model
              │
              └─ later:  load_forecast_archive()
                   ├─ list_prefix("forecasts/")   paginated — the bare API caps at 1,000
                   ├─ read each parquet
                   └─ attach forecast_vintage from the key
                        │
                        ▼
                   join to settled prices ─► LIVE ACCURACY RECORD
                        ▲
                        └─ the thing a backtest number cannot be
```

### What was verified against the live bucket

| Check | Result |
|---|---|
| Bucket posture | versioning **Enabled**, all four public-access blocks **ON**, **AES256** default encryption |
| Credentials | valid; dedicated IAM user, not root |
| `ENV=production` read | first `[PROD]` fetch — 2,007 and 31 rows, matching local exactly |
| Full pipeline from S3 | identical metrics to the local run (16.99 / 13.00 / 9.08 MAE) |
| Write round-trip | 8,256 B Parquet to `forecasts/dt=2020-07-01/`, read back `DataFrame.equals` → True |
| `list_prefix` / archive loader | found the object, returned it with its vintage attached |

The only bucket setting still absent is **lifecycle rules**, which are pointless
at ~100 KB of data. Add them when the archive starts growing daily.

### The three decisions in that code worth remembering

1. **`S3Store.enabled` gates on `ENV` *and* the bucket.** So no calling code
   needs an `if production` branch — the pipeline calls `save_forecast()`
   unconditionally and it is simply inert locally. Gating on the bucket alone
   would let a stray exported variable make a local experiment write to the
   cloud.
2. **Writes fail soft; reads raise.** Losing an archive entry must not kill a
   training run that produced a valid model. A read is something the caller
   depends on, so failing quietly would hand back wrong data.
3. **Neither module passes credentials to `boto3`.** Both rely on the boto3
   resolution chain — environment variables locally, **IAM instance role** on
   EC2. `grep -r "aws_access_key_id" src/` returns nothing, so attaching a role
   to the instance is the only remaining step; no code changes.

---

## How the model interacts with EC2 — the target shape

Not yet deployed. This is what the instance will actually run, and which
process touches which resource.

```
WHAT RUNS ON THE INSTANCE, AND WHAT EACH PROCESS TOUCHES

  EC2 instance (Amazon Linux + Docker + docker compose)
  │
  ├─ CONTINUOUS ─ started once, runs forever
  │   │
  │   ├─ nginx            :443 ◄── the only port reachable from the internet
  │   │     ├─ / ─────────► streamlit:8501
  │   │     └─ /api/ ────► fastapi:8000
  │   │
  │   ├─ fastapi          :8000   docker network only
  │   │     │
  │   │     ├─ AT STARTUP (once):
  │   │     │    resolve_model_uri()  $MODEL_URI ▸ @champion ▸ latest version
  │   │     │    mlflow.pyfunc.load_model()  ──reads──► S3 mlflow-artifacts/
  │   │     │    unwrap ─► PriceForecaster held in RAM
  │   │     │         ▲
  │   │     │         └─ after this instant MLflow is a STARTUP dependency,
  │   │     │            not a runtime one. A running API survives MLflow
  │   │     │            being down; a RESTARTING one does not
  │   │     │
  │   │     └─ PER REQUEST:  validate ─► cascade.simulate ─► predict
  │   │            ▲
  │   │            └─ touches NO network, NO disk, NO MLflow. ~ms
  │   │
  │   ├─ streamlit        :8501   holds no model; HTTP to fastapi
  │   │
  │   └─ mlflow           :5000   docker network only
  │         ├─ backend store  ─► sqlite on EBS   (▸ RDS when >1 writer)
  │         └─ artifact root  ─► S3 mlflow-artifacts/
  │              ▲
  │              └─ NEVER expose :5000. MLflow has no auth of its own;
  │                 anything that reaches it can delete the registry
  │
  └─ SCHEDULED ─ runs to completion, then exits
      │
      └─ training container         EventBridge ─► ECS/EC2 run-task
            ├─ reads   S3 raw/epex/dt=.../
            ├─ trains  folds ─► tune ─► leaderboard ─► gate
            ├─ writes  S3 mlflow-artifacts/  (bundle, SHAP, leaderboard)
            ├─ writes  S3 forecasts/dt=.../  (the archive)
            └─ registers a new model version
                 │
                 └─ promote alias ─► restart fastapi ─► new model live
                      ▲
                      └─ promotion is a REGISTRY change + a restart.
                         Not a code change, not a rebuild. Rollback is
                         the same operation pointing back

STATE BOUNDARY — the rule that makes the instance disposable

  on the instance (disposable)          in S3 (permanent)
  ────────────────────────────          ──────────────────────────────
  container images                      raw data
  the model, in RAM                     the model bundle
  sqlite mlflow.db  ⚠ on EBS            run artifacts, SHAP plots
  nginx config                          the forecast archive
  logs                                  the accuracy record

  Test: "if this instance were terminated right now, what would I lose?"
        Today the answer is "the MLflow sqlite database" -> move it to
        RDS, or snapshot the EBS volume. Everything else: nothing.
```

### The three things this diagram is really saying

1. **A forecast request is self-contained.** It touches no network, no disk and
   no MLflow. That is what makes the API fast and what makes it survive the
   tracking server being down.
2. **Promotion is a registry change plus a restart.** Because the API loads
   `@champion` rather than a version number, shipping a new model is not a code
   change and not a rebuild — and rollback is the same operation pointing back.
3. **The state boundary is the whole game.** Everything permanent lives in S3;
   the instance holds only images, RAM and logs. The one exception today is the
   MLflow SQLite database on EBS, which is exactly what the "what would I lose"
   test surfaces — move it to RDS or snapshot the volume.

---

# Part 4 — The mastery checklist

The 20% that covers most real work. If you can do these unaided, you know enough.

## S3

- [ ] Create a bucket, upload and download an object, in console and in `boto3`
- [ ] Explain why "folders" do not exist, and design a `dt=YYYY-MM-DD` key layout
- [ ] Read a CSV from S3 into pandas two ways: `boto3` + `BytesIO`, and `s3fs`
- [ ] Paginate `list_objects_v2` past 1,000 keys
- [ ] Write an IAM policy granting read-only access to one prefix
- [ ] Turn on versioning and explain why it must precede the accident
- [ ] Configure a lifecycle rule to Standard-IA at 90 days
- [ ] Generate a presigned URL and explain when you would use one
- [ ] Explain the request-cost trap and why fewer, larger files are better

## EC2

- [ ] Launch an instance, SSH in, install Docker, run a container
- [ ] Write a security group with 443 open and 22 restricted to your IP
- [ ] Explain the difference between a security group and a network ACL
- [ ] Attach an IAM role and use `boto3.client("s3")` with **no** credentials
- [ ] Recite the boto3 credential resolution order
- [ ] Explain why `t3` throttles under sustained load, and pick `c7i` instead
- [ ] Attach an Elastic IP and explain what breaks without one
- [ ] Write a user-data script that bootstraps the instance
- [ ] Explain when spot is safe (training) and when it is not (serving)
- [ ] Stop an instance and explain exactly what you are still paying for

## The two facts most beginners get wrong

1. **S3 has no folders.** Every consequence — renaming being a copy, prefixes
   not paths, `Prefix=` instead of `path=` — follows from this.
2. **A public IP changes on stop/start.** Elastic IP or DNS breaks.

---

# Part 5 — Concrete next steps for this project

In order, smallest first.

### ✅ 1. Verify the S3 path — DONE

The bucket was already provisioned correctly (versioning on, public access
blocked, AES256). What had never run was the code. Now verified: first `[PROD]`
fetch of 2,007 and 31 rows, and a full pipeline sourced from S3 producing
identical metrics to the local run.

### ✅ 2. Bucket layout with date partitions — DONE (writes)

`paths.s3_prefixes` in `config.yaml` defines the four prefixes, and
`forecast_key()` writes to `forecasts/dt=YYYY-MM-DD/`.

**Still outstanding:** the two raw CSVs sit at the bucket *root*, not under
`raw/epex/dt=.../`. Moving them means copying the objects and updating
`S3_TRAIN_FILE_KEY` / `S3_PRED_FILE_KEY` in `.env`. Low risk — versioning is on
— but it is a live-bucket change, so it is left as a deliberate decision.

### ✅ 4. Write forecasts to S3 — DONE (in the pipeline)

`src/data/s3_store.py`, wired into `train_pipeline.py` stage 6. Round-trip
verified. **Not** yet called from `/predict`: archiving inline would add an S3
round trip to every request, so it should be fire-and-forget or queued. That is
the right next increment when the API goes live.

---

### ✅ 3. Instance-role credential chain — DONE (code side)

Both `data_loader._load_from_s3` and `s3_store.py` now pass **no** credentials
to `boto3`. Verified: `grep -r "aws_access_key_id" src/` returns nothing, and
`ENV=production` reads still succeed locally because boto3 picks the keys up
from the environment.

**Remaining (on AWS, not in code):** create an IAM role scoped to
`s3:GetObject` / `s3:PutObject` on this one bucket and attach it to the
instance. No code change is needed at that point — which is the whole benefit
of resolving credentials through the chain.

### ✅ 5. Containerise — DONE (built and verified)

- `DockerFile` → **`Dockerfile`**: `python:3.12-slim`, `PYTHONUNBUFFERED=1`,
  `libgomp1` for XGBoost's OpenMP runtime, non-root `app` user, requirements
  cached as their own layer.
- **`docker-compose.yml`**: four services from **two** images — `api` and
  `mlflow` share `:serve` (`requirements-api.txt`), `ui` uses `:ui`, and
  `train` uses the full `:latest`. `ui` and `train` sit behind compose
  **profiles** so they do not start by default. All three targets are built:
  1.17 / 1.34 / 1.64 GB on disk, down from a single 2.65 GB image.
- Ports bound to **`127.0.0.1`**, not `0.0.0.0`. On EC2 only Nginx on :443
  faces the internet.
- Healthcheck hits `/health`, which asserts the **model loaded** rather than
  returning 200 unconditionally.
- `train` has `restart: "no"` — a failing batch job must not retry forever and
  burn the instance.
- **`.dockerignore` rewritten:** build context **15.2 MB → 0.1 MB**. The worst
  offender was `mlartifacts/` at 13 MB, which is a correctness problem as well
  as a size one — baking a stale model bundle into the image risks it shadowing
  the mounted volume.

**✅ Built and verified.** Docker 29.2.1 / Compose v5.0.2:

| Check | Result |
|---|---|
| `docker compose config` | valid; only `api` + `mlflow` in the default profile |
| Image build | all layers; `libgomp1` present so XGBoost imports |
| All pinned deps on `python:3.12-slim` | no wheel built from source (16 direct pins for the API set, 102 packages resolved) |
| Runs as non-root | uid 1000 |
| `docker compose up api` | container reports **healthy** |
| `/predict` in-container | 33.81 / 33.68 / 33.36 — **identical to the host** |
| `/predict` error path | GW unit error → 422 |
| Image size | **2.65 GB → 1.64 GB** after dropping `nvidia-nccl` (288 MB) |

**Two real bugs the container exposed**, both recorded in `CORRECTIONS.md`
findings 13 and 14:

1. **MLflow baked absolute host paths into `mlflow.db`.** The experiment's
   artifact location was `/home/<user>/<project>/mlartifacts`, which does not
   exist inside a container. Fixed: `artifact_location` is now
   environment-aware and becomes `s3://<bucket>/mlflow-artifacts` under
   `ENV=production` — resolvable from any machine, any user, no mount.
2. **Host uid 1001 vs container uid 1000.** MLflow *writes*
   `registered_model_meta` into the artifact directory when loading a
   `models:/` URI, so read permission is not enough. Worked around locally via
   `HOST_UID` / `HOST_GID` in compose; the S3 artifact root removes the need
   entirely.

Both surfaced cleanly because `/health` starts *unhealthy* and reports the exact
cause rather than crash-looping — the deliberate choice in `INTERNALS.md` §11.

**Still open:** the API image carries training-only dependencies
(`llvmlite` 171 MB via `shap`, plus `plotly`, `streamlit`, `optuna`).
Splitting `requirements.txt` per service would remove roughly 270 MB more —
relevant because `t3.micro` ships an 8 GiB EBS volume by default.

### 🟡 5b. Publish images to ECR — workflow built, role outstanding

`.github/workflows/publish.yml` builds `:serve` and `:ui` and pushes them to
ECR on a `v*` tag, authenticating by **OIDC** rather than a stored key: GitHub
mints a short-lived token per run and AWS trades it for temporary credentials
scoped to this one repository. Nothing long-lived is stored as a GitHub secret —
which matters concretely, because `.env` here holds a real access key pair and
the whole point is that CI never needs one.

What is outstanding is one admin action, because the pipeline IAM user is
deliberately S3-scoped and gets `AccessDenied` on every IAM call:

```bash
REPO=sarthak13gupta/epex-price-forecaster ./infra/iam/apply-github-oidc.sh
```

It creates the OIDC provider, the ECR repository (scan-on-push enabled) and the
push role, then prints the three repository variables to set. Idempotent.

The security boundary is one condition in the trust policy, and it is the
single most common way an OIDC setup is misconfigured — `aud` alone would let
**any** GitHub repository on the internet assume the role. See
`infra/iam/README.md`.

Each image gets a moving tag (`:serve`) and an immutable one
(`:serve-<sha>`). **Pin deployments to the SHA tag** — rollback then means
naming the previous commit.

### ⬜ 6. Deploy (half a day)

Instance, Docker, compose, Nginx, security group (443 open, 22 to your IP only),
Elastic IP. Bake the model into the image so MLflow is not needed at boot.

⚠️ **Free-tier warning.** The 12-month free tier is `t2.micro`/`t3.micro` with
**1 GiB RAM** — not enough for three containers plus a training run. Either
train locally and deploy only the API and UI, or accept roughly $60/month for a
`t3.large`.

### ⬜ 7. Lifecycle rules (15 min, once the archive grows)

Standard-IA at 90 days, Glacier Instant at one year, on `raw/` and `forecasts/`.
Pointless at today's ~100 KB; worth it once a daily forecast archive accumulates.

### Cost for all of it

| Item | Monthly |
|---|---|
| `t3.large` on-demand, running 24/7 | ~$60 |
| 30 GiB `gp3` EBS | ~$2.40 |
| S3 storage and requests at this volume | < $1 |
| Elastic IP (attached) | $0 |
| **Total** | **~$65** |

Stop the instance when not in use and it drops to a couple of dollars. Worth
knowing the number: "I designed for AWS" is a stronger claim when you can cost it.
