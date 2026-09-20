#!/usr/bin/env bash
set -euo pipefail

readonly IMAGE="${IMAGE:-epex-forecaster:serve-bundled}"
readonly CONTAINER="${CONTAINER:-epex-phase3-verify}"
readonly MODEL_ARTIFACT="${MODEL_ARTIFACT:-artifacts/releases/french_spot_price_forecaster-v1}"
readonly MODEL_TREE_SHA256="${MODEL_TREE_SHA256:-c287e8bb61766720abdd12223075474dfebd70873b70c996296e0ba67047f132}"
readonly SKIP_BUILD="${SKIP_BUILD:-0}"

if [ -x venv/bin/python ]; then
  readonly VERIFY_PYTHON="${PYTHON:-venv/bin/python}"
else
  readonly VERIFY_PYTHON="${PYTHON:-python3}"
fi

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Refusing to replace existing container: $CONTAINER" >&2
  exit 1
fi

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ "$SKIP_BUILD" = "1" ]; then
  docker image inspect "$IMAGE" >/dev/null
else
  docker build \
    --target bundled-serve \
    --build-arg REQUIREMENTS=requirements-api.txt \
    --build-arg MODEL_ARTIFACT="$MODEL_ARTIFACT" \
    --build-arg MODEL_TREE_SHA256="$MODEL_TREE_SHA256" \
    --tag "$IMAGE" .
fi

# No published port, network, volume, AWS variable, tracking URI or writable
# root filesystem. Requests are executed from inside the isolated container.
docker run --detach \
  --name "$CONTAINER" \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  "$IMAGE" >/dev/null

for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

health="$(docker exec "$CONTAINER" curl -fsS http://localhost:8000/health)"
prediction="$(docker exec "$CONTAINER" curl -fsS \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2020-07-01","nuclear_avail":[29049,29466,30605,29438,26110]}' \
  http://localhost:8000/predict)"

"$VERIFY_PYTHON" - "$health" "$prediction" <<'PY'
import json
import sys

health = json.loads(sys.argv[1])
prediction = json.loads(sys.argv[2])
expected = [
    34.68523989365982,
    35.20491425207375,
    34.94565669127315,
    32.37078821136852,
    31.499444759126654,
]
actual = [day["predicted_price"] for day in prediction["forecast"]]
error = max(abs(a - b) for a, b in zip(actual, expected))

assert health["status"] == "ok"
assert health["model_loaded"] is True
assert health["model_uri"] == "/app/model"
assert prediction["n_days"] == 5
assert error < 1e-10, (actual, expected, error)
print(json.dumps({"health": health, "prices": actual, "max_abs_error": error}))
PY

docker exec "$CONTAINER" test ! -e /app/mlflow.db
echo '{"mlflow_database_present":false}'
docker exec "$CONTAINER" python -m src.utils.artifact_digest /app/model \
  --exclude release-manifest.json --expect "$MODEL_TREE_SHA256"

inspect="$(docker inspect "$CONTAINER")"
"$VERIFY_PYTHON" - "$inspect" <<'PY'
import json
import sys

container = json.loads(sys.argv[1])[0]
env = container["Config"]["Env"]
for forbidden in (
    "AWS_ACCESS_KEY_ID=",
    "AWS_SECRET_ACCESS_KEY=",
    "AWS_SESSION_TOKEN=",
    "MLFLOW_TRACKING_URI=",
):
    assert not any(item.startswith(forbidden) for item in env), (forbidden, env)
assert container["HostConfig"]["ReadonlyRootfs"] is True
assert container["HostConfig"]["NetworkMode"] == "none"
assert container["Mounts"] == []
assert container["Config"]["User"] == "app"
print(json.dumps({
    "user": container["Config"]["User"],
    "read_only": container["HostConfig"]["ReadonlyRootfs"],
    "network_mode": container["HostConfig"]["NetworkMode"],
    "mounts": container["Mounts"],
}))
PY

if docker top "$CONTAINER" -eo pid,args | grep -E 'train_pipeline|optuna' >/dev/null; then
  echo "Training process found in inference container" >&2
  exit 1
fi
echo '{"training_process_present":false}'

docker restart "$CONTAINER" >/dev/null
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$CONTAINER" curl -fsS http://localhost:8000/health

docker image inspect "$IMAGE" --format \
  '{"image_id":"{{.Id}}","size_bytes":{{.Size}},"user":"{{.Config.User}}","os":"{{.Os}}","architecture":"{{.Architecture}}"}'

echo "Phase 3 bundled-container verification passed."
