#!/usr/bin/env bash
# BudgetSwarm one-command dev bring-up (BS-104).
#
# Builds the images, starts the full stack (db + backend + frontend + proxy),
# waits for the healthchecks, then applies migrations and seeds the database.
# Every seed step is idempotent, so this is safe to re-run.
#
# Usage (from anywhere in the repo):
#   ./scripts/dev-up.sh                        # bring up + offline core seed
#   ./scripts/dev-up.sh --with-accommodations  # also import Inside Airbnb (network, slow)
#
# Any extra arguments are forwarded to scripts.seed_all (e.g. --skip-migrate).
set -euo pipefail

# Resolve the repo root so the script works regardless of the current directory.
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "No .env found - creating one from .env.example." >&2
  echo "Edit .env and set a real JWT_SECRET before any non-local use." >&2
  cp .env.example .env
fi

echo "==> Building images and starting the stack (waiting for healthchecks)..."
docker compose up -d --build --wait

echo "==> Applying migrations and seeding data..."
docker compose exec -T backend python -m scripts.seed_all "$@"

echo
echo "BudgetSwarm is up:"
echo "  App:    https://localhost"
echo "  Health: https://localhost/api/health"
