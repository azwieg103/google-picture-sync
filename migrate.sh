#!/usr/bin/env bash
set -euo pipefail
export PYTHONUNBUFFERED=1
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
sync="$PWD/.venv/bin/google-picture-sync"
[[ -x "$sync" ]] || { echo "Install the application in .venv first." >&2; exit 1; }
if (( $# )); then
    selection="$*"
else
    read -r -p "Years to migrate (e.g. 2020,2019 or 2005-2020): " selection
fi
selection="${selection//,/ }"
read -r -a tokens <<< "$selection"
declare -a years=() passed=() failed=()
declare -A seen=()
for token in "${tokens[@]}"; do
    if [[ "$token" =~ ^([1-9][0-9]{3})(-([1-9][0-9]{3}))?$ ]]; then
        first=${BASH_REMATCH[1]}
        last=${BASH_REMATCH[3]:-$first}
        step=1
        if (( first > last )); then step=-1; fi
        for ((year=first; ; year+=step)); do
            if [[ ! ${seen[$year]+yes} ]]; then
                years+=("$year")
                seen[$year]=1
            fi
            if (( year == last )); then break; fi
        done
    else
        echo "Invalid year or range: $token" >&2
        exit 1
    fi
done
(( ${#years[@]} )) || { echo "No years specified." >&2; exit 1; }
exec 9>.env/migrate.lock
flock -n 9 || { echo "Another migration is running." >&2; exit 1; }
umask 077
mkdir -p .env/logs
run_id="$(date +%Y%m%d-%H%M%S)-$$"
for year in "${years[@]}"; do
    log=".env/logs/migrate-${year}-${run_id}.log"
    if (
        echo "=== Migrating $year at $(date -Is) ==="
        "$sync" --year "$year" &&
        "$sync" --cleanup --year "$year" --accept-processed &&
        "$sync" --verify --year "$year" &&
        "$sync" --dry-run --year "$year"
    ) 2>&1 | tee "$log"; then
        passed+=("$year")
        echo "Migration and post-cleanup verification completed for $year." | tee -a "$log"
    else
        failed+=("$year")
        echo "Migration incomplete for $year; inspect before retrying. Continuing." | tee -a "$log" >&2
    fi
    echo "Log: $log"
done
echo "=== Migration summary ==="
echo "Completed: ${passed[*]:-none}"
echo "Needs attention: ${failed[*]:-none}"
if (( ${#failed[@]} )); then exit 1; fi
