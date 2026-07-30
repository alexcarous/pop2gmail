#!/usr/bin/env python3
import os
import sys
import atexit
import base64
import poplib

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

poplib._MAXLINE = 10_000_000

LOCKFILE = "/tmp/poptogmail.lock"


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


def process_instance(name, d):
    env_path = os.path.join(d, ".env")
    load_dotenv(env_path)

    host = os.environ["POP3_HOST"]
    user = os.environ["POP3_USER"]
    password = os.environ["POP3_PASS"]

    service = get_gmail_service(d)

    imported = 0
    errors = 0

    pop = poplib.POP3_SSL(host, 995, timeout=30)
    try:
        pop.user(user)
        pop.pass_(password)

        resp, msg_list, _ = pop.list()
        if not msg_list:
            return imported, errors

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
                    print(f"[{name}] ERROR msg {msg_num}: {e}", file=sys.stderr)
                    errors += 1

                pop.dele(msg_num)

            except Exception as e:
                print(f"[{name}] ERROR msg {msg_num}: {e}", file=sys.stderr)
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

    return imported, errors


def main():
    acquire_lock()

    instances = get_instances()
    if not instances:
        sys.exit("[FATAL] no instances found in instances/")

    for name, d in instances:
        err = validate_instance(name, d)
        if err:
            sys.exit(f"[FATAL] {name}: {err}")

    any_output = False
    for name, d in instances:
        try:
            imported, errors = process_instance(name, d)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)
            any_output = True
            continue

        if imported > 0 or errors > 0:
            print(f"{name}: {imported} imported, {errors} errors")
            any_output = True

    if not any_output:
        print("no new mail")


if __name__ == "__main__":
    main()
