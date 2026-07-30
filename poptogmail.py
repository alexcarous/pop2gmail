#!/usr/bin/env python3
import os
import sys
import time
import fcntl
import base64
import poplib
from datetime import datetime, timezone

from dotenv import dotenv_values
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Set line limit to 35MB to support retrieving emails up to 25MB (accounting for ~33% base64 overhead)
poplib._MAXLINE = 35_000_000

# Localize lockfile to script directory to avoid multi-user permission conflicts
LOCKFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poptogmail.lock")
MAX_LOG_LINES = 1000
MAX_MESSAGES_PER_RUN = 100

_lock_file = None


def acquire_lock():
    global _lock_file
    _lock_file = open(LOCKFILE, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("[FATAL] another instance is running")


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
        creds = flow.run_console()
    
    # Save token.json with secure 600 permissions (read/write by owner only)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    with os.fdopen(os.open(token_path, flags, mode), "w") as token:
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
    config = dotenv_values(env_path)

    expected = config.get("EXPECTED_GMAIL", "").strip()
    if not expected:
        return "EXPECTED_GMAIL not set"

    service = get_gmail_service(d)
    profile = service.users().getProfile(userId="me").execute(num_retries=5)
    actual = profile["emailAddress"]
    if actual.lower() != expected.lower():
        return f"Gmail mismatch: expected '{expected}', got '{actual}'"

    host = config.get("POP3_HOST")
    if not host:
        return "POP3_HOST not set"
    pop3_users_raw = config.get("POP3_USERS")
    if not pop3_users_raw:
        return "POP3_USERS not set"
    
    users = [u.strip() for u in pop3_users_raw.split(",") if u.strip()]
    password = config.get("POP3_PASS")
    if not password:
        return "POP3_PASS not set"

    for user in users:
        try:
            pop = poplib.POP3_SSL(host, 995, timeout=10)
            pop.user(user)
            pop.pass_(password)
            pop.quit()
        except Exception as e:
            return f"POP3 error for {user}: {e}"

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
    config = dotenv_values(env_path)

    host = config.get("POP3_HOST")
    pop3_users_raw = config.get("POP3_USERS")
    users = [u.strip() for u in pop3_users_raw.split(",") if u.strip()]
    password = config.get("POP3_PASS")

    service = get_gmail_service(d)

    imported = 0
    errors = 0
    log_lines = []

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for user in users:
        pop = poplib.POP3_SSL(host, 995, timeout=30)
        try:
            pop.user(user)
            pop.pass_(password)

            resp, msg_list, _ = pop.list()
            if not msg_list:
                continue

            # Limit the number of messages processed in this run to avoid connection timeouts
            msgs_to_process = msg_list[:MAX_MESSAGES_PER_RUN]

            for item in msgs_to_process:
                msg_num = int(item.split()[0])
                try:
                    resp, lines, _ = pop.retr(msg_num)
                    raw_bytes = b"".join(lines)

                    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
                    body = {"raw": encoded, "internalDateSource": "dateHeader"}

                    try:
                        service.users().messages().import_(
                            userId="me", body=body
                        ).execute(num_retries=5)
                        imported += 1
                        pop.dele(msg_num)  # Only delete from POP3 server if import succeeded
                        time.sleep(1)      # 1 second pause between successful imports
                    except HttpError as e:
                        log_lines.append(f"[{ts}] ERROR {user} msg {msg_num}: {e}")
                        errors += 1

                except Exception as e:
                    log_lines.append(f"[{ts}] ERROR {user} msg {msg_num}: {e}")
                    errors += 1

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

    # Process instances sequentially; if one fails validation, print error, log it, and continue to others.
    for name, d in instances:
        try:
            err = validate_instance(name, d)
            if err:
                print(f"[{name}] Validation failed: {err}", file=sys.stderr)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                write_log(os.path.join(d, f"{name}.log"), [f"[{ts}] Validation failed: {err}"])
                continue
            
            process_instance(name, d)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
