#!/usr/bin/env bash
# Explicit teardown: requires the exact instance ID and a confirmation token.
# The encrypted root volume is DeleteOnTermination=true. The IAM role, instance
# profile and no-ingress security group remain reusable and non-billable.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
INSTANCE_ID="${1:-}"
CONFIRM="${2:-}"
[ -n "$INSTANCE_ID" ] || { echo "usage: $0 i-... terminate" >&2; exit 1; }
[ "$CONFIRM" = terminate ] || { echo "ERROR: pass the literal word 'terminate' as the second argument" >&2; exit 1; }

NAME="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].Tags[?Key==`Name`]|[0].Value' --output text)"
[ "$NAME" = epex-forecaster-phase5 ] || {
  echo "ERROR: $INSTANCE_ID is tagged Name=$NAME, not epex-forecaster-phase5" >&2
  exit 1
}

aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'TerminatingInstances[0].{InstanceId:InstanceId,Previous:PreviousState.Name,Current:CurrentState.Name}' \
  --output json
echo "Termination requested. The root EBS volume is configured for deletion with the instance."
