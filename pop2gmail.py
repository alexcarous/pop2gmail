#!/usr/bin/env python3
import argparse
import os
import sys
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True, help="instance name under instances/")
    args = parser.parse_args()

    instance_dir = os.path.join("instances", args.instance)
    env_path = os.path.join(instance_dir, ".env")

    if not os.path.isdir(instance_dir):
        sys.exit(f"[FATAL] instance directory not found: {instance_dir}")
    if not os.path.isfile(env_path):
        sys.exit(f"[FATAL] .env not found in instance directory: {instance_dir}")

    load_dotenv(env_path)

    expected_gmail = os.environ.get("EXPECTED_GMAIL", "").strip()
    if not expected_gmail:
        sys.exit("[FATAL] EXPECTED_GMAIL is not set in .env")

    host = os.environ["POP3_HOST"]
    user = os.environ["POP3_USER"]
    password = os.environ["POP3_PASS"]

    service = get_gmail_service(instance_dir)

    profile = service.users().getProfile(userId="me").execute()
    actual_email = profile["emailAddress"]
    if actual_email.lower() != expected_gmail.lower():
        sys.exit(
            f"[FATAL] Gmail account mismatch: "
            f"expected '{expected_gmail}', got '{actual_email}'"
        )

    pop = poplib.POP3_SSL(host, 995, timeout=30)
    try:
        pop.user(user)
        pop.pass_(password)

        resp, msg_list, _ = pop.list()
        if not msg_list:
            return

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
                except HttpError as e:
                    print(f"[ERROR] msg {msg_num}: {e}", file=sys.stderr)

                pop.dele(msg_num)

            except Exception as e:
                print(f"[ERROR] msg {msg_num}: {e}", file=sys.stderr)
                try:
                    pop.dele(msg_num)
                except Exception:
                    pass

    finally:
        try:
            pop.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
