#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="ethercat-mock-slave"
CONTAINER_NAME="ethercat-mock-slave"
HOST_PORT="${1:-6700}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building Docker image '${IMAGE_NAME}'..."
docker build -t "${IMAGE_NAME}" "${SCRIPT_DIR}"

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}\$"; then
  echo "Removing existing container '${CONTAINER_NAME}'..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

# Publish on loopback only: the mock accepts write_pdo from anyone who can reach it.
echo "Starting container '${CONTAINER_NAME}' on 127.0.0.1:${HOST_PORT} -> container 6700..."
docker run --rm \
  --name "${CONTAINER_NAME}" \
  -p "127.0.0.1:${HOST_PORT}:6700" \
  -e MOCK_ETHERCAT_HOST=0.0.0.0 \
  "${IMAGE_NAME}"

