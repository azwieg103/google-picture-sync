#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
sync="$PWD/.venv/bin/google-picture-sync"
[[ -x "$sync" ]] || { echo "Install the application in .venv first." >&2; exit 1; }
read -r -p "Year to migrate (YYYY): " year
[[ "$year" =~ ^[0-9]{4}$ && "$year" != 0000 ]] || { echo "Invalid year." >&2; exit 1; }
exec 9>.env/migrate.lock
flock -n 9 || { echo "Another migration is running." >&2; exit 1; }
"$sync" --year "$year"
"$sync" --verify --year "$year"
"$sync" --cleanup --year "$year" --accept-processed
"$sync" --verify --year "$year"
"$sync" --dry-run --year "$year"
echo "Migration and post-cleanup verification completed for $year."
