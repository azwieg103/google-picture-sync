# Google Picture Sync

A Linux/Python 3.10+ utility for migrating Google Drive pictures and videos into Google Photos albums, with resumable local state and optional recoverable source cleanup.

**Maintenance handoff — 2026-11-27.** The bulk migration has been exercised successfully. Remaining exceptions are documented below; this is not a certification that every original has been preserved. The repository is ready for documentation handoff, but retain the working installation and migration database until exceptions are resolved.

## Design and limitations

- Each direct four-digit year folder under Pictures maps to one album. Nested folders are flattened recursively; filenames are preserved on upload. Named direct children can be selected explicitly with `--folder`. Loose files at the root are not included.
- Google Photos Library API access is limited to app-created media and albums. A browser-visible item may not be API-visible. This is not a whole-library organizer or a lossless backup.
- Uploads do not delete originals. Explicit cleanup (including the wrapper) moves eligible originals to Drive Trash, never permanently deletes them. Empty nested folders may also be trashed; top-level source folders remain.
- Google may change metadata, image pixels, video codecs, audio, resolution and timing. Relaxed acceptance policies trade fidelity assurance for migration progress.

## Install and update

Install Python 3.10+, venv support, FFmpeg/ffprobe (with required codecs), SQLite CLI for diagnostics, Bash and flock. On Enterprise Linux, FFmpeg may require an additional trusted package repository.

```bash
git clone https://github.com/azwieg103/google-picture-sync.git
cd google-picture-sync
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/google-picture-sync --init
```

For an existing installation, stop active migrations first:

```bash
git pull --ff-only origin main &&
.venv/bin/python -m pip install --force-reinstall --no-deps --no-cache-dir .
.venv/bin/google-picture-sync --help
```

Do not rerun initialization over an existing configuration. Package version 0.1.0 is not a reliable indicator of the installed Git revision; use git log and CLI help.

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



## CLI reference

Run from the repository directory; examples below assume `source .venv/bin/activate`.

| Option | Purpose |
|---|---|
| --init / --auth / --test | Initialize, authorize, test connectivity |
| --scan / --dry-run | Inventory without uploading or trashing |
| --year YEAR | Select one year; alone uploads missing media |
| --folder NAME | Select one non-year direct child; alone uploads missing media |
| --all / --resume | Run migration across yearly folders |
| --retry-failed | Retry failed records; pending records are skipped |
| --limit N | Limit new uploads; zero means unlimited |
| --status | SQLite status and cleanup counts |
| --verify | Check recorded destination IDs and album membership |
| --cleanup | Verify and trash eligible source files; requires year or folder |
| --videos-only | Restrict selected media to videos |
| --create-albums YEAR[-YEAR] | Create/reuse app-visible albums; ascending range up to 101 years |
| --config PATH | Override configuration path |
| --help / --version | CLI help/version |

Action flags are mutually exclusive: use `--dry-run` alone for all years, not `--all --dry-run`. Year and folder selectors cannot be combined. Named folders are not automatically included in all-year runs.

## Normal workflow

```bash
google-picture-sync --year 2003 --dry-run
google-picture-sync --year 2003
google-picture-sync --year 2003 --verify
google-picture-sync --year 2003 --cleanup
google-picture-sync --year 2003 --dry-run
```

Cleanup is partial, not all-or-nothing: passing candidates can be trashed even when other files fail. The source version/checksum is checked again before trashing. Do not edit source or destination during a run. A final verification can include historical records whose originals are already gone; a log saying verified does not always mean content was compared during that run.

### Verification policies

| Policy | Acceptance |
|---|---|
| Default image | Exact MD5 or identical orientation-normalized decoded pixels for supported single-frame images |
| --accept-processed | Also accepts processed single-frame images with matching dimensions and recorded destination identity; pixels may differ |
| Default video | Exact MD5, encoded stream hash/signature or exact decoded frames/audio/timing |
| --accept-processed-video | Conservative processed video checks: duration within 0.10 s, matching geometry, frame rate within 0.01 fps, matching frame counts, minimum SSIM 0.95 and mean 0.98, matching decoded audio |
| --accept-unverified-video | Migration policy: duration within 0.5 s, matching resolution and video/audio stream counts, full destination decode; no content similarity guarantee |
| --trust-uploaded-video | Recorded successful upload and expected album membership only; skips READY, download, resolution, audio and content checks |

The last three options require cleanup; trust-uploaded-video also requires videos-only. The migration policy takes precedence over processed-video comparison for non-identical videos when selected. Use the strictest policy suitable for your needs. Trusted upload is not integrity verification and may accept a video that cannot play. Independently inspect destination media and retain a backup or Drive Trash.

```bash
google-picture-sync --year 2016 --cleanup --accept-processed --accept-processed-video
google-picture-sync --year 2016 --cleanup --videos-only --accept-unverified-video
# Only after accepting the weaker assurance:
google-picture-sync --year 2016 --cleanup --videos-only --trust-uploaded-video
```

### Video-only and named-folder migration

```bash
google-picture-sync --year 2016 --videos-only
google-picture-sync --year 2016 --cleanup --videos-only --trust-uploaded-video

google-picture-sync --folder Dawson --dry-run
google-picture-sync --folder Dawson
google-picture-sync --folder Dawson --cleanup --accept-processed --accept-unverified-video
google-picture-sync --folder Dawson --cleanup --videos-only --trust-uploaded-video
```

HEVC MP4 files may have Drive MIME application/mp4; recognized video extensions are used as a fallback when video inclusion is enabled. Resumable uploads use chunks and bounded session retries, but session state is not persisted across processes and the original payload is buffered in memory. FFmpeg uses -nostdin. Destination download retries refresh URLs. Google transcoding can defeat strict and processed verification even when playback works in the browser.

### Batch and annual operation

```bash
bash migrate.sh 2021-2005
google-picture-sync --create-albums 2022-2026
bash migrate.sh 2026
```

The wrapper accepts year selections/ranges, locks against other wrapper runs, uploads, cleans with --accept-processed, verifies and scans. It does not enable relaxed video policies automatically. Direct CLI runs do not share the wrapper lock: do not overlap them. Album creation reuses recorded or uniquely matching app-visible albums and rejects ambiguous titles.

Example cron (adjust path and disable after migration):
```cron
0 14 * * * /bin/bash /path/to/google-picture-sync/migrate.sh 2021-2005 >> /path/to/google-picture-sync/.env/cron.log 2>&1
```

For detached execution use nohup/setsid with stdin redirected and logs captured. Check for an existing process before starting another. Logs are under .env/logs/ and the configured cron log.

## Quotas and state

Requests to Photos are paced at two seconds. HTTP 429 triggers exponential backoff from 60 seconds up to one hour, honoring Retry-After. The old hard local daily quota cutoff was removed because it caused unnecessary overnight waits; legacy quota_usage records and budget-related code may remain. quota_responses tracks actual responses, not total Google project usage. Check Cloud Console for authoritative quotas. New uploads batch up to 50; token collection can flush after ten minutes. Long throttling can expire upload tokens.

Preserve .env/config.ini, .env/token.json, env/.env (or desktop credentials JSON), .env/migration.sqlite3 and logs securely. Never commit secrets or personal migration state. SQLite tables include media (Drive ID, year/folder, filename, status, remote ID, error), albums, cleanup_history, verified_source, completed_year and quota tables. Integrity checkpoints depend on source version, remote ID and policy. Trusted video acceptance is counted separately from verified. Completed empty years can be skipped; new media is still scanned. Back up the database while no migration is running before any manual repair.

```bash
sqlite3 .env/migration.sqlite3 ".backup migration-backup.sqlite3"
sqlite3 -header -column .env/migration.sqlite3 \
  "SELECT year,name,status,error FROM media WHERE status IN ('failed','pending') ORDER BY year,name;"
sqlite3 -header -column .env/migration.sqlite3 \
  "SELECT year,count(*) AS trashed FROM cleanup_history GROUP BY year ORDER BY year;"
```

## Troubleshooting and lessons learned

| Symptom | Response |
|---|---|
| OAuth 403 access_denied | Check project, test user, enabled APIs and scopes; reauthorize |
| HTTP 429 | Let backoff work; check Cloud Console rather than relying on local counts |
| Video not ready | Wait and retry; browser playback does not prove API readiness |
| Resolution/audio/timing mismatch | Google may transcode; inspect manually or select an explicitly weaker policy |
| Destination missing or outside expected album | Do not trash; check browser and app-visible album, saved ID and upload response |
| No confirmed upload / pending | Reconcile manually; do not blindly reset pending records or upload duplicates |
| Empty video | Check actual Drive file size and download before deciding disposition |
| Unsupported MP4 box size | Recovery marker code cannot handle some containers; retain original |
| Failed upload | Retry with --retry-failed; normal selected migration also handles missing destination records |
| Suspended background process | Check jobs/processes; use detached execution with redirected stdin and output |

A successful batchCreate response does not guarantee immediate album visibility. Commit 74fca1a stops immediate repeat uploads following successful recovery creation; later runs may still attempt recovery if the destination remains missing. This change does not resolve all missing destinations. Duplicate avoidance is limited by API visibility. Pending reconciliation and comprehensive duplicate detection are not implemented. Do not treat filename equality alone as proof of content identity.

## Final inventory and archive checklist

```bash
google-picture-sync --dry-run
google-picture-sync --folder Dawson --dry-run
google-picture-sync --status
```

The November 2026 live migration exercised exact/pixel image acceptance, partial cleanup, named folders, video-only filtering, trusted album membership cleanup, and retrying failed video uploads. For example, 24 videos in 2005 and seven retried videos in 2012/2016 were moved to Trash using trusted membership, not content verification. Dawson's nine pictures passed identical decoded pixel comparison; two videos initially failed resolution/readiness checks.

Last known exceptions (not a fresh inventory): six videos in 2013–2015 had missing destination IDs or album membership, two 2014 videos returned Empty video, three JPEG uploads in 2006/2007 repeatedly failed, and five JPEGs in 2005/2019/2020 were pending. Other uploaded JPEGs remained subject to cleanup. The final supplied log still reports five unverified records for 2015 and one for 2016, with zero files trashed in those summaries. Historical verification counts are not counts of active source files. No claim of complete migration or lossless preservation is made.

Before archiving:
1. Review a fresh dry-run and resolve or explicitly retain exceptions.
2. Review Google Photos playback, quality and album organization; keep original backups.
3. Disable migration cron and stop active jobs.
4. Securely preserve local database, configuration, logs and credentials as needed; revoke OAuth access only when no further recovery is needed.
5. Archive the GitHub repository if no more code changes are needed. Do not empty Drive Trash simply because the repository is archived; Trash may expire automatically.

## References

- `man -M .venv/share/man google-picture-sync` (the bundled manual may lag the final CLI; prefer --help and this README).
- [Google Photos upload guide](https://developers.google.com/photos/library/guides/upload-media)
- [Google Drive API](https://developers.google.com/drive/api/guides/about-sdk)

This utility is not affiliated with Google. Use at your own risk; maintain independent backups.
