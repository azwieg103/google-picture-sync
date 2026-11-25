# Google Picture Sync

Copy Google Drive year folders into Google Photos albums. Python 3.10+, Linux. Originals are never deleted.

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

- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/photoslibrary.appendonly`
- `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata`

Drive access is read-only. Photos read access is limited to app-created content.

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

The program walks four-digit year folders recursively, oldest first, and creates an album per year. Files outside year folders are skipped. Each media creation request contains one item. SQLite records successful uploads by Drive ID. Pending records indicate uncertain outcomes and are deliberately skipped to avoid duplicate uploads; manually inspect Photos before resolving them. Keep the database and credentials together; do not reuse them for another account or source. Existing Photos items cannot be comprehensively deduplicated because of API access restrictions. Do not run multiple instances concurrently. Scan and dry-run do not upload media. The test checks album read access, not upload access. Start with a small real batch. OAuth and API read connectivity have been tested; live uploads still require validation.

For unattended use after authentication, run from the repository directory using the absolute virtual environment executable, with flock to prevent concurrent runs. Review status after each run. Source files remain unchanged.

## Manual

    man -M .venv/share/man google-picture-sync

API references: https://developers.google.com/photos/library/guides/upload-media and https://developers.google.com/drive/api/guides/about-sdk.
