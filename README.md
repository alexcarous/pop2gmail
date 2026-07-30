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
  - Download the JSON and save it into your instance directory (see step 3)

### 3. Create an instance

Each email account lives in its own instance directory under `instances/`. The
layout for an instance called `home`:

```
instances/home/
├── .env              # POP3 credentials + EXPECTED_GMAIL
├── credentials.json  # OAuth client ID (from Google Cloud Console)
└── token.json        # OAuth refresh token (auto-generated on first run)
```

Start from the example:

```bash
cp -r instances/example instances/home
```

Edit `instances/home/.env` with your POP3 host, username, and password. **You
must also set `EXPECTED_GMAIL`** — this is the Gmail address the OAuth token
should resolve to. The script verifies this before processing any mail, so the
wrong credentials can never import into the wrong inbox.

### 4. First run — authorize with Google

```bash
uv run python poptogmail.py --instance home
```

This opens a browser for Google sign-in. After authorizing, a `token.json`
file is created in the instance directory with a long-lived refresh token.
Subsequent runs are fully unattended.

### 5. Secure credential files

```bash
chmod -R 600 instances/home/.env instances/home/token.json
```

### 6. Additional instances

Repeat steps 3-5 for each additional account, using a different instance name:

```bash
cp -r instances/example instances/work
# edit instances/work/.env with that account's credentials
uv run python poptogmail.py --instance work
```

### 7. Schedule with cron (optional)

```
0 * * * * cd /home/alex/scripts/poptogmail && uv run python poptogmail.py --instance home
0 * * * * cd /home/alex/scripts/poptogmail && uv run python poptogmail.py --instance work
```

## Running

```bash
uv run python poptogmail.py --instance <name>
```

The script is silent on success. Errors are printed to stderr with
`[ERROR] msg <n>: <detail>`.

If the OAuth token resolves to a Gmail address different from `EXPECTED_GMAIL`,
the script exits immediately with a fatal error before processing any mail.

## File structure

```
poptogmail/
├── .venv/                    # virtual environment (uv)
├── poptogmail.py             # main script
├── pyproject.toml            # project config
├── uv.lock                   # pinned dependencies
├── requirements.txt          # dependencies (compatibility)
├── .env.example              # template for root .env (not used)
├── .gitignore
├── instances/
│   ├── example/              # template for new instances
│   │   └── .env.example
│   ├── home/                 # your first instance
│   │   ├── .env
│   │   ├── credentials.json
│   │   └── token.json
│   └── work/                 # your second instance
│       └── ...
└── scratch/                  # scratch files (gitignored, you create this)
```

## Security notes

- Passwords and tokens stored on disk in `.env` and `token.json`
- Restrict with `chmod 600` to limit exposure to the file owner
- No email content is written to disk at any point
- All network connections use TLS (POP3 on 995, Gmail API over HTTPS)
- `EXPECTED_GMAIL` check prevents misrouting: if the OAuth token resolves to an
  unexpected address, the script aborts before processing any mail
- Revoke tokens at any time at https://myaccount.google.com/permissions
