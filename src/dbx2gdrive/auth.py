"""Interactive OAuth for both services.

Tokens are stored locally with 0600 permissions. On a server, supply them through
environment variables instead (see `dbx2gdrive auth export`):
  DROPBOX_APP_KEY, DROPBOX_REFRESH_TOKEN, [DROPBOX_APP_SECRET], GOOGLE_TOKEN_JSON
"""

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


def _dropbox_token(config_dir: Path) -> dict:
    if os.environ.get("DROPBOX_REFRESH_TOKEN"):
        if not os.environ.get("DROPBOX_APP_KEY"):
            raise SystemExit("DROPBOX_REFRESH_TOKEN is set but DROPBOX_APP_KEY is not.")
        return {"app_key": os.environ["DROPBOX_APP_KEY"], "app_secret": os.environ.get("DROPBOX_APP_SECRET"),
                "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"]}
    path = config_dir / "dropbox_token.json"
    if not path.exists():
        raise SystemExit("Dropbox is not authorized yet. Run: dbx2gdrive auth dropbox --app-key <KEY>")
    return json.loads(path.read_text())


def _google_token(config_dir: Path):
    """Returns (token dict, path to persist refreshed tokens to or None)."""
    if os.environ.get("GOOGLE_TOKEN_JSON"):
        try:
            return json.loads(os.environ["GOOGLE_TOKEN_JSON"]), None
        except ValueError:
            raise SystemExit("GOOGLE_TOKEN_JSON is not valid JSON (paste it exactly as `auth export` prints it).")
    path = config_dir / "google_token.json"
    if not path.exists():
        raise SystemExit("Google Drive is not authorized yet. Run: dbx2gdrive auth google --client-secret <FILE>")
    return json.loads(path.read_text()), path


def export_env(config_dir: Path) -> dict:
    """The environment variables a server needs, built from the local sign-ins."""
    dbx = _dropbox_token(config_dir)
    google, _ = _google_token(config_dir)
    env = {"DROPBOX_APP_KEY": dbx["app_key"], "DROPBOX_REFRESH_TOKEN": dbx["refresh_token"]}
    if dbx.get("app_secret"):
        env["DROPBOX_APP_SECRET"] = dbx["app_secret"]
    google = {k: v for k, v in google.items() if k not in ("token", "expiry")}  # long-lived parts only
    env["GOOGLE_TOKEN_JSON"] = json.dumps(google, separators=(",", ":"))
    return env


def import_env(config_dir: Path, text: str) -> None:
    """Inverse of export_env: store pasted KEY=VALUE lines as this machine's token files."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    missing = [k for k in ("DROPBOX_APP_KEY", "DROPBOX_REFRESH_TOKEN", "GOOGLE_TOKEN_JSON") if k not in env]
    if missing:
        raise SystemExit(f"Missing from the pasted text: {', '.join(missing)}")
    try:
        google = json.loads(env["GOOGLE_TOKEN_JSON"])
    except ValueError:
        raise SystemExit("GOOGLE_TOKEN_JSON is not valid JSON - paste the `auth export` output exactly.")
    _write_secret(config_dir / "dropbox_token.json",
                  {"app_key": env["DROPBOX_APP_KEY"], "app_secret": env.get("DROPBOX_APP_SECRET"),
                   "refresh_token": env["DROPBOX_REFRESH_TOKEN"]})
    _write_secret(config_dir / "google_token.json", google)


def dropbox_client(config_dir: Path):
    import dropbox

    tok = _dropbox_token(config_dir)
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

    info, path = _google_token(config_dir)
    creds = Credentials.from_authorized_user_info(info, GOOGLE_SCOPES)
    if not creds.valid:
        from google.auth.exceptions import RefreshError

        try:
            creds.refresh(Request())
        except RefreshError:
            raise SystemExit("Google sign-in has expired or was revoked (apps in 'Testing' status expire "
                             "after 7 days). Run: dbx2gdrive auth google --client-secret <FILE>")
        if path:
            _write_secret(path, json.loads(creds.to_json()))
    return creds
