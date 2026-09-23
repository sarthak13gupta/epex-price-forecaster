# Production ML Architecture — Learning Guide

This is the junior-friendly study companion for the architecture actually built
in this repository. It emphasizes system boundaries, release evidence and
operational reasoning rather than copying code.

The broader repository contains training, optional S3 integration and local
MLflow. The first production deployment is deliberately smaller: an immutable
model is bundled with FastAPI, verified as a self-contained container,
published by GitHub Actions to Amazon ECR, and now running privately on EC2.
The deployed service needs no S3, MLflow server, database, training process or
host model disk.

## Current checkpoint — Phase 5 complete

```text
MLflow model v1 (offline source)
  → deterministic checksummed GitHub Release asset
  → GitHub Actions builds Linux/amd64
  → networkless/read-only prediction contract
  → GitHub OIDC exchanges for temporary AWS credentials
  → ECR push + immutable digest + vulnerability scan
  → ECR-pull-only EC2 role
  → digest-pinned private EC2 host in Tokyo
  → healthy model load + real prediction evidence
```

The published image identity is:

```text
955519187689.dkr.ecr.ap-northeast-1.amazonaws.com/epex-forecaster
@sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0
```

Tags help people find an image; the digest identifies the exact bytes. The
release evidence and scan review are in `DEPLOYMENT_PHASE_4.md`. The exact EC2
design, scripts and permission preflight are in `DEPLOYMENT_PHASE_5.md`.

## 1. Conceptual anchor — Full Stack Deep Learning

Start with [Full Stack Deep Learning — Lecture 5: Deployment](https://fullstackdeeplearning.com/course/2022/lecture-5-deployment/).

It covers the ideas that matter here:

- online model-as-a-service versus batch inference;
- REST prediction APIs;
- packaging models with their dependencies;
- Docker images, hosts and registries;
- CPU versus GPU serving;
- rollout and rollback;
- starting with simple infrastructure and adding complexity only when required.

### Translation into this project

| General concept | This project |
|---|---|
| Model-as-a-service | FastAPI |
| Prediction endpoint | `POST /predict` |
| Packaged model | `PriceForecaster` bundle copied to `/app/model` |
| Deployment container | Docker `bundled-serve` target |
| Container registry | Amazon ECR in Tokyo |
| Docker host | Amazon EC2 — Phase 5 running privately in Tokyo |
| Runtime dependencies | XGBoost, scikit-learn, cascade code and pinned Python packages |
| Model rollout | New model release → tested image → ECR digest |
| Runtime model/data store | None; the model is inside the image |
| Initial deployment | One EC2 instance running the digest-pinned API |
| Later scaling option | ECS/Fargate, SageMaker or multiple instances |

## 2. Mature lifecycle reference — AWS Machine Learning Lens

Read these official AWS sections:

1. [ML lifecycle architecture diagram](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/architecture-diagram.html)
2. [Model deployment](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/deployment.html)

The mature lifecycle is:

```text
Data → prepare → train → evaluate → register → release → serve
  ↑                                                       ↓
  └──────────── monitor → approve → retrain ──────────────┘
```

This project has completed image release and initial private host deployment:

| Lifecycle component | Current implementation |
|---|---|
| Data preparation and training | Local pipeline; optional S3 paths |
| Experiment tracking/registry | Local MLflow; source of model v1 |
| Deployment artifact transport | Checksummed GitHub Release asset |
| Container repository | ECR — published |
| Production endpoint | FastAPI on EC2 loopback — healthy; public HTTPS not added |
| Image CI/release | GitHub Actions — complete through ECR |
| Release lineage | Model hashes + Git SHA + workflow evidence + ECR digest |
| Monitoring/retraining | Planned, not implemented |

### The claim this evidence supports

It is accurate to say **the core AWS inference deployment and the MLflow-backed
model-release path are complete**. More precisely: MLflow tracked/registered
model v1 offline; the approved artifact was exported into an immutable image;
GitHub OIDC published it to ECR; and EC2 pulled and served its exact digest.

It is not yet accurate to call this a fully operated public production service.
FastAPI is already deployed and working, but public HTTPS, monitoring/alerts,
automated delivery/rollback and Streamlit hosting remain outside Phase 5. An
MLflow server is absent intentionally—it is not a serving dependency.

## 3. Where MLflow belongs—and where it does not

Read [MLflow's Architecture Overview](https://mlflow.org/docs/latest/self-hosting/architecture/overview/).

MLflow normally separates a tracking server, metadata backend and artifact
store. Those remain useful for offline experimentation and registration. They
are deliberately absent from the first serving host:

```text
offline:  MLflow model v1 → export once
runtime:  /app/model inside the verified image → predict locally
```

This removes three request-time failure modes: tracking-server availability,
database availability and remote artifact download. It also means a new model
requires a new verified image rather than changing an alias behind a running
service.

## 4. CI, release and delivery are different pipelines

Read the overview in [AWS: Build an end-to-end MLOps pipeline using GitHub Actions](https://aws.amazon.com/blogs/machine-learning/build-an-end-to-end-mlops-pipeline-using-amazon-sagemaker-pipelines-github-and-github-actions/).

Do not copy its SageMaker implementation literally. Learn the separation:

```text
Code CI       test source and container contracts
Model flow    train → evaluate → register/export
Release       build → verify → scan → publish immutable image
Delivery      pull chosen digest → restart → validate → rollback if needed
```

This repository completes the release pipeline through ECR and the first
private EC2 delivery. Instance `i-0290d8f3733e43a43` pulled the fixed digest,
loaded the bundled model and returned the Phase-3 reference prediction. The
next boundary is safe public HTTPS, not another model-serving mechanism.

## 5. Phase-5 concepts to understand

### The instance role is not the deployment identity

The identity launching EC2 needs temporary provisioning permissions. The
identity running on EC2 needs only runtime permissions. Keeping them separate
prevents an API-host compromise from gaining permission to create or terminate
infrastructure.

AWS represents the runtime attachment with two objects:

```text
EC2 instance → instance profile → one IAM role → ECR-pull policy
```

Read [AWS: IAM roles for Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html),
then compare `apply-ec2-ecr-pull.sh`, `trust-policy-ec2.json` and
`policy-ecr-pull.json`.

### Booted is not deployed

EC2 user data installed Docker and started the fixed digest on first boot. The
acceptance marker was written only after the container became healthy and a
real prediction succeeded. Read [AWS: EC2 user data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html),
then follow `user-data.sh` from ECR login to the recorded `PHASE5_OK` evidence.

### Layered network controls

At this checkpoint both layers deny public requests:

```text
security group: no inbound packets
Docker publish:  127.0.0.1:8000 only
```

A public IP is not equivalent to a public service. It provides outbound access
for bootstrap, while the security group and loopback bind still block inbound
API traffic. Phase 6 must add a deliberate HTTPS path rather than casually
opening port 8000.

### Metadata and temporary credentials

The host AWS CLI obtains temporary role credentials through IMDSv2. Token use is
required and the response hop limit is 1 because the container itself never
needs AWS credentials. AWS generally recommends considering hop limit 2 for
containers that do need metadata; this service deliberately chooses the more
restrictive case. Read [AWS: configure IMDS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html).

## 6. Phase-4 concepts to understand

### Artifact identity

A filename is not identity because it can be replaced. This project verifies:

1. archive SHA-256 — downloaded bytes;
2. model-tree SHA-256 — safely extracted model directory;
3. ECR digest — exact released container bytes.

Read [Docker build best practices](https://docs.docker.com/build/building/best-practices/)
for digest pinning, then compare `release/model-release.json` with
`DEPLOYMENT_PHASE_4.md`.

### OIDC and temporary credentials

GitHub stores no AWS access key. A workflow requests a signed OIDC token; AWS
validates its audience and repository-specific subject, then returns a
short-lived role session. Read
[GitHub: Configuring OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
beside `.github/workflows/verify-aws-oidc.yml` and the IAM trust policy.

Remember:

```text
trust policy       who may assume the role
permission policy  what the assumed role may do
```

### Least privilege

Read [AWS IAM: grant least privilege](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html),
[AWS IAM: temporary credentials](https://docs.aws.amazon.com/IAM/latest/UserGuide/security-creds.html),
and the [`AdministratorAccess` policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AdministratorAccess.html).

`AdministratorAccess` is effectively every action on every resource. It was
useful for one-time bootstrap but is dangerous on a long-lived pipeline access
key. The steady-state identities should be:

| Identity | Required permission |
|---|---|
| GitHub Actions | Push only to `epex-forecaster` through OIDC |
| Phase-5 EC2 | Pull only from that repository through an instance role |
| Offline pipeline user | Only specific data bucket/prefixes still in use |

### Container scanning and remediation

Read [Amazon ECR image scanning](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-scanning.html).
Basic scanning checks OS packages at push time. Enhanced scanning with Amazon
Inspector can continuously scan OS and language packages.

A scanner finding begins an investigation; severity alone is not the entire
decision:

1. Confirm the affected package/function exists.
2. Determine whether attacker-controlled input can reach it.
3. Check the distribution vendor for a fixed package.
4. Remove unnecessary packages where possible.
5. Rebuild, rerun the ML prediction contract and rescan.
6. If no supported fix exists, document scope, controls and the rebuild trigger.

That process removed curl, reducing the ECR report from six findings—including
two critical—to two. For the remaining results:

- zlib is required by Python/core Debian packages; the vulnerable native
  non-blocking `gzwrite` path is unused and Debian has no fixed candidate;
- `perl-base` is essential, but the reported vulnerable `Pod::Text` module is
  not installed and the API never invokes Perl.

Neither result was hidden or suppressed. Rebuild and rescan when Debian ships a
fix. A base-image migration is a separate release change because it must prove
XGBoost and MLflow deserialization compatibility and reproduce predictions.

## 7. Reproduce the Phase-4 evidence read-only

```bash
gh run view 35762796347

AWS_PROFILE=admin aws ecr describe-images \
  --region ap-northeast-1 \
  --repository-name epex-forecaster \
  --image-ids imageTag=bundled-8ab83d041327fa912f12a1e1cd4820b874e32f74

AWS_PROFILE=admin aws ecr describe-image-scan-findings \
  --region ap-northeast-1 \
  --repository-name epex-forecaster \
  --image-id imageDigest=sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0
```

These queries read evidence. They do not deploy the image or approve risk.

## 8. Recommended study order

1. Watch the Full Stack Deep Learning deployment lecture.
2. Read the AWS Machine Learning Lens lifecycle diagram.
3. Read the MLflow architecture overview and identify what stays offline.
4. Study GitHub OIDC, then compare trust and permission policies locally.
5. Study IAM least privilege and explain why bootstrap admin must be removed.
6. Read the ECR scanning guide and reproduce the final scan report.
7. Study EC2 roles, user data, security groups and IMDSv2.
8. Read the local documents in this order:
   - `MODEL_RELEASE.md` — model selection and immutable export;
   - `DEPLOYMENT_PHASES_2_3.md` — bundled image and state-free contract;
   - `DEPLOYMENT_PHASE_4.md` — OIDC, ECR, digest and scan evidence;
   - `DEPLOYMENT_PHASE_5.md` — private host design, automation and live status;
   - `CI.md` — test and release gates;
   - `DOCKER.md` — image/runtime responsibilities;
   - `ROADMAP.md` — implemented versus planned;
   - `AWS_S3_EC2.md` — broader AWS context and Phase-5 target.

The conceptual resources show mature industry practice. The local documents
show exactly which subset was selected, why it is smaller, what evidence exists
today and what has not been built yet.
