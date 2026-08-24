#!/bin/sh
# Upgrade an existing postgres-data volume without recreating its owner or data.
set -eu

docker compose -f deploy/compose.yaml up --abort-on-container-exit migrate
