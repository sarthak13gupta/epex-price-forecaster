# Deployment Phase 4 — Verified Image Release to ECR

This is the implementation record and operator runbook for moving the bundled
inference image from a locally verified candidate to an immutable ECR release.

Last updated: **2026-09-22**.

## Status

The Phase-4 release mechanism is **implemented and locally validated**. The
source commits and model asset are published on GitHub. AWS publication is
pending the administrator profile, OIDC role and repository variables.

The required CLIs were installed with Homebrew during this phase:

| CLI | Installed version | Authentication state |
|---|---|---|
| GitHub CLI | `gh 2.101.0` | Authenticated as `sarthak13gupta` |
| AWS CLI | `aws-cli 2.36.49` | No profile or static credential configured |

Homebrew installed Python 3.14 as an AWS CLI dependency. Project commands still
use the isolated Python 3.12 environment at `venv/bin/python`; the model and
tests were not migrated to the Homebrew interpreter.

| Phase-4 component | State |
|---|---|
| Deterministic model archive | Complete |
| Archive and extracted-tree verification | Complete |
| Versioned release descriptor | Complete |
| Linux/amd64 bundled image build | Complete locally |
| Phase-3 checks against amd64 image | Passed locally |
| Source changes on `main` | Published through commit `30b1f4d` |
| GitHub Actions build/verify/push workflow | Implemented, awaiting AWS setup |
| GitHub model Release asset | Uploaded and digest-verified |
| AWS OIDC role and repository variables | Account-owner setup still required |
| ECR digest | Not available until the first successful publish |

### Manual interruptions

Only account-bound actions require human input:

1. **GitHub device authorization — complete.** `gh auth login --web` was
   approved in the browser. No password or token was put in project files.
2. **AWS administrator authentication.** Configure an administrator profile or
   use your organization's AWS SSO flow. The current machine has no AWS profile,
   and `.env` contains region/configuration only—no AWS key. Do not use the
   S3-only pipeline identity for IAM setup.
3. **ECR vulnerability review.** After publication, a human must decide whether
   any scan finding is acceptable. Automation can retrieve findings but should
   not approve security risk on the owner's behalf.

The archive upload is complete. Repository variables, workflow dispatch and ECR
digest verification can proceed after item 2 is satisfied.

## Release architecture

```text
Local Phase-1 model directory (gitignored)
        |
        | deterministic packaging
        v
GitHub Release asset (.tar.gz)
        |
        | archive SHA-256 + extracted tree SHA-256
        v
GitHub Actions ubuntu/amd64 runner
        |
        | build bundled-serve -> run Phase-3 isolation contract
        v
same tested local image
        |
        | assume AWS role with GitHub OIDC; tag, push
        v
Amazon ECR
  :bundled                   moving convenience tag
  :bundled-<git-commit>      immutable release tag
  @sha256:<digest>           deployment identity
```

There is no S3 in this flow. GitHub Releases transports the approved model into
the clean runner. ECR stores the finished application image. At runtime the
model remains `/app/model` inside the read-only image and EC2 needs no model
store, registry database or AWS model-download permission.

## 4.1 Prepare the model release asset

The version-controlled descriptor is
[`release/model-release.json`](../release/model-release.json). It binds together:

- numeric model version `1`;
- the expected in-repository artifact path;
- the model-tree checksum from Phase 1;
- the GitHub Release tag and asset name;
- the compressed archive checksum.

Create the archive from the already verified Phase-1 directory:

```bash
venv/bin/python scripts/model_release_archive.py create \
  --source artifacts/releases/french_spot_price_forecaster-v1 \
  --output dist/model-releases/french_spot_price_forecaster-v1.tar.gz \
  --archive-root artifacts/releases/french_spot_price_forecaster-v1
```

Current identity:

| Field | Value |
|---|---|
| Asset | `french_spot_price_forecaster-v1.tar.gz` |
| Archive SHA-256 | `3ff2c977e91cbe43081fa6e4c86ac478bdbd6d717833aa874f556624001c71a8` |
| Model-tree SHA-256 | `c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132` |
| Archive size | about 193 KiB |
| Model files / bytes | 39 / 642,818 |

The packager normalizes timestamps, ownership and modes. Two consecutive local
builds produced the same archive hash. The archive itself stays gitignored.

Before upload, prove extraction into a clean directory:

```bash
release_tmp="$(mktemp -d /tmp/epex-model-release.XXXXXX)"
venv/bin/python scripts/model_release_archive.py extract \
  --archive dist/model-releases/french_spot_price_forecaster-v1.tar.gz \
  --destination "$release_tmp" \
  --expected-root artifacts/releases/french_spot_price_forecaster-v1 \
  --expected-archive-sha256 3ff2c977e91cbe43081fa6e4c86ac478bdbd6d717833aa874f556624001c71a8 \
  --expected-tree-sha256 c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132
```

The extractor rejects absolute paths, `..`, links, special files, unexpected
top-level paths, an existing destination, and either checksum mismatch.

## 4.2 Publish the model asset to GitHub Releases

Do this only after the descriptor and release tooling have been committed and
pushed. With GitHub CLI installed and authenticated:

```bash
gh release create model-french-spot-price-forecaster-v1 \
  dist/model-releases/french_spot_price_forecaster-v1.tar.gz \
  --repo sarthak13gupta/epex-price-forecaster \
  --target main \
  --title "Model: french_spot_price_forecaster v1" \
  --notes "Immutable Phase-1 candidate. Verify against release/model-release.json."
```

If the release already exists, upload without replacing anything silently:

```bash
gh release upload model-french-spot-price-forecaster-v1 \
  dist/model-releases/french_spot_price_forecaster-v1.tar.gz \
  --repo sarthak13gupta/epex-price-forecaster
```

Do not use `--clobber`. A changed asset must become a new model version with a
new tag, descriptor and checksums. GitHub assets can be replaced by a maintainer,
so the workflow never trusts the tag or filename alone; it verifies both hashes.

### Published GitHub evidence

The release was created successfully at:

```text
https://github.com/sarthak13gupta/epex-price-forecaster/releases/tag/model-french-spot-price-forecaster-v1
```

GitHub reports asset ID `581689794`, size `197,752` bytes, state `uploaded`, and
digest `sha256:3ff2c977e91cbe43081fa6e4c86ac478bdbd6d717833aa874f556624001c71a8`.
That digest exactly matches the committed descriptor and local archive.

## 4.3 Create the ECR/OIDC boundary

Run with an AWS administrator identity, never the S3 pipeline user:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=admin
aws sts get-caller-identity

REPO=sarthak13gupta/epex-price-forecaster \
  ./infra/iam/apply-github-oidc.sh
```

The idempotent script creates:

- the GitHub OIDC identity provider, if absent;
- the scan-on-push `epex-forecaster` ECR repository, if absent;
- `github-actions-ecr-push`, restricted to this GitHub repository;
- an inline policy that can push to this ECR repository but cannot delete it.

Set the three values printed by the script as GitHub repository **variables**:

| Variable | Purpose |
|---|---|
| `AWS_ROLE_ARN` | Role GitHub exchanges its OIDC token for |
| `AWS_REGION` | ECR repository region |
| `ECR_REPOSITORY` | Repository name, normally `epex-forecaster` |

No AWS access key is stored in GitHub.

## 4.4 What the workflow does

`.github/workflows/publish.yml` runs manually or for application tags matching
`v*`. A model-release tag begins with `model-`, so uploading the model does not
accidentally publish an application image.

The workflow performs this ordered gate:

1. Validate the committed release descriptor.
2. Confirm all three AWS repository variables exist.
3. Download the exact GitHub Release asset using the built-in GitHub token.
4. Verify its archive checksum, safely extract it, and verify its model-tree
   checksum.
5. Build target `bundled-serve` explicitly for `linux/amd64`.
6. Load that image into the runner and execute the complete Phase-3 isolation,
   prediction and restart contract.
7. Only after those checks pass, request short-lived AWS credentials through
   OIDC and log in to ECR.
8. Tag and push the **same tested local image** as `:bundled` and
   `:bundled-<git-commit>`; it is not rebuilt after testing.
9. Query ECR for the authoritative digest.
10. Retain `release-evidence.json` for 90 days and write the model identity,
    image tag, platform and ECR digest into the workflow summary.

Trigger the first application release only after the asset and AWS variables
exist:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Alternatively, run **Publish bundled inference image** using GitHub's Actions
page. If AWS variables are absent, preflight reports why and the publish job is
skipped without requesting credentials.

## 4.5 Local Linux/amd64 evidence

The exact target platform planned for an EC2 `t3` instance was built locally:

```bash
docker buildx build \
  --platform linux/amd64 \
  --target bundled-serve \
  --build-arg REQUIREMENTS=requirements-api.txt \
  --build-arg MODEL_ARTIFACT=artifacts/releases/french_spot_price_forecaster-v1 \
  --build-arg MODEL_TREE_SHA256=c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132 \
  --load -t epex-forecaster:bundled-amd64-candidate .

IMAGE=epex-forecaster:bundled-amd64-candidate SKIP_BUILD=1 \
  bash scripts/verify_bundled_container.sh
```

Results:

| Check | Result |
|---|---|
| Platform | Linux/amd64 |
| Image ID | `sha256:dadfb9e4750c015ca7ce849b7952f7105e1101a2e5824bda162141737035d48e` |
| Local image size | 293,646,821 bytes |
| Model checksum | Exact match |
| Five prediction fixture | Exact match, maximum error `0.0` |
| Network / mounts / writable root | None / none / false |
| Database / training process | Absent / absent |
| Restart | Healthy |

This local image ID is not an ECR identity. The digest emitted by the first
successful publish workflow becomes the deployment pin.

The first amd64 build downloads then removes XGBoost's roughly 252 MB NCCL
wheel. It makes a cold build slower but does not remain in the final layer. The
GitHub Actions cache makes later builds substantially faster.

## 4.6 Deployment and rollback contract

EC2 must pull by digest, not by the moving tag:

```text
<account>.dkr.ecr.<region>.amazonaws.com/epex-forecaster@sha256:<digest>
```

`:bundled` is useful for humans but can move. `:bundled-<git-commit>` connects
an image to source; the ECR digest identifies the exact bytes. Rollback means
selecting the previous successful evidence file/digest and restarting the API
with that digest. It does not mean rebuilding an old commit.

## Exit criteria

Phase 4 is fully complete only when all of the following evidence exists:

- model Release asset visible at the descriptor's tag — **complete**;
- successful remote workflow run;
- `release-evidence.json` retained by that run;
- ECR contains `bundled-<git-commit>`;
- recorded ECR digest matches `aws ecr describe-images`;
- ECR scan findings have been reviewed.

Until the remaining criteria pass, the correct status is **GitHub release
complete, AWS publication pending**.
