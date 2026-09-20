# Production ML Architecture — Learning Guide

This guide collects trusted, production-oriented resources for understanding the
architecture used by this project. The emphasis is on system boundaries,
lifecycles, flows and operational requirements rather than copying code.

The best fit is not a single "copy this tutorial" walkthrough. This project
combines a self-hosted inference API, MLflow, S3, Docker, GitHub Actions, ECR
and EC2. The resources below provide one strong conceptual anchor followed by
official references for the two most important implementation domains: AWS and
MLflow.

## 1. Primary resource — Full Stack Deep Learning

Start with [Full Stack Deep Learning — Lecture 5: Deployment](https://fullstackdeeplearning.com/course/2022/lecture-5-deployment/).

This is the closest conceptual match to the project. It covers:

- separating the UI from the model service;
- online model-as-a-service versus batch inference;
- REST prediction APIs;
- loading and packaging models with their dependencies;
- Docker images, hosts and registries;
- CPU versus GPU serving;
- scaling and concurrency;
- model rollout and rollback strategies;
- when simple infrastructure is sufficient and when managed services become
  useful.

Its recommendation to begin with a simple deployment and add complexity only
when requirements demand it matches this project's decision to use EC2 and
Docker Compose rather than Kubernetes.

### Translation into this project

| General concept | This project |
|---|---|
| User-facing application | Streamlit |
| Model-as-a-service | FastAPI |
| REST prediction endpoint | `POST /predict` |
| Packaged inference program | `PriceForecaster` MLflow bundle |
| Container images | Docker `serve`, `ui` and training targets |
| Container registry | Amazon ECR |
| Docker host | Amazon EC2 |
| Model dependencies | XGBoost, scikit-learn, cascade code and pinned Python packages |
| Model rollout | MLflow model version/alias plus API restart |
| Durable application data | Amazon S3 |
| Simple initial deployment | One EC2 instance with Docker Compose |
| Later scaling option | ECS/Fargate, SageMaker or multiple instances |

While watching, translate the generic "model service" into this system's
FastAPI container.

## 2. Production architecture reference — AWS Machine Learning Lens

Read these official AWS sections:

1. [ML lifecycle architecture diagram](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/architecture-diagram.html)
2. [Model deployment](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/deployment.html)

The architecture describes the standard production lifecycle:

```text
Data
  ↓
Prepare and engineer features
  ↓
Train → tune → evaluate
  ↓
Register model and artifacts
  ↓
Deploy inference service
  ↓
Monitor predictions and performance
  ↓
Trigger retraining
```

It identifies the model registry, artifact storage, container registry,
inference endpoint, CI/CD pipeline, scheduler, monitoring, lineage and
retraining feedback loop. Those are the same conceptual boxes found in this
repository, although the AWS guide often implements them with SageMaker rather
than self-hosted MLflow.

### Translation into this project

| AWS lifecycle component | Current project implementation |
|---|---|
| Data lake | S3 prefixes |
| Data preparation pipeline | Preprocessing and feature modules |
| Training pipeline | `train_pipeline.py` |
| Experiment tracking | MLflow runs |
| Model registry | MLflow Model Registry |
| Model artifact storage | S3 `mlflow-artifacts/` |
| Container repository | ECR |
| Production endpoint | FastAPI on EC2 |
| Application | Streamlit |
| CI pipeline | GitHub Actions `ci.yml` |
| Image-release pipeline | GitHub Actions `publish.yml` |
| Scheduled retraining | Planned, not implemented |
| Production monitoring | Planned, not implemented |
| Deployment gate | Planned, not implemented |
| Lineage | Partial: Git, MLflow metadata, image SHA and S3 objects |

This reference places the repository between "model development complete" and
"initial deployment": registration and packaging exist, while production
monitoring, automated retraining and the feedback loop do not.

## 3. Exact component reference — MLflow architecture

Read [MLflow's official Architecture Overview](https://mlflow.org/docs/latest/self-hosting/architecture/overview/).

It explains this essential separation:

```text
MLflow tracking server
        │
        ├── Backend store
        │     experiment metadata
        │     parameters, metrics, runs and model versions
        │     SQLite now; PostgreSQL/RDS later
        │
        └── Artifact store
              model bundles, plots and result files
              S3 in production
```

The stores are not interchangeable:

- SQLite/PostgreSQL stores relatively small structured metadata.
- S3 stores large objects such as model bundles, SHAP plots and result files.
- The MLflow server provides the API and UI used to access that information.

This distinction also exposes the current durability gap: S3 objects survive
EC2 termination, but an MLflow SQLite database on an unsnapshotted EBS volume
does not.

## 4. CI/CD reference

For release-flow concepts, read the architecture and solution-overview sections
of [Build an end-to-end MLOps pipeline using GitHub and GitHub Actions — AWS](https://aws.amazon.com/blogs/machine-learning/build-an-end-to-end-mlops-pipeline-using-amazon-sagemaker-pipelines-github-and-github-actions/).

Do not follow its SageMaker-specific implementation literally. Use it to
understand three separate pipelines:

```text
Code CI
    test code and build images

Model pipeline
    prepare → train → evaluate → register

Release pipeline
    approve → deploy → validate → promote
```

This repository performs the first pipeline and much of the second. Its publish
workflow is designed to push Docker images into ECR, but the final automated
delivery step—pulling the chosen image onto EC2 and safely restarting it—is not
built yet.

## 5. Recommended study order

1. Watch the Full Stack Deep Learning deployment lecture for the mental model.
2. Study the AWS lifecycle architecture diagram for the complete production
   loop.
3. Read the MLflow architecture overview to understand metadata versus
   artifacts.
4. Read only the solution overview of the AWS GitHub Actions article to
   understand build, registration, approval and deployment boundaries.
5. Return to the project documentation in this order:
   - `DESIGN.md` — service topology and the served model artifact;
   - `MODEL_RELEASE.md` — the concrete model selected, exported and verified
     for deployment;
   - `DEPLOYMENT_PHASES_2_3.md` — how that immutable model is bundled into the
     API image and validated without external state;
   - `DEPLOYMENT_PHASE_4.md` — how the verified Linux/amd64 image moves through
     GitHub Actions and OIDC into ECR;
   - `AWS_S3_EC2.md` — state versus compute and the target AWS topology;
   - `DOCKER.md` — container responsibilities;
   - `CI.md` — test and image-release flows;
   - `ROADMAP.md` — implemented versus planned.

The Full Stack Deep Learning lecture is the conceptual anchor. The AWS Machine
Learning Lens describes a mature production lifecycle. The local documentation
shows the deliberately smaller implementation selected for this project.
