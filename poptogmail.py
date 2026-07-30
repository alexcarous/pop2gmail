#!/usr/bin/env python3
import os
import sys
import atexit
import base64
import poplib
from datetime import datetime, timezone

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

poplib._MAXLINE = 10_000_000

LOCKFILE = "/tmp/poptogmail.lock"
MAX_LOG_LINES = 1000


def _release_lock():
    try:
        os.remove(LOCKFILE)
    except FileNotFoundError:
        pass


def acquire_lock():
    if os.path.exists(LOCKFILE):
        with open(LOCKFILE) as f:
            pid = f.read().strip()
        try:
            os.kill(int(pid), 0)
            sys.exit(f"[FATAL] another instance is running (pid {pid})")
        except (OSError, ValueError, ProcessLookupError):
            pass
    with open(LOCKFILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)


def get_gmail_service(instance_dir):
    token_path = os.path.join(instance_dir, "token.json")
    creds_path = os.path.join(instance_dir, "credentials.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(token_path, "w") as token:
        token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_instances():
    instances = []
    base = "instances"
    if not os.path.isdir(base):
        return instances
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        env_path = os.path.join(d, ".env")
        if os.path.isdir(d) and name != "example" and os.path.isfile(env_path):
            instances.append((name, d))
    return instances


def validate_instance(name, d):
    env_path = os.path.join(d, ".env")
    load_dotenv(env_path)

    expected = os.environ.get("EXPECTED_GMAIL", "").strip()
    if not expected:
        return "EXPECTED_GMAIL not set"

    service = get_gmail_service(d)
    profile = service.users().getProfile(userId="me").execute()
    actual = profile["emailAddress"]
    if actual.lower() != expected.lower():
        return f"Gmail mismatch: expected '{expected}', got '{actual}'"

    host = os.environ["POP3_HOST"]
    user = os.environ["POP3_USER"]
    password = os.environ["POP3_PASS"]

    try:
        pop = poplib.POP3_SSL(host, 995, timeout=10)
        pop.user(user)
        pop.pass_(password)
        pop.quit()
    except Exception as e:
        return f"POP3 error: {e}"

    return None


def write_log(log_path, lines):
    if not lines:
        return

    existing = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            existing = f.read().splitlines()

    all_lines = existing + lines
    if len(all_lines) > MAX_LOG_LINES:
        all_lines = all_lines[-MAX_LOG_LINES:]

    with open(log_path, "w") as f:
        f.write("\n".join(all_lines) + "\n")


def process_instance(name, d):
    env_path = os.path.join(d, ".env")
    load_dotenv(env_path)

    host = os.environ["POP3_HOST"]
    user = os.environ["POP3_USER"]
    password = os.environ["POP3_PASS"]

    service = get_gmail_service(d)

    imported = 0
    errors = 0
    log_lines = []

    pop = poplib.POP3_SSL(host, 995, timeout=30)
    try:
        pop.user(user)
        pop.pass_(password)

        resp, msg_list, _ = pop.list()
        if not msg_list:
            return

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for item in msg_list:
            msg_num = int(item.split()[0])
            try:
                resp, lines, _ = pop.retr(msg_num)
                raw_bytes = b"".join(lines)

                encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
                body = {"raw": encoded, "internalDateSource": "dateHeader"}

                try:
                    service.users().messages().import_(
                        userId="me", body=body
                    ).execute()
                    imported += 1
                except HttpError as e:
                    log_lines.append(f"[{ts}] ERROR msg {msg_num}: {e}")
                    errors += 1

                pop.dele(msg_num)

            except Exception as e:
                log_lines.append(f"[{ts}] ERROR msg {msg_num}: {e}")
                errors += 1
                try:
                    pop.dele(msg_num)
                except Exception:
                    pass

    finally:
        try:
            pop.quit()
        except Exception:
            pass

    if imported > 0 or errors > 0:
        log_lines.insert(0, f"[{ts}] {name}: {imported} imported, {errors} errors")

    write_log(os.path.join(d, f"{name}.log"), log_lines)


def main():
    acquire_lock()

    instances = get_instances()
    if not instances:
        sys.exit("[FATAL] no instances found in instances/")

    for name, d in instances:
        err = validate_instance(name, d)
        if err:
            sys.exit(f"[FATAL] {name}: {err}")

    for name, d in instances:
        try:
            process_instance(name, d)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
