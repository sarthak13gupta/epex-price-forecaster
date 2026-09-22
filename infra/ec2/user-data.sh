#!/usr/bin/env bash
# Rendered by launch-inference.sh. The placeholders are not valid defaults.
set -euo pipefail
exec > >(tee /var/log/epex-phase5-bootstrap.log | logger -t epex-phase5 -s 2>/dev/console) 2>&1

REGION="__REGION__"
REGISTRY="__REGISTRY__"
IMAGE="__IMAGE__"
DIGEST="__DIGEST__"
CONTAINER_NAME="epex-forecaster"
FULL_IMAGE="${IMAGE}@${DIGEST}"

echo "PHASE5_BOOTSTRAP_START $(date -u +%FT%TZ)"
dnf install -y docker
systemctl enable --now docker

# The password is short-lived and comes from the instance role. It is never
# written to this file, an environment file, an AMI, or a GitHub secret.
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker pull "$FULL_IMAGE"

docker run --detach \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --publish 127.0.0.1:8000:8000 \
  --health-cmd 'python -m src.utils.http_probe http://localhost:8000/health' \
  --health-interval 10s \
  --health-timeout 5s \
  --health-retries 6 \
  --health-start-period 30s \
  "$FULL_IMAGE"

for attempt in $(seq 1 36); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_NAME")"
  echo "health attempt ${attempt}: ${health}"
  [ "$health" = healthy ] && break
  [ "$health" = unhealthy ] && {
    docker logs "$CONTAINER_NAME"
    exit 1
  }
  sleep 5
done

[ "$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_NAME")" = healthy ]
echo "DEPLOYED_REFERENCE $(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME")"
echo "IMAGE_IDENTITY $(docker inspect --format '{{.Image}}' "$CONTAINER_NAME")"
echo "HEALTH_RESPONSE $(docker exec "$CONTAINER_NAME" python -m src.utils.http_probe http://localhost:8000/health)"
echo "PREDICTION_RESPONSE $(docker exec "$CONTAINER_NAME" python -m src.utils.http_probe \
  http://localhost:8000/predict \
  --data '{"start_date":"2020-07-01","nuclear_avail":[29049,29466,30605]}')"
echo "PHASE5_OK $(date -u +%FT%TZ)"
