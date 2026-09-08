# Docker in This Project

What Docker is, how it fits this system, the step-by-step approach that was
actually taken, and the two portability bugs it exposed.

Companion to `AWS_S3_EC2.md` (where the containers will run), `DESIGN.md`
(architecture) and `INTERNALS.md` (the code inside them).

---

## 1. Why Docker here

**Docker packages an application together with its entire environment** — the
Python version, every library, the OS packages — into a single immutable
artifact that runs identically anywhere.

The generic case for it is "works on my machine". The specific case for *this*
project is sharper, and there are three reasons:

**1. The model artifact is a cloudpickle of a live Python object.** The
`PriceForecaster` bundle contains a fitted `ExogenousCascade`, an
`XGBRegressor`, and a `RobustArcSinTransformer`. Unpickling requires the *same*
library versions that pickled it. A pandas or xgboost major bump breaks
deserialisation. One image for training and serving makes that class of failure
structurally impossible.

**2. XGBoost has a native dependency most people discover the hard way.** It
needs `libgomp1` (the OpenMP runtime) present at import. On a bare
`python:3.12-slim` the import fails with an opaque shared-object error. Putting
that apt package in the Dockerfile means it is never missing again.

**3. A `t3.large` needs to be reproducible, not hand-configured.** Anything done
over SSH must be redone on the next instance. In an image, it is done once.

**What it is NOT for here.** Not scale, not orchestration, not microservices.
One small always-on service and one scheduled batch job. Kubernetes would be
theatre.

---

## 2. The five concepts you need

| Concept | What it is | In this project |
|---|---|---|
| **Image** | An immutable filesystem snapshot + default command. Read-only | `epex-forecaster:latest`, 1.64 GB |
| **Container** | A running instance of an image. Disposable | four of them: `api`, `ui`, `mlflow`, `train` |
| **Layer** | One filesystem diff per Dockerfile instruction, **cached** | why `COPY requirements.txt` comes before `COPY src/` |
| **Volume** | A host directory mounted into a container. **The only writable, surviving state** | `./mlflow.db`, `./mlartifacts`, `./results` |
| **Network** | A private DNS namespace between containers | the UI reaches the API at `http://api:8000` |

**The one rule that governs everything below:**

> **The image is code. Everything mutable is a volume or S3.**
> A container must be destroyable at any moment with nothing lost.

---

## 3. The architecture

![Docker architecture: one Dockerfile builds one image through cached layers; that single image runs as four containers chosen by command — api, ui, mlflow, train. Two are always on, two are profile-gated. Ports bind to loopback, host volumes hold mutable state, and S3 holds durable artifacts.](images/08-docker-architecture.png)

### One image, four roles

This is the central design decision, and it is worth defending because the
obvious alternative is one image per service.

```
                      epex-forecaster:latest
                               │
    ┌──────────────┬───────────┴────────────┬──────────────┐
    ▼              ▼                        ▼              ▼
  api            ui                     mlflow          train
  uvicorn        streamlit               mlflow ui      python -m
  :8000          :8501                   :5000          train_pipeline
  always on      profile: ui             always on      profile: train
                                                         exits when done
```

| Option | Pros | Cons |
|---|---|---|
| **One image, command per service** ✅ | Training and serving can never diverge in library versions — the cloudpickle problem cannot occur. One build, one thing to push | Larger than any single service needs; the API carries `optuna` and `shap` it never calls |
| One image per service | Each is minimal; smaller pulls | Three Dockerfiles to keep in sync, and **the versions can drift** — which is exactly the failure the bundle is vulnerable to |

The trade is deliberate: **correctness over size**, given the artifact format.
The size cost is real and quantified in §7.

### Two always-on, two gated

`ui` and `train` sit behind compose **profiles**, so `docker compose up` starts
only `api` and `mlflow`.

- `ui` is gated because **`src/ui/app.py` does not exist yet**. Without the
  profile, `docker compose up` would fail on a service that was never written.
- `train` is gated because it is a **batch job**, not a service. It must be
  invoked deliberately: `docker compose --profile train run --rm train`.

### Ports bind to loopback, not 0.0.0.0

```
ports:
  - "127.0.0.1:8000:8000"     not "8000:8000"
```

`8000:8000` binds to every interface, which on EC2 means **the public
internet**, security group permitting. Binding to `127.0.0.1` means only
processes on the host — i.e. Nginx — can reach them.

This matters most for MLflow: **it has no authentication of its own.** Anything
that can reach `:5000` can delete the model registry.

---

## 4. The Dockerfile, decision by decision

```
FROM python:3.12-slim
```
`slim` over `alpine`: alpine uses musl libc, and scientific Python wheels are
built for glibc — on alpine, numpy, scipy and xgboost compile from source, which
is slow and fragile. `slim` keeps glibc and still drops ~700 MB versus the full
image. `3.12` matches the development interpreter exactly.

```
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PROJECT_ROOT=/app
```

| Variable | Why |
|---|---|
| `PYTHONUNBUFFERED=1` | Without it `print()` sits in a block buffer and **container logs stay empty until the process exits** — a hung training run looks identical to a silent one. This was observed for real earlier in the project |
| `PYTHONDONTWRITEBYTECODE=1` | No `.pyc` in a layer that is discarded anyway |
| `PIP_NO_CACHE_DIR=1` | pip's wheel cache would otherwise be baked into the image |
| `PROJECT_ROOT=/app` | `config_loader` resolves paths from this rather than `__file__`, because MLflow's `code_paths` shadows the `src` package (see `INTERNALS.md` §2) |

```
RUN apt-get install -y --no-install-recommends libgomp1 curl && rm -rf /var/lib/apt/lists/*
```
`libgomp1` is XGBoost's OpenMP runtime — non-optional. `curl` exists solely for
the healthcheck. `--no-install-recommends` and deleting the apt lists in the
**same layer** keeps them out of the image.

```
COPY requirements.txt .
RUN pip install ... && pip uninstall -y nvidia-nccl-cu13 && find ... -delete
```
**Two separate ideas here, both important.**

*Layer ordering.* Requirements are copied and installed **before** the source.
Docker caches layers by content, so editing `src/` invalidates only the last two
layers and rebuilds in seconds. Copying source first would reinstall all 34
dependencies on every code change — 85 seconds, every time.

*Same-layer cleanup.* `nvidia-nccl-cu13` arrives transitively via `xgboost` and
weighs **288 MB**, but is only used for multi-GPU collective operations. This
project trains on CPU (`tree_method="hist"`), verified to work without it. It
must be uninstalled **in the same `RUN`** — a later layer cannot delete bytes
from an earlier one, it only adds a whiteout entry, and the image keeps both.

```
COPY configs/ ./configs/
COPY src/ ./src/
```
Only what runtime needs. `data/`, `notebooks/`, `docs/`, `mlartifacts/` and
`.git/` are excluded by `.dockerignore` — see §5.

```
RUN useradd app && chown -R app:app /app
USER app
```
Non-root. Nothing here needs privileges, and a container that cannot write
outside its own directories limits the blast radius of a compromised
dependency. This choice interacts with volume permissions — see §6.

```
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
`--host 0.0.0.0` binds inside the *container*, which is required for the port
mapping to reach it. The **host-side** restriction to loopback happens in
compose. Two different bindings, easy to confuse.

---

## 5. `.dockerignore` — a 15 MB and a correctness win

The build context is everything sent to the daemon before the build starts.

| | Context size |
|---|---|
| Original `.dockerignore` (5 lines) | **15.2 MB** |
| Rewritten | **0.1 MB** |

The worst offender was **`mlartifacts/` at 13 MB**, and that is a *correctness*
problem as much as a size one: baking a stale copy of the model bundle into the
image risks it shadowing the mounted volume. `.git/`, `docs/`, `results/` and
`mlflow.db` were also being shipped.

Also excluded, and non-negotiable: **`.env`**. Secrets must never enter an image
layer — layers are distributable and `docker history` can expose them.

---

## 6. The step-by-step approach actually taken

Not the idealised order — the real one, including what went wrong.

```
STEP 1  Rename and write the Dockerfile
        DockerFile -> Dockerfile          (case matters to docker build)
        + PYTHONUNBUFFERED, libgomp1, non-root user, layer ordering

STEP 2  Write docker-compose.yml
        four services from one image, differing only by command
        ui + train behind profiles          (ui isn't written yet)
        ports -> 127.0.0.1                  (not 0.0.0.0)
        healthcheck -> /health              (asserts the MODEL loaded)
        train: restart "no"                 (a batch job must not retry forever)

STEP 3  Rewrite .dockerignore
        15.2 MB -> 0.1 MB; exclude mlartifacts (stale-model hazard) and .env

STEP 4  Validate before building
        docker compose config --quiet       ✅ valid
        docker compose config --services    ✅ only api + mlflow by default
        ▲ cheap, catches YAML and interpolation errors in a second

STEP 5  Build
        docker compose build api            ✅ all layers
        ▲ libgomp1 worked; all 34 pinned deps installed as wheels,
          nothing compiled from source

STEP 6  Smoke-test imports inside the container
        docker run --rm ... python -c "import xgboost, mlflow, src.api.main"
        ▲ proves the environment before involving networking or volumes

STEP 7  Run it — and hit BUG 1
        docker run ... -v ./mlartifacts:/app/mlartifacts
        /health -> 503  "No such artifact: ''"
        ▲ MLflow recorded an ABSOLUTE HOST PATH in mlflow.db

STEP 8  Mount at the recorded host path — and hit BUG 2
        -v ./mlartifacts:/home/<user>/<project>/mlartifacts
        /health -> 503  "[Errno 13] Permission denied: registered_model_meta"
        ▲ host uid 1001 vs container uid 1000, and MLflow WRITES that file

STEP 9  Confirm the image is fine
        docker run --user "$(id -u):$(id -g)" ...
        /health -> ok, model_loaded=true
        /predict -> 33.81 33.68 33.36  == identical to the host
        ▲ isolates "the image is good" from "the mounts are wrong"

STEP 10 Fix both properly, not with flags
        config_loader._resolve_artifact_location() -> s3:// under ENV=production
        compose: user "${HOST_UID}:${HOST_GID}" + mount at $HOST_PROJECT_DIR
        ▲ S3 is the real fix; the compose flags only rescue the LOCAL path

STEP 11 Run through compose
        docker compose up -d api            ✅ healthy
        /predict                            ✅ identical predictions

STEP 12 Slim it
        drop nvidia-nccl-cu13 in the install layer, prune tests/ and *.pyc
        2.65 GB -> 1.64 GB, predictions unchanged, still healthy
```

### The two bugs, and why containerising was worth it

Both are recorded in `CORRECTIONS.md` findings 13-15. Neither could have been
found by running on the host, and both are real portability defects rather than
Docker problems.

**Bug 1 — MLflow bakes absolute paths.** `artifact_location` is recorded on the
experiment at *creation* time, and MLflow stores absolute paths thereafter:

```
mlflow.db  ->  /home/sarthakgupta/quantitave_forecasting/mlartifacts
container  ->  /home/sarthakgupta : No such file or directory
               /app/mlartifacts   : exists, but MLflow never looks there
```

*Fix:* `artifact_location` is now environment-aware and becomes
`s3://<bucket>/mlflow-artifacts` under `ENV=production` — one URI that resolves
identically from any machine, any user, with no mount.

*Residual limitation:* MLflow cannot relocate an **existing** experiment, so the
S3 location applies to experiments created in production.

**Bug 2 — uid mismatch on a bind mount.** Host uid **1001**, container `app`
user **1000**. And MLflow **writes** `registered_model_meta` into the artifact
directory when loading a `models:/` URI, so read permission is not enough.

*Fix:* compose passes `user: "${HOST_UID}:${HOST_GID}"`. Note `UID` is readonly
in bash, hence the `HOST_` prefix. **The S3 artifact root removes the need
entirely** — no bind mount, no uid mapping, no local write.

**Both diagnosed themselves.** The API starts *unhealthy* and reports the exact
cause rather than crash-looping — the deliberate design choice in
`INTERNALS.md` §11 paying for itself.

---

## 7. Verified state, and what is still open

| Check | Result |
|---|---|
| Docker / Compose | 29.2.1 / v5.0.2 |
| `docker compose config` | valid; only `api` + `mlflow` default |
| Build | all layers; `libgomp1` present, XGBoost imports |
| 34 pinned deps on `python:3.12-slim` | no wheel compiled from source |
| Runs as non-root | uid 1000 (or `$HOST_UID`) |
| `docker compose up api` | **healthy** |
| `/predict` in-container | **33.81 / 33.68 / 33.36 — identical to host** |
| `/predict` error path | GW unit error → 422 |
| Image size | **1.64 GB** (from 2.65 GB) |

### Open: split requirements per service

The API image still carries training-only dependencies:

| Package | Size | Pulled in by | API needs it? |
|---|---|---|---|
| `llvmlite` | 171 MB | `numba` ← `shap` | no |
| `pyarrow` | 149 MB | parquet I/O | **yes** |
| `plotly` | 39 MB | Streamlit UI | no |
| `streamlit` | 30 MB | UI service | no |
| `statsmodels` | ~30 MB | notebook EDA only | no |
| `optuna` | small | tuning | no |

Splitting into `requirements-base/api/train.txt` would remove roughly **270 MB**
more from the serving image. This is the per-service-image recommendation in
`MLOPS.md` step 10, and it matters for the AWS free tier where `t3.micro` ships
an 8 GiB EBS volume by default.

**The tension to be aware of:** separate images reintroduce the version-drift
risk that one image was chosen to eliminate. The safe form is a **shared
`requirements-base.txt` with pinned versions**, plus thin per-service additions —
never independently resolved dependency sets.

---

## 8. Command reference

```bash
# --- local, ENV=local -------------------------------------------------------
export HOST_PROJECT_DIR=$PWD HOST_UID=$(id -u) HOST_GID=$(id -g)

docker compose config --quiet          # validate before building
docker compose build api               # build (cached after the first run)
docker compose up -d api               # start the API
docker compose ps                      # status, including health
docker compose logs -f api             # follow logs
docker compose down                    # stop and remove

# --- ad-hoc -----------------------------------------------------------------
docker compose exec api bash                       # shell into a running container
docker run --rm epex-forecaster:latest python -c "import xgboost"
docker compose --profile train run --rm train      # one training run, then exit
docker compose --profile ui up -d ui               # once src/ui/app.py exists

# --- inspection -------------------------------------------------------------
docker images epex-forecaster
docker inspect --format '{{.State.Health.Status}}' <container>
docker run --rm epex-forecaster:latest \
  du -sm /usr/local/lib/python3.12/site-packages/* | sort -rn | head

# --- housekeeping -----------------------------------------------------------
docker system df                        # what disk is being used
docker builder prune                    # drop the build cache
```

### Two things that will bite you

1. **The proxy.** In this environment `curl localhost:8000` is intercepted by an
   HTTP proxy and returns `504`. Use `curl --noproxy '*' localhost:8000/...`.
   Not a container problem, but it looks exactly like one.
2. **`docker compose up` with no `HOST_PROJECT_DIR`** falls back to `/app`, the
   artifact mount lands in the wrong place, and the API starts unhealthy with
   `No such artifact`. Export the three variables, or set `ENV=production` and
   let S3 handle it.

---

## 9. The path to EC2

Docker is what makes the deployment in `AWS_S3_EC2.md` Part 5 step 6 a short
job rather than an afternoon of SSH:

```
1. launch t3.large, Amazon Linux
2. install docker + compose plugin        (via user-data, so it is reproducible)
3. attach an IAM role                      s3:GetObject / s3:PutObject, one bucket
4. clone the repo, set ENV=production
       ▲ artifact_location becomes s3:// — no volume mount, no uid mapping
5. docker compose up -d api mlflow
6. Nginx in front: :443 public, proxy to 127.0.0.1:8000 and :8501
7. security group: 443 from anywhere, 22 from your IP only
```

**Two cautions carried over.** The 12-month free tier is `t2.micro`/`t3.micro`
with **1 GiB RAM** — not enough for three containers plus a training run; either
train locally and deploy only the API, or pay roughly $60/month for a
`t3.large`. And **bake the model into the image** (or cache it on EBS) so MLflow
is not a boot dependency: a running API survives MLflow being down, but a
*restarting* one does not.
