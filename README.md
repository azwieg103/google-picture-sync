# Google Picture Sync

Copy Google Drive year folders into Google Photos albums. Python 3.10+, Linux. Supports verified migration with recoverable deletion of originals from Drive (move to Trash).

## Install

    git clone https://github.com/azwieg103/google-picture-sync.git
    cd google-picture-sync
    python3 -m venv .venv
    source .venv/bin/activate
    pip install .
    google-picture-sync --init

## Google Cloud API and OAuth setup

Use the same Google account for Drive and Photos. You do not need to provide your Google password or create a service account.

### 1. Create or select a Google Cloud project

Open [Google Cloud Console](https://console.cloud.google.com/), select the project menu at the top, and create or select a project (for example, Photo Sync).

### 2. Enable both APIs

With that project selected, open each page and click **Enable**:

- [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
- [Google Photos Library API](https://console.cloud.google.com/apis/library/photoslibrary.googleapis.com)

### 3. Configure OAuth consent

Open [Google Auth Platform](https://console.cloud.google.com/auth/overview). Click **Get started** if prompted. Enter the application name, user support email and developer contact email. Select **External** for a personal Google account.

Open [Audience](https://console.cloud.google.com/auth/audience). While the app is in Testing mode, under **Test users**, click **Add users**, enter the Google account you intend to migrate, and save.

If Google reports **Error 403: access_denied** or says the application has not completed verification, confirm that your account is on this test-user list and that you selected the correct project.

The application requests these OAuth scopes (see [Data Access](https://console.cloud.google.com/auth/scopes)):

- `https://www.googleapis.com/auth/drive` (required for cleanup; read-only tokens support copying only)
- `https://www.googleapis.com/auth/photoslibrary.appendonly`
- `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata`

Drive write access is requested during authentication so verified originals can be moved to Trash. Photos read access is limited to app-created content.

### 4. Create desktop OAuth credentials

Open [Clients](https://console.cloud.google.com/auth/clients), click **Create client**, select **Desktop app**, enter a name, and click **Create**. Copy the client ID and client secret.

From the repository directory:

    mkdir -p env
    chmod 700 env
    vi env/.env

Enter the following, replacing the placeholders with your own values:

    CLIENT_ID=your-client-id.apps.googleusercontent.com
    CLIENT_SECRET=your-client-secret

Protect the credentials and verify Git ignores them:

    chmod 600 env/.env
    git check-ignore -v env/.env
    git ls-files env/

The last command should return no tracked files. Never commit credentials or OAuth tokens. Alternatively, if env/.env is absent, the application accepts Google's original desktop client JSON at .env/credentials.json.

### 5. Authenticate and test

From the repository directory with the virtual environment active:

    google-picture-sync --auth
    google-picture-sync --test

Sign in through the browser, approve the requested permissions, and wait for the authentication completion message. For the default source folder, a successful connectivity test prints:

    Drive: Pictures
    Photos album read successful; no upload tested

The application saves its OAuth token in .env/token.json and refreshes credentials as needed. External OAuth apps in Testing may require reauthorization after seven days.

### 6. Preview and test a migration

    google-picture-sync --dry-run --year 2003
    google-picture-sync --year 2003 --limit 1
    google-picture-sync --status

Confirm the uploaded item in [Google Photos](https://photos.google.com/). The connectivity test alone does not prove that uploads work. No source files are deleted.

## Configuration

See .env.example/config.ini. source_folder is a top-level Drive folder name; source_folder_id optionally overrides it with the ID from its Drive URL. album_prefix is prepended to each year. include_videos accepts true or false. Run from the repository directory so relative paths resolve correctly.

## Recommended yearly migration

After installation and authentication, run:

```bash
bash migrate.sh
```

Enter a four-digit year. The wrapper locks against concurrent wrapper runs, scans and uploads missing media, verifies the destination, performs integrity-gated cleanup, verifies Photos again, and displays a final Drive scan. Empty nested folders are moved to Trash; the main year folder and folders containing other files are retained. The wrapper enables `--accept-processed`, which permits a decoded single-frame image with matching dimensions and a confirmed destination ID even if pixels differ. This can lose original quality or metadata. Use manual cleanup without that option for stricter verification.

For existing installations:

```bash
git pull --ff-only origin main
python -m pip install --upgrade .
```

Pillow is installed automatically. Reauthorize with `google-picture-sync --auth` if the existing token lacks Drive write access. Keep `.env/migration.sqlite3` and credentials intact.

## Basic Drive to Photos migration

Google Drive → Google Photos albums

Each top-level four-digit year folder under Pictures maps to one album of the same year. Photos and videos in nested subfolders are recursively consolidated into that album; Google Photos does not support nested albums.

```bash
google-picture-sync --dry-run --year 2003
google-picture-sync --year 2003
google-picture-sync --verify --year 2003
google-picture-sync --cleanup --year 2003
google-picture-sync --verify --year 2003
google-picture-sync --dry-run --year 2003
```

1. Preview the source files without uploading.
2. Copy the year into its corresponding Google Photos album.
3. Verify the destination media and album membership.
4. Compare source and destination bytes by MD5; for single-frame images, fall back to orientation-normalized decoded pixel comparison. Metadata may differ. All eligible source files must pass before trashing begins. Videos require exact bytes.
5. Trash verified originals and empty nested folders, then verify destination records again and rescan Drive. Trash is recoverable until emptied or expired; never permanently delete through this utility.

**Current working status (2026-11-25):** A live 2003 migration completed with 198 destination records verified and zero errors in final verification; the operator confirmed originals appeared removed from Drive. Exact-checksum and identical-pixel fallback have been exercised. Empty-folder cleanup and the latest complete wrapper are implemented but not yet confirmed in a full live run. Cleanup is not transactional: interruption may leave a partially cleaned year. Do not modify source or destination during migration. Final verification checks existence and album membership, not original bytes.

## Usage

    google-picture-sync --help
    google-picture-sync --scan
    google-picture-sync --dry-run
    google-picture-sync --dry-run --year 2003
    google-picture-sync --year 2003 --limit 10
    google-picture-sync --status
    google-picture-sync --year 2003
    google-picture-sync --all
    google-picture-sync --resume
    google-picture-sync --retry-failed
    google-picture-sync --verify --year 2003
    google-picture-sync --cleanup --year 2003
    google-picture-sync --cleanup --year 2003 --accept-processed

The program walks four-digit year folders recursively, oldest first, and creates an album per year. Files outside year folders are skipped. Each media creation request contains one item. SQLite records successful uploads by Drive ID. Pending records indicate uncertain outcomes and are deliberately skipped to avoid duplicate uploads; manually inspect Photos before resolving them. Keep the database and credentials together; do not reuse them for another account or source. Existing Photos items cannot be comprehensively deduplicated because of API access restrictions. Do not run multiple instances concurrently. Scan and dry-run do not upload media. The test checks album read access, not upload access. Start with a small real batch. OAuth, live uploads, destination verification and media trash cleanup have been exercised. Stale pending records or deleted destination items may require manual reconciliation; back up SQLite before editing records.

For unattended use after authentication, run from the repository directory using the absolute virtual environment executable, with flock to prevent concurrent runs. Review status after each run. Source files remain unchanged unless cleanup is explicitly invoked, including through migrate.sh.

## Manual

    man -M .venv/share/man google-picture-sync

API references: https://developers.google.com/photos/library/guides/upload-media and https://developers.google.com/drive/api/guides/about-sdk.

## Daily Google Photos quota management

The migration defaults to 9,000 API requests and 67,500 media requests per Pacific calendar day. Counters persist in `.env/migration.sqlite3` across years and restarts. At the local budget, the process waits until 00:05 Pacific the next day. HTTP 429 responses trigger unlimited exponential backoff (60 seconds to one hour, honoring Retry-After). Requests are paced at two-second intervals within each process. Configure `daily_api_budget` and `daily_media_budget` under `[photos]` in `.env/config.ini`. Local counters do not include earlier versions or other clients; the assumed reset time is not guaranteed by Google. Run `bash migrate.sh 2021-2005` to migrate separate yearly albums sequentially. Logs are in `.env/logs/`. Existing verification and cleanup safeguards remain in place.

## Efficient unattended migration

New uploads are created in batches of up to 50, with early flush near the local API budget or after ten minutes of token collection. Existing album membership is fetched in pages of 100. Missing items use individual recovery checks. Integrity checkpoints are keyed by source version, destination ID and processed-image policy; album membership is rechecked before cleanup. Videos not READY are retained for another run. Successfully cleaned years with empty source inventories are skipped on subsequent runs; newly added media is still scanned. Drive trash remains individual to minimize the interval between source version check and trash. Ambiguous batch submissions remain pending for manual reconciliation and block cleanup. External quota usage or a prolonged 429 can still cause upload tokens to expire. Run `bash migrate.sh 2021-2005` from cron; inspect `.env/logs/` for failures. These changes require live validation before relying on unattended deletion.


## Annual migration

After the current migration exits, install the latest version and create albums in advance:

```bash
.venv/bin/google-picture-sync --create-albums 2022-2026
```

A single year is also supported. Existing app-visible albums are reused and recorded in SQLite; ambiguous titles are rejected. Google Photos only exposes albums created by this application under its current API scopes. Albums created manually may not be discoverable. This command does not upload or trash media. Do not run concurrently with a migration.

For an annual migration, place photos and videos in the corresponding year folder beneath the configured Drive Pictures folder, then run:

```bash
bash migrate.sh 2026
```

The wrapper uploads, verifies and moves eligible originals to Drive Trash. Keep Trash until results are reviewed. For upload only, use `--year 2026` directly.
