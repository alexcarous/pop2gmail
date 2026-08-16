# pop2gmail

Imports emails from POP3 mailboxes into Gmail using the Gmail API. Designed as a lightweight, self-hosted alternative following Google's discontinuation of POP3 fetch in October 2024.

Pull mail from any standard POP3 server and import it directly into Gmail with original timestamps, custom labels, and spam/malware scanning intact.

## Features

- **Multi-account setup**: Define separate accounts ("instances") under `instances/` and process them in a single run.
- **Safe deletion**: POP3 messages are only deleted after a successful Gmail import.
- **Fail-safe isolation**: Validation checks ensure broken instances or credential errors don't stop other accounts from processing.
- **No local storage**: Messages are fetched over TLS, converted in-memory, and pushed directly to the Gmail API.
- **Overlapping execution protection**: Uses a file lock (`pop2gmail.lock`) to prevent concurrent cron executions.

## Requirements

- Python 3.9+
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the Gmail API enabled

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure Google Cloud OAuth Credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create or select a project and enable the **Gmail API**.
3. Configure the **OAuth consent screen** (External user type, add your email under Test Users).
4. Add the following scopes under **Data Access**:
   - `https://www.googleapis.com/auth/gmail.insert`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `openid`
5. Create an **OAuth client ID** (Application type: *Desktop app*) and download the JSON file.

### 3. Create an Account Instance

Instances live inside `instances/<name>` with their own `.env` and `credentials.json`.

```bash
cp -r instances/example instances/home
```

Copy your downloaded OAuth client JSON file to `instances/home/credentials.json`, then configure `instances/home/.env`:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `POP3_HOST` | **Required**. Hostname of the POP3 server. | `pop.mailserver.com` |
| `POP3_USERS` | **Required**. Comma-separated POP3 usernames to pull mail from. | `user@domain.com` |
| `POP3_PASS` | **Required**. Password for the POP3 mailbox. | `your-password` |
| `EXPECTED_GMAIL` | **Required**. Expected Gmail address (verifies target identity). | `your@gmail.com` |
| `POP3_PORT` | POP3 TLS port. | `995` |
| `POP3_TIMEOUT` | Network timeout in seconds. | `30` |
| `GMAIL_LABELS` | Comma-separated list of label IDs to apply in Gmail. | `INBOX, UNREAD, IMPORTED` |
| `SUBJECT_PREFIX` | Optional prefix added to the subject line. | |

### 4. Authenticate

Run the initial OAuth handshake for your instance:

```bash
uv run python pop2gmail.py --auth home
```

Follow the terminal prompt: open the provided link, authenticate, and paste the final redirect URL back into the terminal. This generates a `token.json` file.

Set safe file permissions on sensitive files:

```bash
chmod 600 instances/home/.env instances/home/token.json
```

### 5. Automation (Cron)

To process all instances automatically, add a cron entry:

```cron
0 * * * * cd /path/to/pop2gmail && uv run python pop2gmail.py > /dev/null 2>&1
```

## Usage

```bash
# Sync all configured instances
uv run python pop2gmail.py

# Perform a dry run without importing or deleting messages
uv run python pop2gmail.py --dry-run

# Re-authenticate a specific instance
uv run python pop2gmail.py --auth home
```

### Logs

Run output is logged to stdout and saved per-instance at `instances/<name>/<name>.log` (capped at 1,000 lines).

## Project Structure

```
pop2gmail/
├── pop2gmail.py             # Main runner script
├── pyproject.toml            # Project dependencies and setup
├── instances/
│   ├── example/              # Configuration template
│   │   └── .env.example
│   └── home/                 # Individual account instance
│       ├── .env
│       ├── credentials.json
│       ├── token.json
│       └── home.log
```

## Security

- Credentials (`.env`, `token.json`) reside locally inside instance folders. Ensure `chmod 600` is set.
- Scopes are restricted strictly to `gmail.insert` and `userinfo.email`.
- `EXPECTED_GMAIL` verification prevents accidentally pushing emails to the wrong target account.

