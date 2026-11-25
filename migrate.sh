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
# Fail closed until the application implements integrity and cleanup safeguards.
echo "Cleanup blocked: source integrity verification and safe trash support are not implemented." >&2
exit 1
