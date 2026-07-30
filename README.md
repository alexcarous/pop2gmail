# poptogmail

Import emails from multiple POP3 mailboxes into Gmail via the Gmail API.
Designed for migrating email from providers like mailserver, with minimal overhead
and no local attachment processing.

## How it works

The script scans `instances/` for configured accounts and processes them all
in a single run:

1. Validates each instance before importing (checks credentials, POP3 connectivity, Gmail
   auth, and `EXPECTED_GMAIL` match).
2. If an instance fails validation, the error is printed and logged, and the script skips to the next instance (preventing one broken mailbox from blocking other healthy ones).
3. Connects to the POP3 server over TLS (default port 995).
4. For each message (up to 100 messages per run to avoid POP3 session timeouts):
   - Downloads raw bytes (supports messages up to 25MB).
   - Encodes to base64url.
   - Imports into Gmail via `users.messages.import` with specified label IDs, preserving original timestamps.
   - A 1-second pause is applied between successful imports to stay within Gmail API rate limits.
   - Deletes the message from the POP3 server **only** if the Gmail import succeeded (preventing data loss).
5. If the script is interrupted (e.g. `Ctrl+C`), it gracefully finishes the current message before exiting.

No attachments are parsed, rendered, or saved to disk. TLS in transit is used for both
POP3 and Gmail API. Google's spam/malware scanning applies via `import`.

A kernel-level file lock (`poptogmail.lock` in the script's directory) prevents overlapping runs.

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

Edit `instances/home/.env`. The available variables are:

| Variable | Description | Example / Default |
| :--- | :--- | :--- |
| `POP3_HOST` | **Required**. Hostname of the POP3 server. | `pop.mailserver.com` |
| `POP3_USERS` | **Required**. Comma-separated POP3 usernames to pull mail from. | `user@domain.com,other@domain.com` |
| `POP3_PASS` | **Required**. Password for POP3 mailboxes. | `your-password` |
| `EXPECTED_GMAIL` | **Required**. The destination Gmail address. Verifies correct target match. | `your@gmail.com` |
| `POP3_PORT` | Optional. POP3 TLS port. | `995` |
| `POP3_TIMEOUT` | Optional. POP3 network timeout in seconds. | `30` |
| `GMAIL_LABELS` | Optional. Comma-separated list of label IDs to apply to imported emails. | `INBOX, UNREAD` |

Save your downloaded OAuth `credentials.json` into `instances/home/`.

### 4. First run — authorize with Google

```bash
uv run python poptogmail.py
```

Any instance without a `token.json` will print an authorization URL. Open that URL in a browser on any device, sign in to Google, and paste the resulting code back into the terminal. A `token.json` is created with a long-lived refresh token. Subsequent runs are fully unattended.

The script automatically secures `token.json` with `600` permissions (owner read/write only).

### 5. Secure credential files

Ensure your local environment configuration is also secured:

```bash
chmod 600 instances/home/.env
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
0 * * * * cd /home/alex/scripts/poptogmail && uv run python poptogmail.py > /dev/null 2>&1
```

One cron line covers all instances. No flags needed.

## Running

```bash
uv run python poptogmail.py [--dry-run]
```

### Command Line Options
* `--dry-run`: Performs a simulated sync. It connects to POP3 and authenticates, gets the count of messages, and outputs the simulated processing to the log without writing emails to Gmail or deleting them from the POP3 mailbox.

### Logs
Output is printed to the console (`stdout/stderr`) and also recorded to instance log files at `instances/<name>/<name>.log`. Each log file is capped at 1000 lines (oldest trimmed).

Example log output:

```
[2026-07-30T14:02:03Z] home: 12 processed, 0 errors
[2026-07-30T14:02:04Z] home: 5 processed, 1 errors
[2026-07-30T14:02:04Z] ERROR user@domain.com msg 47: <HttpError ...>
```

If there are no messages and no errors, nothing is written or logged.

## File structure

```
poptogmail/
├── .venv/                    # virtual environment (uv)
├── poptogmail.py             # main script
├── pyproject.toml            # project config
├── uv.lock                   # pinned dependencies
├── .gitignore
├── instances/
│   ├── example/              # template for new instances (committed)
│   │   └── .env.example
│   ├── home/                 # your first instance (gitignored)
│   │   ├── .env
│   │   ├── credentials.json
│   │   ├── token.json
│   │   └── home.log
│   └── work/                 # your second instance (gitignored)
│       └── ...
└── scratch/                  # scratch files (gitignored)
```

## Security notes

- Passwords and tokens stored on disk in each instance's `.env` and `token.json`
- Restrict with `chmod 600` to limit exposure to the file owner
- No email content is written to disk at any point
- All network connections use TLS (POP3 on 995/custom, Gmail API over HTTPS)
- `EXPECTED_GMAIL` check prevents misrouting: the script validates every instance's Gmail identity before processing any mail.
- Kernel-level locking (`poptogmail.lock` in script root) prevents concurrent runs stomping on each other.
- Revoke tokens at any time at https://myaccount.google.com/permissions
