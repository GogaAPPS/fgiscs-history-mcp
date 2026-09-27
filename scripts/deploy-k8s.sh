#!/usr/bin/env bash
set -Eeuo pipefail

: "${IMAGE:?Usage: IMAGE=ghcr.io/gogaapps/fgiscs-history-mcp:tag scripts/deploy-k8s.sh}"

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${K8S_NAMESPACE:-apps}"
DEPLOYMENT="${K8S_DEPLOYMENT:-fgiscs-history-mcp}"
CONTAINER="${K8S_CONTAINER:-fgiscs-history-mcp}"

"$KUBECTL" -n "$NAMESPACE" set image "deployment/$DEPLOYMENT" "$CONTAINER=$IMAGE"
"$KUBECTL" -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=600s
