# poptogmail

Import emails from multiple POP3 mailboxes into Gmail via the Gmail API.
Designed for migrating email from providers like mailserver, with minimal overhead
and no local attachment processing.

## How it works

The script scans `instances/` for configured accounts and processes them all
in a single run:

1. Validates every instance before touching mail (POP3 connectivity, Gmail
   auth, `EXPECTED_GMAIL` match)
2. For each instance: connects to the POP3 server over TLS (port 995)
3. For each message: downloads raw bytes → base64url-encodes → sends to
   `users.messages.import` with `internalDateSource: dateHeader` to preserve
   original timestamps
4. Deletes the message from the POP3 server
5. Messages that fail to import are still deleted (force-delete mode)

No attachments are parsed, rendered, or saved to disk. TLS in transit for both
POP3 and Gmail API. Google's spam/malware scanning applies via `import`.

A lock file at `/tmp/poptogmail.lock` prevents overlapping runs.

## Requirements

- Python 3.9+
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the Gmail API enabled
- One or more POP3 mailboxes

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Google Cloud Console — create OAuth credentials

Do this for each Gmail account you want to import into.

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

Each email account is an instance — a directory under `instances/` with its
own `.env` and `credentials.json`:

```bash
cp -r instances/example instances/home
```

Edit `instances/home/.env` with your POP3 host, username, password, and
**`EXPECTED_GMAIL`** (the Gmail address you're importing into). The script
verifies this before processing any mail, so the wrong credentials can never
import into the wrong inbox.

Save your OAuth `credentials.json` into `instances/home/`.

### 4. First run — authorize with Google

```bash
uv run python poptogmail.py
```

The script validates and processes all instances. Any instance without a
`token.json` will open a browser for Google sign-in. After authorizing, a
`token.json` is created in that instance's directory with a long-lived
refresh token. Subsequent runs are fully unattended.

### 5. Secure credential files

```bash
chmod -R 600 instances/home/.env instances/home/token.json
```

### 6. Additional instances

Set up additional accounts the same way:

```bash
cp -r instances/example instances/work
# edit instances/work/.env, add credentials.json
uv run python poptogmail.py
```

### 7. Schedule with cron (optional)

```
0 * * * * cd /home/alex/scripts/poptogmail && uv run python poptogmail.py
```

One cron line covers all instances. No flags needed.

## Running

```bash
uv run python poptogmail.py
```

Output when mail is processed:

```
home: 12 imported, 0 errors
work: 5 imported, 1 errors
```

Output when no mail is pending:

```
no new mail
```

If any instance fails validation (wrong Gmail account, POP3 unreachable, etc.),
the script exits before processing any mail for any account. Errors during
import are logged to stderr and the script continues to the next instance.

## File structure

```
poptogmail/
├── .venv/                    # virtual environment (uv)
├── poptogmail.py             # main script
├── pyproject.toml            # project config
├── uv.lock                   # pinned dependencies
├── requirements.txt          # dependencies (compatibility)
├── .env.example              # template for root .env (documentation only)
├── .gitignore
├── instances/
│   ├── example/              # template for new instances (committed)
│   │   └── .env.example
│   ├── home/                 # your first instance (gitignored)
│   │   ├── .env
│   │   ├── credentials.json
│   │   └── token.json
│   └── work/                 # your second instance (gitignored)
│       └── ...
└── scratch/                  # scratch files (gitignored)
```

## Security notes

- Passwords and tokens stored on disk in each instance's `.env` and `token.json`
- Restrict with `chmod 600` to limit exposure to the file owner
- No email content is written to disk at any point
- All network connections use TLS (POP3 on 995, Gmail API over HTTPS)
- `EXPECTED_GMAIL` check prevents misrouting: the script validates every
  instance's Gmail identity before processing any mail, and aborts all instances
  if any validation fails
- A lock file at `/tmp/poptogmail.lock` prevents concurrent runs stomping on
  each other
- Revoke tokens at any time at https://myaccount.google.com/permissions
