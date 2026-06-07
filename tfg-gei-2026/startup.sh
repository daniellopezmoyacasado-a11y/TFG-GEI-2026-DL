#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker compose -f "$SCRIPT_DIR/waltid-identity/docker-compose/docker-compose.yaml" up -d
docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d --build
