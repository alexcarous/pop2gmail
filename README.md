# poptogmail

Import emails from a POP3 mailbox into Gmail via the Gmail API. Designed for
migrating email from a provider like mailserver, with minimal overhead and no
local attachment processing.

## How it works

1. Connects to the POP3 server over TLS (port 995)
2. For each message: downloads raw bytes → base64url-encodes → sends to
   `users.messages.import` with `internalDateSource: dateHeader` to preserve
   original timestamps
3. Deletes the message from the POP3 server
4. Messages that fail to import are still deleted (force-delete mode)

No attachments are parsed, rendered, or saved to disk. TLS in transit for both
POP3 and Gmail API. Google's spam/malware scanning applies via `import`.

## Requirements

- Python 3.9+
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the Gmail API enabled
- A POP3 mailbox

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Google Cloud Console — create OAuth credentials

- Go to [console.cloud.google.com](https://console.cloud.google.com)
- Create a project (or select an existing one)
- **Enable the Gmail API:** APIs & Services → Library → search "Gmail API" → Enable
- **Configure OAuth consent screen:** APIs & Services → OAuth consent screen
  - User type: External
  - Fill in app name and support email
  - Add scope: `https://www.googleapis.com/auth/gmail.modify`
  - Add your email as a test user
- **Create OAuth client ID:** APIs & Services → Credentials → Create Credentials → OAuth client ID
  - Application type: Desktop app
  - Download the JSON and save it as `credentials.json` in the project directory

### 3. Configure POP3 credentials

```bash
cp .env.example .env
```

Edit `.env` with your POP3 host, username, and password.

### 4. First run — authorize with Google

```bash
uv run python poptogmail.py
```

This opens a browser for Google sign-in. After authorizing, a `token.json`
file is created with a long-lived refresh token. Subsequent runs are
fully unattended.

### 5. Secure credential files

```bash
chmod 600 .env token.json
```

### 6. Schedule with cron (optional)

```
0 * * * * cd /home/pi/poptogmail && uv run python poptogmail.py
```

## Running

```bash
uv run python poptogmail.py
```

The script is silent on success. Errors are printed to stderr with
`[ERROR] msg <n>: <detail>`.

## File structure

```
poptogmail/
├── .venv/                   # virtual environment (uv)
├── credentials.json         # OAuth client ID (you create this)
├── token.json               # OAuth refresh token (auto-generated)
├── poptogmail.py            # main script
├── pyproject.toml           # project config
├── uv.lock                  # pinned dependencies
├── requirements.txt         # dependencies (compatibility)
├── .env                     # POP3 credentials (you create this)
├── .env.example             # template for .env
└── .gitignore
```

## Security notes

- Passwords and tokens stored on disk in `.env` and `token.json`
- Restrict with `chmod 600` to limit exposure to the file owner
- No email content is written to disk at any point
- All network connections use TLS (POP3 on 995, Gmail API over HTTPS)
- Revoke the token at any time at https://myaccount.google.com/permissions
