#!/usr/bin/env bash
# Creates the OIDC identity provider, the ECR repository, and the role that
# GitHub Actions assumes to push images.
#
# Run this with an ADMIN identity, not the pipeline user — that user is
# deliberately scoped to S3 only and cannot manage IAM.
#
#   REPO=sarthak13gupta/quantitave_forecasting ./infra/iam/apply-github-oidc.sh
#
# Idempotent: re-running updates the trust and inline policies rather than
# failing. Nothing here is destructive.
#
# Why OIDC and not an access key: a key stored as a GitHub secret is a
# long-lived credential sitting in a third-party system, and rotating it is
# manual. OIDC has GitHub mint a short-lived token per run that AWS trades for
# temporary credentials, scoped by the `sub` condition to this one repository.
# There is nothing to leak and nothing to rotate.
set -euo pipefail

ROLE="${ROLE:-github-actions-ecr-push}"
PROVIDER_HOST="token.actions.githubusercontent.com"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/../../.env"

# ------------------------------------------------------------------ inputs
REPO="${REPO:-}"
[ -n "$REPO" ] || { echo "ERROR: set REPO=<owner>/<repo>"; exit 1; }
case "$REPO" in */*) ;; *) echo "ERROR: REPO must be <owner>/<repo>"; exit 1;; esac

REGION="${AWS_DEFAULT_REGION:-}"
if [ -z "$REGION" ] && [ -f "$ENV_FILE" ]; then
  REGION="$(grep -E '^AWS_DEFAULT_REGION=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
REGION="${REGION:-eu-west-1}"

ECR_REPOSITORY="${ECR_REPOSITORY:-epex-forecaster}"

# The account id is read from the caller, never committed.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo "role       : $ROLE"
echo "repo       : $REPO"
echo "region     : $REGION"
echo "ecr repo   : $ECR_REPOSITORY"
echo "account    : ...${ACCOUNT_ID: -4}"
echo

subst() { sed -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
              -e "s|__REGION__|${REGION}|g" \
              -e "s|__ECR_REPOSITORY__|${ECR_REPOSITORY}|g" \
              -e "s|__REPO__|${REPO}|g" "$1"; }

TRUST="$(mktemp)"; POLICY="$(mktemp)"
trap 'rm -f "$TRUST" "$POLICY"' EXIT
subst "$HERE/trust-policy-github-oidc.json" > "$TRUST"
subst "$HERE/policy-ecr-push.json"          > "$POLICY"

# ------------------------------------------- 1. the OIDC identity provider
# One per account. The thumbprint argument is required by the API but AWS no
# longer validates it for this provider, so any well-formed value is accepted;
# GitHub's documented value is used for clarity.
PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${PROVIDER_HOST}"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "[=] OIDC provider already exists"
else
  aws iam create-open-id-connect-provider \
    --url "https://${PROVIDER_HOST}" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1" >/dev/null
  echo "[+] created OIDC provider"
fi

# ------------------------------------------------------- 2. the ECR repository
if aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] ECR repository already exists"
else
  # Scan on push is free and catches known CVEs in the base image. Tags stay
  # MUTABLE so `:serve` can move; the immutable pin is the `-<sha>` tag.
  aws ecr create-repository \
    --repository-name "$ECR_REPOSITORY" \
    --region "$REGION" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE >/dev/null
  echo "[+] created ECR repository"
fi

# ------------------------------------------------------------- 3. the role
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" \
    --policy-document "file://$TRUST"
  echo "[=] role exists; trust policy updated"
else
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "file://$TRUST" \
    --description "Lets GitHub Actions in ${REPO} push images to ECR" >/dev/null
  echo "[+] created role"
fi

aws iam put-role-policy --role-name "$ROLE" \
  --policy-name "ecr-push" \
  --policy-document "file://$POLICY"
echo "[+] attached ecr-push policy"

# ------------------------------------------------------------------ 4. output
ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)"
cat <<EOF

Done. Set these as repository VARIABLES (not secrets) at
  https://github.com/${REPO}/settings/variables/actions

  AWS_ROLE_ARN     ${ROLE_ARN}
  AWS_REGION       ${REGION}
  ECR_REPOSITORY   ${ECR_REPOSITORY}

Then run the "Publish images" workflow. Until those three are set it skips
rather than fails.
EOF
