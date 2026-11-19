"""Command-line entry point: dbx2gdrive."""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__

DEFAULT_CONFIG = Path(os.environ.get("DBX2GDRIVE_HOME", Path.home() / ".dbx2gdrive"))
log = logging.getLogger("dbx2gdrive")


def _setup_logging(verbose: bool, log_file: str = None) -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("googleapiclient", "urllib3", "dropbox", "google_auth_oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_auth(args) -> None:
    from . import auth

    if args.service == "export":
        return cmd_export(args)
    if args.service == "import":
        if sys.stdin.isatty():
            print("Paste the output of `dbx2gdrive auth export`, then press Ctrl-D:", file=sys.stderr)
        auth.import_env(args.config_dir, sys.stdin.read())
        print(f"Sign-ins saved to {args.config_dir}.")
        return
    if args.service == "dropbox":
        key = args.app_key or os.environ.get("DROPBOX_APP_KEY")
        if not key:
            raise SystemExit("Pass --app-key or set DROPBOX_APP_KEY (see README: creating a Dropbox app).")
        auth.dropbox_login(args.config_dir, key, args.app_secret or os.environ.get("DROPBOX_APP_SECRET"))
    else:
        if not args.client_secret:
            raise SystemExit("Pass --client-secret path/to/client_secret.json (see README).")
        auth.google_login(args.config_dir, args.client_secret, open_browser=not args.no_browser, port=args.port)


def _state_path(args) -> str:
    return (args.state or os.environ.get("DBX2GDRIVE_STATE") or os.environ.get("DATABASE_URL")
            or str(args.config_dir / "state.sqlite3"))


def _idle(message: str, seconds: float = None) -> None:
    """Service mode: wait instead of exiting, so the host doesn't restart us in a loop."""
    deadline = time.monotonic() + seconds if seconds else None
    while deadline is None or time.monotonic() < deadline:
        log.info(message)
        time.sleep(min(3600, deadline - time.monotonic()) if deadline else 3600)


def cmd_migrate(args) -> int:
    if not args.service:
        return _migrate_once(args)
    from .retry import FatalError

    failed_rounds = 0
    while True:
        try:
            rc = _migrate_once(args, service=True)
        except FatalError as exc:
            if exc.retry_in:
                log.warning("%s Waiting, then resuming automatically.", exc)
                _idle("Waiting for Google's limit to reset ...", exc.retry_in)
                continue
            _idle(f"PAUSED: {exc} Fix this, then restart the service to continue.")
        except SystemExit as exc:  # e.g. missing/expired sign-in
            _idle(f"PAUSED: {exc} Then restart the service.")
        except Exception as exc:
            if type(exc).__name__ == "AuthError":  # dropbox.exceptions.AuthError
                _idle(f"PAUSED: Dropbox rejected the sign-in ({exc}). Re-run `dbx2gdrive auth dropbox`, "
                      "re-import the sign-ins, then restart the service.")
            log.exception("Unexpected error; retrying in 5 minutes")
            time.sleep(300)
            continue
        if rc == 0:
            _idle("Migration complete. Nothing left to do - you can stop/delete this service now.")
        failed_rounds += 1
        if failed_rounds > 3:
            _idle("Migration finished, but some files could not be copied (see `dbx2gdrive status "
                  "--problems`). You can stop this service.")
        log.info("Some files failed; retrying them in 10 minutes (round %d of 3).", failed_rounds)
        time.sleep(600)
        args.skip_scan = True


def _migrate_once(args, service: bool = False) -> int:
    from . import auth
    from .drive_dest import DriveDest
    from .dropbox_source import DropboxSource
    from .migrator import Migrator, Options, human
    from .retry import FatalError
    from .state import State, is_postgres

    src = DropboxSource(lambda: auth.dropbox_client(args.config_dir), include_team_space=not args.home_only)
    log.info("Source:      Dropbox account %s", src.account_label())

    if args.dry_run:
        state = State(":memory:")
        m = Migrator(src, None, state, "", Options(source_root=args.source))
        m.scan()
        _print_summary(state, dry_run=True)
        return 0

    dst = DriveDest(auth.google_credentials(args.config_dir), shared_drive_id=args.shared_drive_id)
    log.info("Destination: Google Drive account %s", dst.account_label())
    state_path = _state_path(args)
    if not is_postgres(state_path):
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    state = State(state_path)

    stored_root = state.get_meta("dest_root_id")
    stored_source = state.get_meta("source_root")
    if stored_source is not None and stored_source != args.source:
        raise SystemExit(f"This state database belongs to a migration of Dropbox '{stored_source or '/'}'. "
                         "Use --state with a new database for a different source folder.")
    if args.dest_folder_id:
        if stored_root and stored_root != args.dest_folder_id:
            raise SystemExit("This state database belongs to a different Drive destination. Use --state <new>.")
        root_id = args.dest_folder_id
        log.info("Destination folder: %s", dst.check_folder(root_id))
    elif stored_root:
        root_id = stored_root
    else:
        parent = args.shared_drive_id or "root"
        root_id = dst.ensure_folder(args.dest_folder_name, parent)
        log.info("Destination folder: %s (id %s)", args.dest_folder_name, root_id)
    state.set_meta("dest_root_id", root_id)
    state.set_meta("source_root", args.source)

    opts = Options(source_root=args.source, workers=args.workers, retry_skipped=args.retry_skipped,
                   skip_scan=args.skip_scan)
    migrator = Migrator(src, dst, state, root_id, opts)
    try:
        migrator.run()
    except FatalError as exc:
        log.error("Migration stopped: %s", exc)
        _print_summary(state)
        if service:
            raise
        return 2
    except KeyboardInterrupt:
        log.warning("Stopped by user. Run the same command again to resume.")
        _print_summary(state)
        return 130
    log.info("Run finished: %d files copied (%s), %d failed.",
             migrator.files_done, human(migrator.bytes_done), migrator.files_failed)
    return _print_summary(state)


def cmd_export(args) -> int:
    from . import auth

    env = auth.export_env(args.config_dir)
    print("# Secrets: paste these into your server's environment settings. Do not share or commit them.",
          file=sys.stderr)
    for k, v in env.items():
        print(f"{k}={v}")
    return 0


def _print_summary(state, dry_run: bool = False) -> int:
    from .migrator import human

    s = state.summary()
    def get(kind, status):
        return s.get((kind, status), (0, 0))
    files_total = sum(v[0] for (k, _), v in s.items() if k == "file")
    bytes_total = sum(v[1] for (k, _), v in s.items() if k == "file")
    folders_total = sum(v[0] for (k, _), v in s.items() if k == "folder")
    print()
    if dry_run:
        print(f"Dry run: {files_total} files ({human(bytes_total)}) in {folders_total} folders would be copied.")
        nd = state.undownloadable_count()
        if nd:
            print(f"{nd} files cannot be downloaded from Dropbox and will be skipped.")
        return 0
    print(f"Folders: {get('folder', 'done')[0]}/{folders_total} created")
    print(f"Files:   {get('file', 'done')[0]}/{files_total} copied "
          f"({human(get('file', 'done')[1])} of {human(bytes_total)})")
    failed, skipped = get("file", "failed")[0] + get("folder", "failed")[0], get("file", "skipped")[0]
    pending = get("file", "pending")[0]
    if pending:
        print(f"Pending: {pending} files not attempted yet")
    if failed or skipped:
        print(f"Problems: {failed} failed (will be retried on the next run), "
              f"{skipped} skipped (cannot be copied; see `dbx2gdrive status --problems`)")
    if not (failed or pending):
        print("Migration complete." if not skipped else "Migration complete apart from skipped files.")
        return 0
    return 1


def cmd_status(args) -> int:
    from .state import State

    from .state import is_postgres

    path = _state_path(args)
    if not is_postgres(path) and not Path(path).exists():
        raise SystemExit(f"No migration state at {path}")
    state = State(path)
    rc = _print_summary(state)
    if args.problems:
        print()
        for r in state.problems(limit=args.limit):
            print(f"[{r['status']}] {r['path_display']}\n    {r['error']}")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dbx2gdrive", description="Bulk-migrate a Dropbox account to Google Drive.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG,
                   help=f"where tokens and state live (default {DEFAULT_CONFIG})")
    p.add_argument("-v", "--verbose", action="store_true", help="log every file")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("auth", help="authorize access to Dropbox or Google Drive")
    a.add_argument("service", choices=["dropbox", "google", "export", "import"],
                   help="'export' prints the sign-ins for a server; 'import' (on the server) reads them")
    a.add_argument("--app-key", help="Dropbox app key (or env DROPBOX_APP_KEY)")
    a.add_argument("--app-secret", help="Dropbox app secret (optional; PKCE is used without it)")
    a.add_argument("--client-secret", help="Google OAuth desktop client JSON file")
    a.add_argument("--no-browser", action="store_true", help="print the Google URL instead of opening a browser")
    a.add_argument("--port", type=int, default=0, help="local port for the Google OAuth redirect (default: random)")
    a.set_defaults(func=cmd_auth)

    m = sub.add_parser("migrate", help="copy everything (re-run to resume or to pick up changes)")
    m.add_argument("--source", default="", help="Dropbox folder to copy (default: the whole account)")
    dest = m.add_mutually_exclusive_group()
    dest.add_argument("--dest-folder-id", help="existing Google Drive folder ID to copy into")
    dest.add_argument("--dest-folder-name", default="Dropbox",
                      help="name of the folder to create in My Drive / the shared drive (default: Dropbox)")
    m.add_argument("--shared-drive-id", help="copy into this shared drive instead of My Drive")
    m.add_argument("--workers", type=int, default=4, help="parallel transfers (default 4)")
    m.add_argument("--state", help="state database: a file path or a postgres:// URL "
                   "(default: $DBX2GDRIVE_STATE, $DATABASE_URL, or <config-dir>/state.sqlite3)")
    m.add_argument("--service", action="store_true",
                   help="for servers: never exit; wait out daily limits, retry failures, then idle")
    m.add_argument("--dry-run", action="store_true", help="only list what would be copied")
    m.add_argument("--skip-scan", action="store_true", help="don't re-list Dropbox; just finish what's pending")
    m.add_argument("--retry-skipped", action="store_true", help="also retry files previously skipped as uncopyable")
    m.add_argument("--home-only", action="store_true",
                   help="business accounts: copy only your member folder, not the team space")
    m.add_argument("--log-file", help="also write the log to this file")
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("status", help="show progress of the migration")
    s.add_argument("--state", help="state database path or postgres:// URL")
    s.add_argument("--problems", action="store_true", help="list failed/skipped files and why")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, getattr(args, "log_file", None))
    if args.command == "migrate":
        args.source = "" if args.source in ("", "/") else "/" + args.source.strip("/")
        if args.workers < 1:
            raise SystemExit("--workers must be at least 1")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
