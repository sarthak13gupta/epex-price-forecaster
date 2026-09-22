#!/usr/bin/env bash
# Create the EC2 service role used by the bundled inference host.
#
# The role can authenticate to ECR and pull from exactly one repository. It
# cannot push images, read S3, call MLflow, manage EC2, or assume other roles.
# Re-running this script updates the trust/pull policy and reuses the profile.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
ECR_REPOSITORY="${ECR_REPOSITORY:-epex-forecaster}"
ROLE="${EC2_ROLE_NAME:-epex-forecaster-ec2-role}"
PROFILE="${EC2_PROFILE_NAME:-epex-forecaster-ec2-profile}"

CALLER="$(aws sts get-caller-identity --output json)" || {
  echo "ERROR: AWS credentials are unavailable. Set AWS_PROFILE to a bootstrap identity." >&2
  exit 1
}
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

subst() {
  sed -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
      -e "s|__REGION__|${REGION}|g" \
      -e "s|__ECR_REPOSITORY__|${ECR_REPOSITORY}|g" "$1"
}

TRUST="$(mktemp)"
POLICY="$(mktemp)"
trap 'rm -f "$TRUST" "$POLICY"' EXIT
subst "$HERE/trust-policy-ec2.json" >"$TRUST"
subst "$HERE/policy-ecr-pull.json" >"$POLICY"

echo "caller      : $(printf '%s' "$CALLER" | sed -n 's/.*\"Arn\": *\"\([^\"]*\)\".*/\1/p')"
echo "region      : $REGION"
echo "repository  : $ECR_REPOSITORY"
echo "role        : $ROLE"
echo "profile     : $PROFILE"

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy \
    --role-name "$ROLE" \
    --policy-document "file://$TRUST"
  echo "[=] role exists; EC2 trust policy updated"
else
  aws iam create-role \
    --role-name "$ROLE" \
    --description "ECR-pull-only role for the EPEX bundled inference host" \
    --assume-role-policy-document "file://$TRUST" >/dev/null
  echo "[+] created role"
fi

aws iam put-role-policy \
  --role-name "$ROLE" \
  --policy-name ecr-pull \
  --policy-document "file://$POLICY"
echo "[+] applied repository-scoped ecr-pull policy"

if aws iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  echo "[=] instance profile already exists"
else
  aws iam create-instance-profile --instance-profile-name "$PROFILE" >/dev/null
  echo "[+] created instance profile"
fi

CURRENT_ROLE="$(aws iam get-instance-profile \
  --instance-profile-name "$PROFILE" \
  --query 'InstanceProfile.Roles[0].RoleName' --output text)"
if [ "$CURRENT_ROLE" = "None" ]; then
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$PROFILE" \
    --role-name "$ROLE"
  echo "[+] attached role to instance profile"
elif [ "$CURRENT_ROLE" != "$ROLE" ]; then
  echo "ERROR: profile $PROFILE already contains unexpected role $CURRENT_ROLE" >&2
  exit 1
else
  echo "[=] expected role is already attached to the profile"
fi

echo
echo "Done: arn:aws:iam::${ACCOUNT_ID}:instance-profile/${PROFILE}"
echo "IAM can take a few seconds to propagate before EC2 accepts the profile."
