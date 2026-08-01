#!/usr/bin/env python3
"""
POP3 to Gmail sync utility.
Fetches emails from POP3 mailboxes and imports them into Gmail.
Supports multi-instance configurations.
"""

import os
import sys
import time
import json
import fcntl
import base64
import poplib
import socket
import signal
import argparse
import urllib.request
from datetime import datetime, timezone
from typing import List, Tuple, Optional

from dotenv import dotenv_values
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

sys.dont_write_bytecode = True

SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

# Suppress oauthlib scope-change warnings (Google may add scopes like openid
# that were not explicitly requested but are implicit in the OAuth2 flow).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Set line limit to 35MB to support retrieving emails up to 25MB (accounting for ~33% base64 overhead)
poplib._MAXLINE = 35_000_000

# Localize lockfile to script directory to avoid multi-user permission conflicts
LOCKFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pop2gmail.lock")
MAX_LOG_LINES = 1000
MAX_MESSAGES_PER_RUN = 100
IMPORT_HEADER = "pop2gmail"

_lock_file = None
termination_requested = False


def signal_handler(signum, frame) -> None:
    """Handles termination signals gracefully by setting a flag."""
    global termination_requested
    if termination_requested:
        sys.exit(1)
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


def get_credentials(instance_dir: str, interactive: bool = False):
    """Retrieves or refreshes OAuth credentials for an instance.

    When interactive=False, returns None if authentication requires user
    interaction instead of prompting. When interactive=True, prompts for
    browser-based OAuth."""
    token_path = os.path.join(instance_dir, "token.json")
    creds_path = os.path.join(instance_dir, "credentials.json")
    creds = None
    
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
    if creds and creds.valid:
        return creds
        
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            suffix = ". Re-authenticating..." if interactive else "."
            print(f"[WARNING] Refreshing OAuth token failed: {e}{suffix}", file=sys.stderr)
            creds = None
            
    if not creds or not creds.valid:
        if not interactive:
            return None
        if termination_requested:
            sys.exit("Aborted during OAuth flow.")
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        flow.redirect_uri = "http://localhost"
        auth_url, _ = flow.authorization_url(access_type="offline")
        print(
            f"Open this URL in your browser:\n{auth_url}\n\n"
            "After authorizing, Google will redirect you to http://localhost — this\n"
            "will show 'connection refused' in your browser, which is expected.\n"
            "Copy the full URL from your browser's address bar and paste it here:",
            file=sys.stderr,
        )
        redirect_response = input().strip()
        redirect_response = redirect_response.replace("http:", "https:")
        flow.fetch_token(authorization_response=redirect_response)
        creds = flow.credentials
    
    # Save token.json with secure 600 permissions (read/write by owner only)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    with os.fdopen(os.open(token_path, flags, mode), "w") as token:
        token.write(creds.to_json())
        
    return creds


def get_gmail_service(instance_dir: str):
    """Returns an authorized Gmail API service client."""
    creds = get_credentials(instance_dir)
    if creds is None:
        raise ValueError("No valid credentials")
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
        creds = get_credentials(d)
        if creds is None:
            return f"No valid token — run with --auth {name} first"
        req = urllib.request.Request("https://www.googleapis.com/oauth2/v2/userinfo")
        req.add_header("Authorization", f"Bearer {creds.token}")
        with urllib.request.urlopen(req) as resp:
            profile = json.loads(resp.read())
    except Exception as e:
        return f"Gmail API verification failed: {e}"

    actual = profile["email"]
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
    """Appends log lines to file."""
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


def _deduplicate_headers(raw: bytes) -> bytes:
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        header_end = raw.find(b"\n\n")
    if header_end == -1:
        return raw

    headers = raw[:header_end]
    body = raw[header_end:]

    kept = []
    seen = set()
    skip = False

    for line in headers.split(b"\n"):
        line = line.rstrip(b"\r")
        if not line:
            continue
        is_continuation = line.startswith(b" ") or line.startswith(b"\t")
        if not is_continuation and b":" in line:
            key = line.split(b":")[0].strip().lower()
            if key in seen:
                skip = True
            else:
                seen.add(key)
                skip = False
        if not skip:
            kept.append(line)

    return b"\r\n".join(kept) + body


def _header_value(raw: bytes, name: bytes) -> str:
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        header_end = raw.find(b"\n\n")
    if header_end == -1:
        return "?"
    headers = raw[:header_end]
    for line in headers.split(b"\n"):
        line = line.rstrip(b"\r")
        if b":" in line:
            parts = line.split(b":", 1)
            if parts[0].strip().lower() == name.lower():
                return parts[1].strip().decode("utf-8", errors="replace")
    return "?"


def _modify_subject(raw: bytes, prefix: str) -> bytes:
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        header_end = raw.find(b"\n\n")
    if header_end == -1:
        return raw

    headers = raw[:header_end]
    body = raw[header_end:]
    prefix_bytes = prefix.encode("utf-8")
    modified = []
    subject_done = False

    for line in headers.split(b"\n"):
        clean = line.rstrip(b"\r")

        if not subject_done and b":" in clean:
            key, _, val = clean.partition(b":")
            if key.strip().lower() == b"subject":
                modified.append(b"Subject: " + prefix_bytes + b" " + val.strip())
                subject_done = True
                continue

        if not subject_done and clean.startswith((b" ", b"\t")):
            continue

        modified.append(clean)

    return b"\r\n".join(modified) + body


def _add_header(raw: bytes, name: str, value: str) -> bytes:
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        header_end = raw.find(b"\n\n")
    if header_end == -1:
        return raw
    header_line = f"{name}: {value}\r\n".encode("utf-8")
    return raw[:header_end] + b"\r\n" + header_line + raw[header_end:]


def process_instance(name: str, d: str, dry_run: bool, debug: bool = False) -> None:
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
                    raw_bytes = b"\r\n".join(lines)
                    raw_before = raw_bytes
                    raw_bytes = _deduplicate_headers(raw_bytes)
                    raw_bytes = _add_header(raw_bytes, "X-Imported-By", IMPORT_HEADER)

                    prefix = config.get("SUBJECT_PREFIX", "").strip()
                    if prefix:
                        raw_bytes = _modify_subject(raw_bytes, prefix)

                    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
                    body = {
                        "raw": encoded, 
                        "internalDateSource": "dateHeader",
                        "labelIds": label_ids
                    }

                    try:
                        if dry_run:
                            line = f"[{ts}] [DRY-RUN] Would import {user} msg {msg_num} with labels {label_ids}"
                            log_lines.append(line)
                            print(line)
                            imported += 1
                        else:
                            service.users().messages().import_(
                                userId="me", body=body
                            ).execute(num_retries=5)
                            imported += 1
                            pop.dele(msg_num)
                            fm = _header_value(raw_bytes, b"From")
                            to = _header_value(raw_bytes, b"To")
                            subj = _header_value(raw_bytes, b"Subject")
                            dt = _header_value(raw_bytes, b"Date")
                            line = f"[{ts}] Imported {user} msg {msg_num} ({fm} → {to} — {subj} [{dt}])"
                            log_lines.append(line)
                            print(line)
                            time.sleep(1)
                    except HttpError as e:
                        err_msg = str(e)
                        if "Invalid attachment" in err_msg:
                            pop.dele(msg_num)
                            line = f"[{ts}] SKIPPED {user} msg {msg_num} (attachment blocked by Gmail)"
                            imported += 1
                        else:
                            line = f"[{ts}] ERROR {user} msg {msg_num}: {e}"
                            errors += 1
                        log_lines.append(line)
                        print(line, file=sys.stderr)
                        if debug:
                            def _header_end(data):
                                end = data.find(b"\r\n\r\n")
                                if end == -1:
                                    end = data.find(b"\n\n")
                                return end

                            def _dump(label, data):
                                end = _header_end(data)
                                if end != -1:
                                    print(f"[DEBUG] {label} (msg {msg_num}):", file=sys.stderr)
                                    print(data[:end].decode("utf-8", errors="replace"), file=sys.stderr)

                            _dump("Pre-dedup headers", raw_before)
                            _dump("Post-dedup headers", raw_bytes)

                            print(f"[DEBUG] raw len before dedup = {len(raw_before)}", file=sys.stderr)
                            print(f"[DEBUG] raw len after  dedup = {len(raw_bytes)}", file=sys.stderr)

                            end = _header_end(raw_before)
                            if end != -1:
                                headers = raw_before[:end]
                                lines = headers.split(b"\n")
                                from_lines = [l for l in lines if l.lower().startswith(b"from")]
                                print(f"[DEBUG] Lines starting with 'From' in pre-dedup headers ({len(from_lines)}):", file=sys.stderr)
                                for l in from_lines:
                                    print(f"        {l.decode('utf-8', errors='replace')}", file=sys.stderr)

                                keys = set()
                                for l in lines:
                                    if not l.startswith((b" ", b"\t")) and b":" in l:
                                        keys.add(l.split(b":")[0].strip().lower().decode("utf-8", errors="replace"))
                                print(f"[DEBUG] Header keys found: {sorted(keys)}", file=sys.stderr)
                            else:
                                print(f"[DEBUG] No header/body boundary found (no \\r\\n\\r\\n or \\n\\n)", file=sys.stderr)
                                print(f"[DEBUG] First 2000 bytes of message:", file=sys.stderr)
                                print(raw_before[:2000].decode("utf-8", errors="replace"), file=sys.stderr)

                except Exception as e:
                    line = f"[{ts}] ERROR {user} msg {msg_num}: {e}"
                    log_lines.append(line)
                    print(line, file=sys.stderr)
                    errors += 1

        finally:
            try:
                pop.quit()
            except Exception:
                pass

    if imported > 0 or errors > 0:
        dry_prefix = "[DRY-RUN] " if dry_run else ""
        line = f"[{ts}] {dry_prefix}{name}: {imported} processed, {errors} errors"
        log_lines.insert(0, line)
        print(line)

    write_log(os.path.join(d, f"{name}.log"), log_lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="POP3 to Gmail sync utility.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without importing emails to Gmail or deleting them from POP3."
    )
    parser.add_argument(
        "--auth",
        metavar="NAME",
        help="Authenticate the named instance (e.g. --auth home) and exit."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Dump raw message headers to stderr when an import fails."
    )
    args = parser.parse_args()

    # Apply default timeout globally for all sockets (defensive measure)
    socket.setdefaulttimeout(60)
    
    acquire_lock()

    instances = get_instances()
    if not instances:
        sys.exit("[FATAL] no instances found in instances/")

    if args.auth:
        for name, d in instances:
            if name == args.auth:
                print(f"[{name}] Starting authentication...", file=sys.stderr)
                try:
                    creds = get_credentials(d, interactive=True)
                    if creds:
                        print(f"[{name}] Authentication successful.")
                    else:
                        print(f"[{name}] Authentication failed.", file=sys.stderr)
                        sys.exit(1)
                except Exception as e:
                    print(f"[{name}] Authentication failed: {e}", file=sys.stderr)
                    sys.exit(1)
                return
        sys.exit(f"[FATAL] no instance named '{args.auth}'")

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
            
            process_instance(name, d, args.dry_run, debug=args.debug)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
