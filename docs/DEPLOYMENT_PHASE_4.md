# Deployment Phase 4 — Verified Image Release to ECR

This is the implementation record and operator runbook for moving the bundled
inference image from a locally verified candidate to an immutable ECR release.

Last updated: **2026-09-22**.

## Status

**Phase 4 is complete.** The checksummed model release was built into a
self-contained Linux/amd64 image, passed the Phase-3 isolation and prediction
contract, and was published to Tokyo ECR through the repository-scoped GitHub
OIDC role. The workflow evidence and ECR independently report the same digest.
Scan findings were reviewed; avoidable curl findings were removed and the two
unfixed, unreachable base-OS findings are recorded below as residual risk.

The required CLIs were installed with Homebrew during this phase:

| CLI | Installed version | Authentication state |
|---|---|---|
| GitHub CLI | `gh 2.101.0` | Authenticated as `sarthak13gupta` |
| AWS CLI | `aws-cli 2.36.49` | `admin` profile verified in `ap-northeast-1`; temporary administrator access active |

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
| Source changes on `main` | Published; final image commit `8ab83d0` |
| GitHub Actions build/verify/push workflow | Passed remotely |
| GitHub model Release asset | Uploaded and digest-verified |
| Tokyo ECR repository | Created; hardened bundled image published |
| AWS OIDC provider and push role | Created; GitHub assumption verified |
| GitHub repository variables | All three set for Tokyo |
| ECR digest | `sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0` |

### Manual interruptions

Only account-bound actions require human input:

1. **GitHub device authorization — complete.** `gh auth login --web` was
   approved in the browser. No password or token was put in project files.
2. **AWS administrator authentication — bootstrap complete temporarily.** The `admin`
   profile uses `ap-northeast-1` and resolves to `quantitative-pipeline-user`,
   which now has the AWS-managed `AdministratorAccess` policy. IAM role/OIDC
   reads, Tokyo ECR reads, and policy simulation for the required create/push
   actions passed. OIDC/ECR setup and verification are now complete. Removing
   this temporary administrator policy is the remaining manual security action;
   it is intentionally not removed without the account owner's approval.
3. **ECR vulnerability review — complete with recorded residual risk.** The
   first scan caused a hardening change rather than accepting avoidable critical
   findings. The final scan and rationale are recorded in §4.7.

The archive, identity setup, image publication, digest verification and scan
review are complete. Removing temporary administrator access remains a separate
account-security action and is not required by the running image.

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
AWS_DEFAULT_REGION=ap-northeast-1 \
  ./infra/iam/apply-github-oidc.sh
```

If `gh` authentication is unavailable in a headless shell, pass the immutable
IDs already verified from GitHub instead:

```bash
REPO=sarthak13gupta/epex-price-forecaster \
AWS_DEFAULT_REGION=ap-northeast-1 \
GITHUB_OWNER_ID=74540123 \
GITHUB_REPOSITORY_ID=1360961812 \
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

The variables were applied with GitHub CLI:

```bash
gh variable set AWS_ROLE_ARN \
  --body 'arn:aws:iam::955519187689:role/github-actions-ecr-push'
gh variable set AWS_REGION --body 'ap-northeast-1'
gh variable set ECR_REPOSITORY --body 'epex-forecaster'
gh variable list
```

### Applied AWS and GitHub state — 2026-09-22

The following resources were created in account ending `7689`:

| Resource | Applied value |
|---|---|
| AWS region | `ap-northeast-1` (Tokyo) |
| OIDC provider | `token.actions.githubusercontent.com` |
| ECR repository | `epex-forecaster` |
| IAM role | `github-actions-ecr-push` |
| Inline policy | `ecr-push` (push to this repository, no delete) |

The following repository variables were set on
`sarthak13gupta/epex-price-forecaster`:

| Variable | Value |
|---|---|
| `AWS_ROLE_ARN` | `arn:aws:iam::955519187689:role/github-actions-ecr-push` |
| `AWS_REGION` | `ap-northeast-1` |
| `ECR_REPOSITORY` | `epex-forecaster` |

These values are configuration, not credentials. The workflow receives
short-lived AWS credentials only after GitHub presents a matching OIDC token.

### AWS permission preflight history

On 2026-09-22, the following checks were run before attempting any mutation:

```bash
AWS_PROFILE=admin aws sts get-caller-identity
AWS_PROFILE=admin aws iam list-roles --max-items 1
AWS_PROFILE=admin aws iam list-open-id-connect-providers
AWS_PROFILE=admin aws ecr describe-repositories --region ap-northeast-1
```

STS succeeded, proving the key is valid, but identified
`quantitative-pipeline-user`. All three service preflights returned
`AccessDenied`. No IAM, OIDC or ECR resource was created. The profile name is
only a local label; naming it `admin` does not grant administrative permissions.

After `AdministratorAccess` was attached, the 2026-09-22 preflight was repeated:

- `iam:ListRoles` and `iam:ListOpenIDConnectProviders`: allowed;
- Tokyo `ecr:DescribeRepositories`: allowed;
- simulation of `iam:CreateRole`, `iam:CreateOpenIDConnectProvider`,
  `iam:PutRolePolicy`, `ecr:CreateRepository` and `ecr:PutImage`: allowed;
- GitHub OIDC provider: absent;
- Tokyo `epex-forecaster` ECR repository: absent.

Provisioning can therefore start from a known-empty state. Administrator access
on a pipeline identity is a bootstrap exception, not the intended steady state.

The first provisioning attempt exposed a guard defect before any resource was
changed: `apply-github-oidc.sh` rejected the identity solely because its username
contained `quantitative-pipeline-user`, despite the newly attached bootstrap
policy. The guard now checks the real `iam:ListRoles` capability instead. IAM
authorization is policy-based; a username is not evidence of current privilege.

### OIDC verification and immutable subject correction

The first verification run reached AWS but was denied
`sts:AssumeRoleWithWebIdentity`. The original trust condition expected the
legacy subject form `repo:owner/repository:*`. Inspection of only the non-secret
claims—not the signed token—showed that this repository uses GitHub's immutable
subject form:

```text
repo:sarthak13gupta@74540123/epex-price-forecaster@1360961812:ref:refs/heads/main
```

The trust policy and bootstrap script were corrected to use the numeric owner
and repository IDs. This resists repository rename/reuse ambiguity while still
allowing this repository's workflows. The live AWS condition is:

```text
repo:sarthak13gupta@74540123/epex-price-forecaster@1360961812:*
```

The separate `.github/workflows/verify-aws-oidc.yml` workflow then passed all
four gates: claim validation, role assumption, STS caller validation and ECR
repository read. Evidence:

```bash
gh workflow run verify-aws-oidc.yml --ref main
gh run watch 35761192958 --exit-status
```

| Field | Value |
|---|---|
| Run | [35761192958](https://github.com/sarthak13gupta/epex-price-forecaster/actions/runs/35761192958) |
| Result | Success |
| Tested commit | `25740d21800a5dd9ecdce2cd9cc06706fa2767bd` |
| AWS operations | STS identity and ECR read only |
| ECR image count after test | `0` |

Thus GitHub can assume the AWS role, but this test did not build or push an
image. That separation makes authentication failures cheap to diagnose.

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

## 4.7 Remote publication and vulnerability review

The first publish run completed successfully, but its scan found six OS-package
findings: 2 critical, 2 high, 1 medium and 1 undefined. Four came from installing
`curl` solely for internal container probes or its dependencies. Rather than
accepting that unnecessary surface, commit `8ab83d0` removed curl and replaced
the probes with Python's standard library. All 53 tests, Compose validation and
the complete remote Phase-3 contract passed after the change.

Final release evidence:

| Field | Value |
|---|---|
| Workflow run | [35762796347](https://github.com/sarthak13gupta/epex-price-forecaster/actions/runs/35762796347) |
| Result | Success |
| Git commit | `8ab83d041327fa912f12a1e1cd4820b874e32f74` |
| Platform | `linux/amd64` |
| ECR repository | `955519187689.dkr.ecr.ap-northeast-1.amazonaws.com/epex-forecaster` |
| Tags | `bundled`, `bundled-8ab83d041327fa912f12a1e1cd4820b874e32f74` |
| Deployment digest | `sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0` |
| Compressed image size | 289,511,806 bytes |
| Evidence artifact | `bundled-release-evidence-8ab83d041327fa912f12a1e1cd4820b874e32f74` (90-day retention) |

Both tags resolve to the recorded digest. EC2 must use the repository URI plus
the full `@sha256:2e65...126c0` digest, not either mutable tag.

### Final ECR scan

Scan-on-push completed successfully. The hardening reduced the count from six
to two and removed every critical finding:

| Severity | Before | Final |
|---|---:|---:|
| Critical | 2 | 0 |
| High | 2 | 1 |
| Medium | 1 | 0 |
| Undefined | 1 | 1 |

The remaining findings are:

| Finding | Package | Review decision |
|---|---|---|
| `CVE-2026-85091` (high) | zlib `1:1.3.dfsg+really1.3.1-1+b1` | Debian has no fixed package yet. The flaw requires the native `gzwrite`/`gzprintf` non-blocking stale-buffer path; the API does not call that path or accept gzip files. Temporarily accepted for this inference-only release; rebuild immediately when Debian publishes a fix. |
| `CVE-2026-82560` (undefined) | `perl-base` `5.40.1-6+deb13u1` | Requires formatting an attacker-controlled POD document with Pod::Text. The exact image cannot import `Pod::Text`; that module is absent. The API does not invoke Perl or accept POD input. Debian has no fixed package yet. Temporarily accepted and monitored. |

This is not a claim that the packages are generally safe. It is a
deployment-specific reachability decision backed by a read-only container,
non-root user, dropped capabilities, no network during the contract test and a
narrow JSON API. Rebuild the same commit after base-image security updates and
do not suppress either finding from future scans. Debian's security tracker
currently records both issues as unfixed:

- <https://security-tracker.debian.org/tracker/CVE-2026-85091>
- <https://security-tracker.debian.org/tracker/CVE-2026-82560>

### Why the remaining findings were not force-fixed

The exact digest was pulled from ECR and inspected, not inferred from the
Dockerfile:

- `apt-cache policy` reports the installed zlib and Perl versions as the newest
  candidates in Debian trixie;
- simulated zlib removal fails because `dpkg`, `apt`, `libssl` and core system
  packages require it, and Python imports the same zlib 1.3.1 runtime;
- `perl-base` is marked `Essential: yes`, so forced removal would create an
  unsupported base system;
- `perl-modules-5.40` and `Pod::Text` are not installed, making the scanner's
  source-package match broader than the code actually present.

There are three future remediation paths:

1. **Supported default:** when Debian publishes fixed packages, rebuild the
   unchanged application, rerun the Phase-3 contract, publish a new digest and
   review the new scan.
2. **Base-image migration:** move to another maintained Python 3.12/glibc image
   only after proving XGBoost, MLflow deserialization, non-root execution and
   the exact prediction fixture. This is not a drop-in security edit.
3. **Emergency custom patch:** build and maintain a patched zlib package. This
   makes this team responsible for an OS library and should be reserved for a
   reachable, urgent vulnerability when no vendor update exists.

Deleting package metadata to silence ECR, force-removing essential packages,
or suppressing the findings would change the report—not the risk—and is not an
acceptable fix.

## 4.8 Temporary administrator access

`quantitative-pipeline-user` still has both `AdministratorAccess` and
`AmazonS3FullAccess`. The former is especially dangerous because its policy is
effectively `Action: "*"` and `Resource: "*"`: anyone holding that user's
long-lived access key could alter IAM, create new credentials, delete ECR/EC2/S3
resources or grant another principal permanent administrator access.

Phase 4 no longer needs it. GitHub publishes through a short-lived,
repository-scoped OIDC role. Detach the bootstrap policy after explicit account
owner approval:

```bash
AWS_PROFILE=admin aws iam detach-user-policy \
  --user-name quantitative-pipeline-user \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess

AWS_PROFILE=admin aws iam list-attached-user-policies \
  --user-name quantitative-pipeline-user
```

`AmazonS3FullAccess` is a separate decision. It is unnecessary for the deployed
bundled API, but the legacy offline data pipeline may still use S3. Replace it
with a bucket/prefix-scoped policy before detaching it if that offline workflow
must continue. The Phase-5 EC2 host must receive only ECR pull permissions.

## Exit criteria

Phase 4 exit evidence:

- model Release asset visible at the descriptor's tag — **complete**;
- successful remote workflow run — **complete**;
- `release-evidence.json` retained by that run — **complete**;
- ECR contains `bundled-<git-commit>` — **complete**;
- recorded ECR digest matches `aws ecr describe-images` — **complete**;
- ECR scan findings reviewed and residual risk recorded — **complete**.

At the time this phase completed, the correct status was **Phase 4 complete;
digest-pinned EC2 deployment next**. Phase 5 subsequently deployed this exact
digest and recorded health/prediction evidence in `DEPLOYMENT_PHASE_5.md`.

## Deployment phases left to implement

These are working phase names for continuing the same documented sequence:

1. **Phase 5 — EC2 inference host.** Create a least-privilege ECR-pull instance
   role, launch an appropriately sized Tokyo EC2 instance, install Docker, and
   run the bundled API image pinned to its digest. The serving host needs no S3,
   MLflow server, database, model training or persistent model disk.
2. **Phase 6 — networking and HTTPS.** Restrict SSH to the operator's address,
   expose only HTTPS publicly, keep the application port private, add Nginx and
   TLS, and validate `/health` and `/predict` externally.
3. **Phase 7 — delivery and operations.** Automate pull/restart (for example
   with SSM), make rollback select a previous digest, and add logs, metrics,
   alarms plus reproducible teardown/redeploy instructions.
4. **Phase 8 — later ML lifecycle.** Add a model-approval gate, prediction and
   drift monitoring, and eventually retraining. These are valuable production
   capabilities but are not required for the first inference-only deployment.
