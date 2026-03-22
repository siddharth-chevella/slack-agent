#!/usr/bin/env bash
# Build and push a multi-architecture (linux/amd64 + linux/arm64) agent image to Docker Hub.
#
# Usage:
#   export SLACK_AGENT_IMAGE=yourdockerhub/slack-agent   # required: your namespace/repo
#   ./scripts/docker-build-push-agent.sh
#
# Tags: ${SLACK_AGENT_IMAGE}:latest and :<version from pyproject.toml>
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE="${SLACK_AGENT_IMAGE:-}"
if [[ -z "$IMAGE" ]]; then
  echo "Set SLACK_AGENT_IMAGE to your Docker Hub repository, e.g. export SLACK_AGENT_IMAGE=myuser/slack-agent" >&2
  exit 1
fi

VERSION="$(grep -E '^version\s*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ -z "$VERSION" ]]; then
  VERSION="latest"
fi

echo "Building and pushing ${IMAGE}:${VERSION} and ${IMAGE}:latest (linux/amd64,linux/arm64)..."
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f docker/Dockerfile.agent \
  -t "${IMAGE}:${VERSION}" \
  -t "${IMAGE}:latest" \
  --push \
  .

echo "Done. Others can run: SLACK_AGENT_IMAGE=${IMAGE} docker compose up -d"
echo "Or edit docker-compose.yml default image to ${IMAGE}:latest"
