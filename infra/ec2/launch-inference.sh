#!/usr/bin/env bash
# Launch one private-by-default EC2 host for the bundled inference image.
# No SSH key and no inbound security-group rules are created. The API binds to
# host loopback; public HTTPS belongs to the next deployment phase.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_REPOSITORY="${ECR_REPOSITORY:-epex-forecaster}"
IMAGE_DIGEST="${IMAGE_DIGEST:-}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
PROFILE="${EC2_PROFILE_NAME:-epex-forecaster-ec2-profile}"
NAME="${EC2_INSTANCE_NAME:-epex-forecaster-phase5}"
SG_NAME="${EC2_SECURITY_GROUP_NAME:-epex-forecaster-phase5-private}"

if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ERROR: set IMAGE_DIGEST to the sha256:... digest recorded by Phase 4" >&2
  exit 1
fi

existing="$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" 'Name=instance-state-name,Values=pending,running,stopping,stopped' \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [ -n "$existing" ]; then
  echo "ERROR: refusing to create a duplicate. Existing instance: $existing" >&2
  echo "Run infra/ec2/describe-inference.sh to inspect it." >&2
  exit 1
fi

VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
[ "$VPC_ID" != None ] || { echo "ERROR: no default VPC in $REGION; provide a reviewed VPC path first" >&2; exit 1; }

SUBNET_ID="$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" Name=map-public-ip-on-launch,Values=true \
  --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text)"
[ "$SUBNET_ID" != None ] || { echo "ERROR: default VPC has no public-IP-enabled subnet" >&2; exit 1; }

SG_ID="$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [ "$SG_ID" = None ]; then
  SG_ID="$(aws ec2 create-security-group --region "$REGION" \
    --vpc-id "$VPC_ID" --group-name "$SG_NAME" \
    --description 'Phase 5 EPEX inference: no inbound access' \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=$SG_NAME},{Key=Project,Value=epex-price-forecaster},{Key=Phase,Value=5}]" \
    --query GroupId --output text)"
  echo "[+] created security group $SG_ID with no ingress rules"
else
  ingress_count="$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$SG_ID" \
    --query 'length(SecurityGroups[0].IpPermissions)' --output text)"
  [ "$ingress_count" = 0 ] || { echo "ERROR: existing $SG_ID has inbound rules; refusing to use it" >&2; exit 1; }
  echo "[=] reusing no-ingress security group $SG_ID"
fi

AMI_ID="$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${ECR_REPOSITORY}"
USER_DATA="$(mktemp)"
trap 'rm -f "$USER_DATA"' EXIT
sed -e "s|__REGION__|${REGION}|g" \
    -e "s|__REGISTRY__|${REGISTRY}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__DIGEST__|${IMAGE_DIGEST}|g" \
    "$HERE/user-data.sh" >"$USER_DATA"

echo "region      : $REGION"
echo "ami         : $AMI_ID (Amazon Linux 2023 x86_64)"
echo "type        : $INSTANCE_TYPE"
echo "subnet      : $SUBNET_ID"
echo "security sg : $SG_ID (no ingress)"
echo "image       : ${IMAGE}@${IMAGE_DIGEST}"

INSTANCE_ID="$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=$PROFILE" \
  --associate-public-ip-address \
  --metadata-options HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=1 \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=12,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}' \
  --user-data "file://$USER_DATA" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=epex-price-forecaster},{Key=Phase,Value=5},{Key=ImageDigest,Value=$IMAGE_DIGEST}]" \
  --query 'Instances[0].InstanceId' --output text)"

echo "[+] launched $INSTANCE_ID"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
echo "[+] instance is running; cloud-init is installing Docker and pulling the image"
echo "Run: AWS_DEFAULT_REGION=$REGION $HERE/describe-inference.sh $INSTANCE_ID"
