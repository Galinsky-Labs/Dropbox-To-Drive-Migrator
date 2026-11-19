"""Interactive OAuth for both services. Tokens are stored locally with 0600 permissions."""

import json
import os
from pathlib import Path

DROPBOX_SCOPES = ["account_info.read", "files.metadata.read", "files.content.read"]
# Full Drive scope so you can migrate into an existing folder or shared drive you pick by ID.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _write_secret(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def dropbox_login(config_dir: Path, app_key: str, app_secret: str = None) -> None:
    from dropbox import DropboxOAuth2FlowNoRedirect

    flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        consumer_secret=app_secret,
        use_pkce=app_secret is None,
        token_access_type="offline",
        scope=DROPBOX_SCOPES,
    )
    print("1. Open this URL and click 'Allow':\n\n   " + flow.start() + "\n")
    code = input("2. Paste the authorization code here: ").strip()
    result = flow.finish(code)
    _write_secret(
        config_dir / "dropbox_token.json",
        {"app_key": app_key, "app_secret": app_secret, "refresh_token": result.refresh_token},
    )
    print(f"Dropbox authorized for account {result.account_id}.")


def dropbox_client(config_dir: Path):
    import dropbox

    path = config_dir / "dropbox_token.json"
    if not path.exists():
        raise SystemExit("Dropbox is not authorized yet. Run: dbx2gdrive auth dropbox --app-key <KEY>")
    tok = json.loads(path.read_text())
    return dropbox.Dropbox(
        oauth2_refresh_token=tok["refresh_token"],
        app_key=tok["app_key"],
        app_secret=tok.get("app_secret"),
        max_retries_on_error=5,
        max_retries_on_rate_limit=20,
        timeout=300,
    )


def google_login(config_dir: Path, client_secret_file: str, open_browser: bool = True, port: int = 0) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, GOOGLE_SCOPES)
    creds = flow.run_local_server(port=port, open_browser=open_browser, prompt="consent", access_type="offline")
    _write_secret(config_dir / "google_token.json", json.loads(creds.to_json()))
    print("Google Drive authorized.")


def google_credentials(config_dir: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = config_dir / "google_token.json"
    if not path.exists():
        raise SystemExit("Google Drive is not authorized yet. Run: dbx2gdrive auth google --client-secret <FILE>")
    creds = Credentials.from_authorized_user_file(str(path), GOOGLE_SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        _write_secret(path, json.loads(creds.to_json()))
    return creds
