#!/usr/bin/env bash
set -Eeuo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${K8S_NAMESPACE:-apps}"
SECRET_NAME="${K8S_SECRET_NAME:-fgis-secrets}"
SECRET_ENV_FILE="${SECRET_ENV_FILE:-/opt/fgiscs-history-mcp/secrets/app.env}"

if [ ! -f "$SECRET_ENV_FILE" ]; then
  echo "Secret env file not found: $SECRET_ENV_FILE (no FGIS CS secret is currently required)" >&2
  exit 1
fi

"$KUBECTL" -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  --from-env-file="$SECRET_ENV_FILE" \
  --dry-run=client -o yaml | "$KUBECTL" apply -f -

echo "Secret $SECRET_NAME synced from $SECRET_ENV_FILE"
