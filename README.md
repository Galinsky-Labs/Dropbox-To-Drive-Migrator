# Dropbox → Google Drive Migrator

`dbx2gdrive` copies **everything** in a Dropbox account into Google Drive. It keeps the folder structure, so empty folders, shared folders you've added to your Dropbox, and team folders all come across.

It's built for large accounts:

- **Resumable.** Progress is recorded per file in a local SQLite database. Stop it, lose your connection or reboot, then run the same command again and it picks up where it left off.
- **Incremental.** When you re-run, it re-lists Dropbox, copies new files and overwrites the Drive copy of any file that changed. Unchanged files aren't touched.
- **Verified.** Every download is checked against Dropbox's `content_hash`. Every upload is checked against the MD5 that Google Drive reports. A mismatch is retried and never recorded as done.
- **Parallel.** It runs several transfers at once (`--workers`), with resumable chunked uploads for large files. Rate limits and transient errors are retried with backoff.
- **Faithful.** Files keep their original modification time. Dropbox Paper docs and other cloud-only files are exported (for example to `.md`/`.docx`) instead of being silently dropped.

## Install

Requires Python 3.9+.

```bash
git clone https://github.com/Galinsky-Labs/Dropbox-To-Drive-Migrator.git
cd Dropbox-To-Drive-Migrator
python3 -m venv .venv && source .venv/bin/activate
pip install .
```

## One-time setup

Neither Dropbox nor Google lets a third-party tool read or write your files without an app registration. Both registrations are free and take about 5 minutes.

### 1. Dropbox app (read-only access)

1. Go to <https://www.dropbox.com/developers/apps> → **Create app**.
2. Choose **Scoped access** → **Full Dropbox** and give it any name.
3. On the **Permissions** tab, tick `account_info.read`, `files.metadata.read` and `files.content.read`, then click **Submit**.
4. On the **Settings** tab, copy the **App key**.

```bash
dbx2gdrive auth dropbox --app-key YOUR_APP_KEY
```

Open the URL it prints, click **Allow** and paste the code back in. This uses PKCE, so you don't need the app secret.

### 2. Google OAuth client

1. In <https://console.cloud.google.com/>, create (or pick) a project.
2. Go to **APIs & Services → Library**, find **Google Drive API** and click **Enable**.
3. Go to **APIs & Services → OAuth consent screen** (Google Auth Platform), choose **External** and fill in the required fields. Then, under **Audience**, either:
   - **Testing (simplest):** click **+ Add users** and add the destination Google account as a test user. Google expires the sign-in after **7 days** in this mode. If a long migration hits that, it stops with a "sign-in has expired" message: run `dbx2gdrive auth google ...` again, then re-run `migrate`, and it resumes where it stopped.
   - **Publish app:** no 7-day expiry. First complete the **Branding** page (app name, support email, developer contact). An unverified app is fine for your own use; you'll click through an "unverified app" warning once.
4. Go to **Credentials → Create credentials → OAuth client ID**, choose application type **Desktop app**, then download the JSON file.

```bash
dbx2gdrive auth google --client-secret ~/Downloads/client_secret_XXXX.json
```

A browser opens so you can sign in to the **destination** Google account. On a headless server, run it with `--no-browser --port 8765`, forward the port (`ssh -L 8765:localhost:8765 server`) and open the printed URL on your own machine.

Tokens are stored in `~/.dbx2gdrive/` (permissions `0600`). Set `DBX2GDRIVE_HOME` or pass `--config-dir` to change the location.

## Migrate

Preview what will be copied:

```bash
dbx2gdrive migrate --dry-run
```

Copy everything into a new **`Dropbox`** folder in My Drive:

```bash
dbx2gdrive migrate --log-file migration.log
```

Run the same command again at any time. It resumes interrupted work, retries failures and picks up anything new or changed in Dropbox. Use `screen`/`tmux` (or `nohup`) for big accounts.

Check progress, including from another terminal while it runs:

```bash
dbx2gdrive status
dbx2gdrive status --problems   # list failed / skipped files and why
```

### Options

| Option | Description |
| --- | --- |
| `--source /Some/Folder` | Copy just one Dropbox folder instead of the whole account |
| `--dest-folder-name NAME` | Name of the folder created in Drive (default `Dropbox`) |
| `--dest-folder-id ID` | Copy into an existing Drive folder (the ID from its URL) |
| `--shared-drive-id ID` | Copy into a shared drive instead of My Drive |
| `--workers N` | Parallel transfers (default 4; 8–16 works well on fast links) |
| `--temp-dir DIR` | Where files are staged between download and upload (needs roughly `workers × largest file` of free space) |
| `--skip-scan` | Don't re-list Dropbox; just finish what's pending from the last scan |
| `--retry-skipped` | Retry files that were previously skipped as uncopyable |
| `--home-only` | Business accounts: only your member folder, not the team space |
| `--state FILE` | Use a separate state DB (needed to run a second, different migration) |
| `-v` | Log every file |

Exit codes: `0` everything copied, `1` some files failed or were skipped (re-run, or see `status --problems`), `2` stopped on a fatal condition (for example Drive full or the daily upload limit), `130` interrupted.

## Things to know

- **Google's upload limit is about 750 GB per user per day.** When it's reached, the tool stops cleanly with a message. Re-run it the next day to continue.
- **Drive storage.** Make sure the destination account has enough free space; `--dry-run` shows the total size. Drive's per-file maximum is 5 TB.
- **What doesn't transfer.** Sharing permissions, comments, version history and links aren't copied, because Drive has no equivalent import for them. Files Dropbox refuses to serve (for example DMCA-restricted) are recorded as *skipped* and listed by `status --problems`.
- **It's a copy, not a sync.** Nothing is ever deleted from Dropbox or from Drive. If a file is deleted or renamed in Dropbox after it was copied, the old copy stays in Drive and the renamed file is copied again under its new name.
- **Disk.** Each file is streamed to a temp file, verified, uploaded and then deleted immediately.

## Development

```bash
pip install -e '.[dev]'
pytest
```
