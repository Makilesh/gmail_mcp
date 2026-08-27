# Gmail MCP Server

An MCP server (stdio transport) that gives an MCP client read access to a Gmail
account plus a **two-step** compose/send flow.

| Tool | What it does |
| --- | --- |
| `list_messages(query="is:unread", max_results=10)` | Gmail search; returns `id, thread_id, from, subject, date, snippet`. No bodies. |
| `get_message(message_id)` | Full message: `from, to, cc, subject, date, body_text, thread_id`. Bodies over 10k chars are truncated with `"truncated": true`. |
| `create_draft(to, subject, body, cc=[], thread_id=None)` | Saves a Gmail draft. Returns `draft_id, message_id`. **Does not send.** |
| `send_draft(draft_id)` | Sends an existing draft. The only tool that sends mail. |

## Why there is no "send email" tool

Composing and sending are deliberately separate. `create_draft` puts the message
in your Gmail drafts, where you can open it, check the recipients and the
wording, and edit it. Nothing leaves the mailbox until `send_draft` is called
with that draft's id. There is no tool that takes arbitrary text and sends it in
one step — that gap is the safety property, so don't add one without meaning to.

Both tool descriptions spell this out so the model on the other end understands
the handoff and asks before sending.

## Setup

### 1. Enable the Gmail API

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   a project (or pick an existing one).
2. **APIs & Services → Library → Gmail API → Enable**.
3. **APIs & Services → OAuth consent screen**: choose **External** (unless you
   are on Workspace and want Internal), fill in the app name and your email.
   While the app is in *Testing*, add your own Gmail address under
   **Test users** — otherwise consent fails with `access_denied`.
4. Add these scopes (or just accept them at the consent screen on first run):
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
   - `https://www.googleapis.com/auth/gmail.send`

### 2. Download `client_secret.json`

**APIs & Services → Credentials → Create Credentials → OAuth client ID →
Application type: Desktop app**. Download the JSON. Keep it out of version
control — anywhere outside the repo is safest, e.g.
`C:\Users\you\.gmail-mcp\client_secret.json`.

### 3. Install

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS/Linux the interpreter is `.venv/bin/python` instead.

The server works with both the 1.x and 2.x MCP SDK — it imports `MCPServer`
(SDK 2.x) and falls back to `FastMCP` (SDK 1.x). Verified against `mcp` 2.1.1.

### 4. First run / consent

Point `GMAIL_CREDENTIALS_PATH` at the file you downloaded and run the selftest:

```bash
set GMAIL_CREDENTIALS_PATH=C:\Users\you\.gmail-mcp\client_secret.json
```

```bash
.venv\Scripts\python.exe server.py --selftest
```

(PowerShell: `$env:GMAIL_CREDENTIALS_PATH = "..."`. bash: `export GMAIL_...`.)

The server starts a loopback listener on a random port, opens your browser, and
**prints the consent URL to stderr** — stdout is reserved for the MCP transport
and must never carry anything else. Approve the three scopes; Google will warn
that the app is unverified, which is expected for a personal desktop client
(**Advanced → Go to … (unsafe)**).

Set `GMAIL_MCP_OPEN_BROWSER=0` to suppress the browser launch and copy the URL
from stderr by hand (useful over SSH).

The selftest then prints the authorized address and three inbox messages:

```
gmail-mcp selftest: authenticating...
authenticated as you@gmail.com (48213 messages total)
token cached at C:\Users\you\.gmail-mcp\token.json
listed 3 message(s):
{"id": "1932...", "thread_id": "1932...", "from": "...", ...}
selftest OK
```

Exit code is 0 on success, 1 on failure — usable as a health check.

### 5. Token caching

The refresh token is written to `token.json` next to `client_secret.json`
(override with `GMAIL_TOKEN_PATH`). Expired access tokens refresh silently on
the next call; no browser is involved again unless the refresh token is revoked
or the file is deleted. **`token.json` grants access to your mailbox — treat it
like a password.**

## Registering with Claude Desktop

Add to `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "gmail": {
      "command": "D:\\GEN AI\\gmail_mcp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\GEN AI\\gmail_mcp\\server.py"],
      "env": {
        "GMAIL_CREDENTIALS_PATH": "C:\\Users\\you\\.gmail-mcp\\client_secret.json"
      }
    }
  }
}
```

macOS/Linux equivalent:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "/path/to/gmail_mcp/.venv/bin/python",
      "args": ["/path/to/gmail_mcp/server.py"],
      "env": {
        "GMAIL_CREDENTIALS_PATH": "/Users/you/.gmail-mcp/client_secret.json"
      }
    }
  }
}
```

Use absolute paths for both the interpreter and the script, and restart Claude
Desktop afterwards. Run `--selftest` **before** registering: Claude Desktop
gives the consent flow nowhere to show itself, and the server would just hang
waiting on a browser that never opens.

## Behavior notes

- **Errors never raise.** Every tool returns
  `{"error": "...", "retryable": true|false}` on failure, with the HTTP
  `status` included when there is one. `retryable` is true for 429, 5xx, and
  network faults.
- **Retries.** 429 and 5xx responses are retried up to 3 attempts total with
  exponential backoff (~1s, ~2s) plus jitter.
- **Logging.** All logs and diagnostics go to stderr. Set
  `GMAIL_MCP_LOG_LEVEL=DEBUG` for verbose output including tracebacks.
- **Bodies.** `get_message` prefers `text/plain` and falls back to `text/html`
  with tags stripped (stdlib `HTMLParser`, no extra dependency). Attachments are
  ignored.
- **Replies.** When `create_draft` gets a `thread_id`, it reads the thread's
  last message and sets `In-Reply-To`/`References` so the reply threads
  correctly in every mail client, not just Gmail.

## Environment variables

| Variable | Required | Meaning |
| --- | --- | --- |
| `GMAIL_CREDENTIALS_PATH` | yes | Path to the OAuth desktop `client_secret.json`. |
| `GMAIL_TOKEN_PATH` | no | Where to cache `token.json`. Default: next to the client secret. |
| `GMAIL_MCP_OPEN_BROWSER` | no | `0` to print the consent URL without launching a browser. |
| `GMAIL_MCP_LOG_LEVEL` | no | Python log level, default `INFO`. |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `GMAIL_CREDENTIALS_PATH is not set` | Set it in your shell, or in the `env` block of `claude_desktop_config.json`. |
| `access_denied` at consent | Add your Gmail address as a **Test user** on the OAuth consent screen. |
| `insufficient authentication scopes` | Scopes changed after the token was minted — delete `token.json` and re-run `--selftest`. |
| Server hangs in Claude Desktop on first use | Consent was never completed. Run `--selftest` from a terminal first. |
| Client reports invalid JSON from the server | Something wrote to stdout. Keep every `print` on stderr. |
