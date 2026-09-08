#!/usr/bin/env bash
# Run this ON the EC2 instance after attaching the role. Proves the credential
# chain works with NO static keys present.
set -uo pipefail

echo "=== 1. is there an instance role? (IMDSv2) ==="
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60") || true
curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
     http://169.254.169.254/latest/meta-data/iam/security-credentials/ 2>/dev/null \
  | sed 's/^/  role: /' || echo "  NO ROLE ATTACHED"

echo
echo "=== 2. who does the SDK think we are? ==="
aws sts get-caller-identity --query Arn --output text 2>&1 | sed 's/^/  /'
echo "  expect: arn:aws:sts::<acct>:assumed-role/epex-forecaster-ec2-role/i-..."
echo "  NOT:    arn:aws:iam::<acct>:user/..."

echo
echo "=== 3. confirm no static keys are present ==="
grep -qE '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)=.+' .env 2>/dev/null \
  && echo "  WARNING: .env still contains AWS keys — remove them" \
  || echo "  no AWS keys in .env"
env | grep -qE '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)=' \
  && echo "  WARNING: AWS keys in the environment" \
  || echo "  no AWS keys in the environment"

echo
echo "=== 4. can the app actually read S3 through the role? ==="
ENV=production python -m src.data.data_loader 2>&1 | grep -E '^\[PROD' | sed 's/^/  /'
