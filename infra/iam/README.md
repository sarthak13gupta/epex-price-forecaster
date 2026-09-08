# IAM

IAM policy documents accept only `Version`, `Id` and `Statement` at the top
level — a `_comment` key makes `aws iam put-role-policy` fail with
`MalformedPolicyDocument`. So the files here carry no comments and the reasoning
lives in this README instead. Every `Sid` is written to be self-describing.

Nothing in this directory contains an account ID or a bucket name. Placeholders
(`__BUCKET__`, `__ACCOUNT_ID__`, `__REGION__`, `__ECR_REPOSITORY__`, `__REPO__`)
are substituted at apply time from `.env` and from the current caller identity,
so the repository stays publishable.

## Two roles, two purposes

| Role | Assumed by | Gets | Created by |
|---|---|---|---|
| `epex-forecaster-ec2-role` | the EC2 instance | S3 + (optionally) ECR pull | `apply.sh` |
| `github-actions-ecr-push` | GitHub Actions, via OIDC | ECR push only | `apply-github-oidc.sh` |

They are separate because the things they trust are separate: one trusts the
EC2 service, the other trusts a federated identity provider. Merging them would
mean a CI token could read the S3 bucket.

## Files

| File | What it is |
|---|---|
| `trust-policy-ec2.json` | Lets the EC2 service assume the instance role. |
| `policy-s3-forecaster.json` | Full pipeline: read data, write processed data, forecasts and MLflow artifacts. |
| `policy-s3-inference-only.json` | Read the model artifact, write forecasts. Nothing else. |
| `trust-policy-github-oidc.json` | Lets GitHub Actions **in one repository** assume the CI role. |
| `policy-ecr-push.json` | Push images to one ECR repository. No delete actions. |
| `policy-ecr-pull.json` | Pull that image. Attach to the instance role when the instance runs from ECR. |
| `apply.sh` | Creates the EC2 role + instance profile. Idempotent. |
| `apply-github-oidc.sh` | Creates the OIDC provider, the ECR repository, and the CI role. Idempotent. |
| `verify-on-instance.sh` | Run **on the instance**: proves the role works and that no static keys shadow it. |

## The one condition that matters

In `trust-policy-github-oidc.json`:

```json
"StringLike": {
  "token.actions.githubusercontent.com:sub": "repo:__REPO__:*"
}
```

This is the entire security boundary. The `aud` check is necessary but not
sufficient — **`aud` alone would let any GitHub repository on the internet
assume the role**, because every GitHub OIDC token carries the same audience.
`sub` is what scopes it to one repository. It is the single most common way an
OIDC setup is misconfigured.

To tighten further, restrict to release tags:

```json
"token.actions.githubusercontent.com:sub": "repo:__REPO__:ref:refs/tags/v*"
```

## Why OIDC rather than an access key

A key pair stored as a GitHub secret is a long-lived credential living in a
third-party system, with manual rotation. OIDC has GitHub mint a short-lived
token per workflow run, which AWS exchanges for temporary credentials scoped by
the `sub` condition above. There is nothing to leak and nothing to rotate.

That is not abstract here: this project's `.env` holds a real access key pair
for the S3 pipeline user. Keeping it out of CI entirely is the point.

## `ecr:GetAuthorizationToken` on `"*"`

Not laziness. It is an account-level call with no repository-scoped ARN, so it
cannot be narrowed — which is why it sits in its own statement, leaving every
actual read/write action confined to one repository ARN. Splitting the
statements makes the scope of each visible at a glance.

## Order of operations

```
apply-github-oidc.sh          # OIDC provider + ECR repo + CI role
  -> set the 3 repo variables # AWS_ROLE_ARN, AWS_REGION, ECR_REPOSITORY
  -> run "Publish images"     # image lands in ECR

apply.sh                      # instance role + profile
  -> launch EC2 with the profile
  -> verify-on-instance.sh    # confirms the role resolves and no keys shadow it
```

## The failure mode to remember

`.env` copied to an EC2 instance **must not** contain `AWS_ACCESS_KEY_ID` or
`AWS_SECRET_ACCESS_KEY`. boto3's credential chain checks environment variables
first, so a stray key means the instance role is never reached — and the symptom
is an `AccessDenied` that looks like a broken policy rather than a shadowed
credential. `verify-on-instance.sh` checks for exactly this.
