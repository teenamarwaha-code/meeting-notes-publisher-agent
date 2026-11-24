#!/usr/bin/env bash
# Automated Google Cloud Run & ADK CLI Deployment Script
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-}"
REGION="${GCP_LOCATION:-us-central1}"
SERVICE_NAME="session-publisher-agent"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Usage: GCP_PROJECT_ID=<your-project> ./deploy_cloud_run.sh" >&2
  exit 1
fi

echo "==> Running unit tests & golden evaluation suite prior to deployment..."
python3 -m unittest discover -s tests -p "test_*.py"
python3 eval/run_eval.py

if command -v adk >/dev/null 2>&1; then
  echo "==> Deploying Google ADK Agent to Cloud Run via 'adk deploy cloud_run'..."
  adk deploy cloud_run \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --service_name="${SERVICE_NAME}" \
    --with_ui \
    ./session_publisher_agent
else
  echo "==> Deploying container directly via gcloud run deploy..."
  gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},USE_SECRET_MANAGER=true" \
    --no-allow-unauthenticated
fi
