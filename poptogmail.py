#!/usr/bin/env python3
"""
POP3 to Gmail sync utility.
Fetches emails from POP3 mailboxes and imports them into Gmail.
Supports multi-instance configurations.
"""

import os
import sys
import time
import fcntl
import base64
import poplib
import socket
import signal
import argparse
from datetime import datetime, timezone
from typing import List, Tuple, Optional

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
termination_requested = False


def signal_handler(signum, frame) -> None:
    """Handles termination signals gracefully by setting a flag."""
    global termination_requested
    termination_requested = True
    print("\n[INFO] Termination requested. Gracefully stopping after current message...", file=sys.stderr)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def acquire_lock() -> None:
    """Acquires a kernel-level file lock to prevent concurrent runs."""
    global _lock_file
    _lock_file = open(LOCKFILE, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("[FATAL] another instance is running")


def get_gmail_service(instance_dir: str):
    """Retrieves or refreshes the Gmail API service client."""
    token_path = os.path.join(instance_dir, "token.json")
    creds_path = os.path.join(instance_dir, "credentials.json")
    creds = None
    
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds)
        
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"[WARNING] Refreshing OAuth token failed: {e}. Re-authenticating...", file=sys.stderr)
            creds = None  # Force re-authentication flow
            
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        creds = flow.run_local_server(port=0)
    
    # Save token.json with secure 600 permissions (read/write by owner only)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    with os.fdopen(os.open(token_path, flags, mode), "w") as token:
        token.write(creds.to_json())
        
    return build("gmail", "v1", credentials=creds)


def get_instances() -> List[Tuple[str, str]]:
    """Discovers valid non-example instance subdirectories, avoiding symlinks."""
    instances = []
    base = "instances"
    if not os.path.isdir(base):
        return instances
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        env_path = os.path.join(d, ".env")
        # Ensure we do not follow symlinks for safety
        if os.path.isdir(d) and not os.path.islink(d) and name != "example" and os.path.isfile(env_path):
            instances.append((name, d))
    return instances


def validate_instance(name: str, d: str) -> Optional[str]:
    """Validates the configuration and connection for a specific instance."""
    env_path = os.path.join(d, ".env")
    config = dotenv_values(env_path)

    expected = config.get("EXPECTED_GMAIL", "").strip()
    if not expected:
        return "EXPECTED_GMAIL not set in .env"

    creds_path = os.path.join(d, "credentials.json")
    if not os.path.exists(creds_path):
        return "credentials.json missing in instance directory"

    try:
        service = get_gmail_service(d)
        profile = service.users().getProfile(userId="me").execute(num_retries=5)
    except Exception as e:
        return f"Gmail API verification failed: {e}"

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

    port = int(config.get("POP3_PORT", 995))
    timeout = int(config.get("POP3_TIMEOUT", 10))

    for user in users:
        try:
            pop = poplib.POP3_SSL(host, port, timeout=timeout)
            pop.user(user)
            pop.pass_(password)
            pop.quit()
        except Exception as e:
            return f"POP3 connection error for {user}: {e}"

    return None


def write_log(log_path: str, lines: List[str]) -> None:
    """Appends log lines to file and prints them to terminal for console feedback."""
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
        
    for line in lines:
        print(line)


def process_instance(name: str, d: str, dry_run: bool) -> None:
    """Processes message retrieval and GMail import for a specific instance."""
    env_path = os.path.join(d, ".env")
    config = dotenv_values(env_path)

    host = config.get("POP3_HOST")
    pop3_users_raw = config.get("POP3_USERS")
    users = [u.strip() for u in pop3_users_raw.split(",") if u.strip()]
    password = config.get("POP3_PASS")
    
    port = int(config.get("POP3_PORT", 995))
    timeout = int(config.get("POP3_TIMEOUT", 30))
    
    gmail_labels_raw = config.get("GMAIL_LABELS", "INBOX,UNREAD")
    label_ids = [lbl.strip() for lbl in gmail_labels_raw.split(",") if lbl.strip()]

    service = get_gmail_service(d)

    imported = 0
    errors = 0
    log_lines = []

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for user in users:
        if termination_requested:
            break
            
        pop = poplib.POP3_SSL(host, port, timeout=timeout)
        try:
            pop.user(user)
            pop.pass_(password)

            resp, msg_list, _ = pop.list()
            if not msg_list:
                continue

            # Limit the number of messages processed in this run to avoid connection timeouts
            msgs_to_process = msg_list[:MAX_MESSAGES_PER_RUN]

            for item in msgs_to_process:
                if termination_requested:
                    break
                    
                msg_num = int(item.split()[0])
                try:
                    resp, lines, _ = pop.retr(msg_num)
                    raw_bytes = b"".join(lines)

                    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
                    body = {
                        "raw": encoded, 
                        "internalDateSource": "dateHeader",
                        "labelIds": label_ids
                    }

                    try:
                        if dry_run:
                            log_lines.append(f"[{ts}] [DRY-RUN] Would import {user} msg {msg_num} with labels {label_ids}")
                            imported += 1
                        else:
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
        dry_prefix = "[DRY-RUN] " if dry_run else ""
        log_lines.insert(0, f"[{ts}] {dry_prefix}{name}: {imported} processed, {errors} errors")

    write_log(os.path.join(d, f"{name}.log"), log_lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="POP3 to Gmail sync utility.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without importing emails to Gmail or deleting them from POP3."
    )
    args = parser.parse_args()

    # Apply default timeout globally for all sockets (defensive measure)
    socket.setdefaulttimeout(60)
    
    acquire_lock()

    instances = get_instances()
    if not instances:
        sys.exit("[FATAL] no instances found in instances/")

    # Validate all instances
    for name, d in instances:
        if termination_requested:
            break
        try:
            err = validate_instance(name, d)
            if err:
                print(f"[{name}] Validation failed: {err}", file=sys.stderr)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                write_log(os.path.join(d, f"{name}.log"), [f"[{ts}] Validation failed: {err}"])
                continue
            
            process_instance(name, d, args.dry_run)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
