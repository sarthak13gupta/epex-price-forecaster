#!/usr/bin/env bash
# Read-only evidence collector. It does not open ports or change the instance.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
INSTANCE_ID="${1:-}"
if [ -z "$INSTANCE_ID" ]; then
  INSTANCE_ID="$(aws ec2 describe-instances --region "$REGION" \
    --filters Name=tag:Name,Values=epex-forecaster-phase5 'Name=instance-state-name,Values=pending,running,stopping,stopped' \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)"
fi
[ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != None ] || { echo "ERROR: no Phase-5 instance found" >&2; exit 1; }

aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].{InstanceId:InstanceId,State:State.Name,Type:InstanceType,AMI:ImageId,AZ:Placement.AvailabilityZone,PublicIp:PublicIpAddress,Profile:IamInstanceProfile.Arn,SecurityGroups:SecurityGroups[*].GroupId,IMDSv2:MetadataOptions.HttpTokens,RootDevice:RootDeviceName,LaunchTime:LaunchTime,ImageDigest:Tags[?Key==`ImageDigest`]|[0].Value}' \
  --output json

SG_IDS="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId' --output text)"
aws ec2 describe-security-groups --region "$REGION" --group-ids $SG_IDS \
  --query 'SecurityGroups[].{GroupId:GroupId,Ingress:IpPermissions,Egress:IpPermissionsEgress}' --output json

echo "--- latest bootstrap evidence (look for PHASE5_OK) ---"
aws ec2 get-console-output --region "$REGION" --instance-id "$INSTANCE_ID" --latest \
  --query Output --output text | grep -E 'PHASE5_|DEPLOYED_REFERENCE|IMAGE_IDENTITY|HEALTH_RESPONSE|PREDICTION_RESPONSE|health attempt' || true
