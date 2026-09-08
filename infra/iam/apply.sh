#!/usr/bin/env bash
# Creates the EC2 role + instance profile for the forecaster.
#
# Run this with an ADMIN identity, not the pipeline user — that user is
# deliberately scoped to S3 only and cannot manage IAM.
#
#   POLICY=inference-only ./infra/iam/apply.sh     # strict: read model, write forecasts
#   POLICY=forecaster     ./infra/iam/apply.sh     # full pipeline (train + inference)
#
# Idempotent: re-running updates the inline policy rather than failing.
set -euo pipefail

ROLE="${ROLE:-epex-forecaster-ec2-role}"
PROFILE="${PROFILE:-epex-forecaster-ec2-profile}"
POLICY="${POLICY:-inference-only}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Bucket comes from .env so the account ID is never committed to the repo.
BUCKET="${S3_BUCKET_NAME:-}"
if [ -z "$BUCKET" ] && [ -f "$HERE/../../.env" ]; then
  BUCKET="$(grep -E '^S3_BUCKET_NAME=' "$HERE/../../.env" | cut -d= -f2-)"
fi
[ -n "$BUCKET" ] || { echo "ERROR: set S3_BUCKET_NAME or put it in .env"; exit 1; }

# ------------------------------------------------------- credential preflight
# The AWS CLI does NOT read .env — it has its own chain (env vars, then
# ~/.aws/credentials, then an instance role). And this script needs an ADMIN
# identity: the project's pipeline user is deliberately scoped to S3 only and
# gets AccessDenied on every IAM call, halfway through, which reads like a
# broken script rather than the wrong identity.
if ! CALLER="$(aws sts get-caller-identity --output json 2>&1)"; then
  cat <<'NOCREDS'
ERROR: the AWS CLI cannot find credentials.

  This script needs an ADMIN identity. Configure one as a named profile:

      aws configure --profile admin        # access key, secret, region
      export AWS_PROFILE=admin

  Do NOT export the pipeline user's keys from .env: environment variables win
  over AWS_PROFILE, so the profile would be silently ignored — and that user
  cannot manage IAM anyway.
NOCREDS
  exit 1
fi

CALLER_ARN="$(printf '%s' "$CALLER" | sed -n 's/.*"Arn": *"\([^"]*\)".*/\1/p')"
case "$CALLER_ARN" in
  *quantitative-pipeline-user*)
    cat <<'WRONGUSER'
ERROR: you are authenticated as the pipeline user, which is scoped to S3 only
       and cannot manage IAM. It will fail partway through.

  Switch to an admin identity:

      aws configure --profile admin
      unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY   # env vars beat profiles
      export AWS_PROFILE=admin
WRONGUSER
    exit 1
    ;;
esac

SRC="$HERE/policy-s3-${POLICY}.json"
[ -f "$SRC" ] || { echo "ERROR: no such policy file: $SRC"; exit 1; }

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
sed "s|__BUCKET__|${BUCKET}|g" "$SRC" > "$TMP"

echo "role            : $ROLE"
echo "instance profile: $PROFILE"
echo "policy          : $POLICY"
echo "bucket          : ...${BUCKET: -24}"
echo

# 1. the role, with a trust policy naming ec2.amazonaws.com as the principal
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "[=] role already exists"
else
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "file://$HERE/trust-policy-ec2.json" \
    --description "EPEX forecaster on EC2: scoped S3 access, no static keys" >/dev/null
  echo "[+] role created"
fi

# 2. the permissions, inline so they live and die with the role
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name "epex-s3-${POLICY}" \
  --policy-document "file://$TMP"
echo "[+] inline policy epex-s3-${POLICY} attached"

# 3. the instance profile — EC2 cannot attach a role directly, only a profile.
#    The console creates this for you; the CLI does not.
if aws iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  echo "[=] instance profile already exists"
else
  aws iam create-instance-profile --instance-profile-name "$PROFILE" >/dev/null
  echo "[+] instance profile created"
fi

if aws iam get-instance-profile --instance-profile-name "$PROFILE" \
     --query "InstanceProfile.Roles[?RoleName=='$ROLE']" --output text | grep -q .; then
  echo "[=] role already in the profile"
else
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$PROFILE" --role-name "$ROLE" >/dev/null
  echo "[+] role added to instance profile"
  echo "    (IAM is eventually consistent — allow ~10s before launching)"
fi

echo
echo "attach at launch with:  --iam-instance-profile Name=$PROFILE"
