#!/usr/bin/env bash
set -Eeuo pipefail

: "${IMAGE:?Set IMAGE to the full image reference before bootstrapping}"

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${K8S_NAMESPACE:-apps}"
APP_ENV="${APP_ENV:-production}"
case "$APP_ENV" in
  preproduction) LOG_LEVEL="DEBUG" ;;
  production) LOG_LEVEL="INFO" ;;
  *) echo "APP_ENV must be preproduction or production" >&2; exit 1 ;;
esac

# Namespace and persistent storage are provisioned separately by a cluster
# administrator. The deployer service account only manages app runtime resources.
"$KUBECTL" -n "$NAMESPACE" apply -f k8s/service.yaml

python3 - "$APP_ENV" "$LOG_LEVEL" k8s/configmap.yaml <<'PY' | "$KUBECTL" -n "$NAMESPACE" apply -f -
import json
import sys
from pathlib import Path

app_env, log_level, manifest = sys.argv[1:]
text = Path(manifest).read_text(encoding="utf-8")
text = text.replace("APP_ENV: production", f"APP_ENV: {json.dumps(app_env)}")
text = text.replace("LOG_LEVEL: INFO", f"LOG_LEVEL: {json.dumps(log_level)}")
sys.stdout.write(text)
PY

python3 - "$IMAGE" k8s/deployment.yaml <<'PY' | "$KUBECTL" -n "$NAMESPACE" apply -f -
import sys
from pathlib import Path

image, manifest = sys.argv[1:]
text = Path(manifest).read_text(encoding="utf-8")
text = text.replace("ghcr.io/gogaapps/fgiscs-history-mcp:bootstrap", image)
sys.stdout.write(text)
PY

"$KUBECTL" -n "$NAMESPACE" rollout status deployment/fgiscs-history-mcp --timeout=600s
