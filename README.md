# Google Picture Sync

Copy Google Drive year folders into Google Photos albums. Python 3.10+, Linux. Originals are never deleted.

## Install

    git clone https://github.com/azwieg103/google-picture-sync.git
    cd google-picture-sync
    python3 -m venv .venv
    source .venv/bin/activate
    pip install .
    google-picture-sync --init

## Credentials

In https://console.cloud.google.com/ create a project, enable Drive API and Photos Library API, configure OAuth consent and add your account as a test user if needed. Create an OAuth client ID of type Desktop app. Download its JSON to .env/credentials.json. The format is Google's unmodified desktop client JSON, with an installed object containing client_id, client_secret, auth_uri, token_uri and redirect_uris. Do not use your Google password or a service account. Never commit credentials or token.json.

    chmod 700 .env
    chmod 600 .env/credentials.json
    google-picture-sync --auth
    google-picture-sync --test

Authorize the same account for both services. The app writes .env/token.json and refreshes credentials as needed. External OAuth apps in Testing may require reauthorization after seven days.

## Configuration

See .env.example/config.ini. source_folder is a top-level Drive folder name; source_folder_id optionally overrides it with the ID from its Drive URL. album_prefix is prepended to each year. include_videos accepts true or false. Run from the repository directory so relative paths resolve correctly.

## Usage

    google-picture-sync --help
    google-picture-sync --scan
    google-picture-sync --dry-run
    google-picture-sync --year 2003 --limit 10
    google-picture-sync --status
    google-picture-sync --year 2003
    google-picture-sync --all
    google-picture-sync --resume
    google-picture-sync --retry-failed

The program walks four-digit year folders recursively, oldest first, and creates an album per year. Files outside year folders are skipped. Each media creation request contains one item. SQLite records successful uploads by Drive ID. Pending records indicate uncertain outcomes and are deliberately skipped to avoid duplicate uploads; manually inspect Photos before resolving them. Keep the database and credentials together; do not reuse them for another account or source. Existing Photos items cannot be comprehensively deduplicated because of API access restrictions. Do not run multiple instances concurrently. Scan and dry-run do not upload media. The test checks album read access, not upload access. Start with a small real batch. This initial version has not been live-tested.

For unattended use after authentication, run from the repository directory using the absolute virtual environment executable, with flock to prevent concurrent runs. Review status after each run. Source files remain unchanged.

## Manual

    man -M .venv/share/man google-picture-sync

API references: https://developers.google.com/photos/library/guides/upload-media and https://developers.google.com/drive/api/guides/about-sdk.
