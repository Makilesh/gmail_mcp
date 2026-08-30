#!/usr/bin/env python3
"""Gmail MCP server.

Exposes read + two-step compose/send access to a single Gmail account over
the MCP stdio transport.

Design note: there is deliberately no "send arbitrary text" tool. Composing
(`create_draft`) and sending (`send_draft`) are separate steps so a human can
open the draft in Gmail and inspect it before it leaves the mailbox.

stdout is reserved for the MCP transport. Every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import html
import json
import logging
import os



import random
import socket
import sys
import threading
import time
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any, Callable

from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ModuleNotFoundError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]

MAX_BODY_CHARS = 10_000
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1.0

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("GMAIL_MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s gmail-mcp %(message)s",
)
log = logging.getLogger("gmail-mcp")

mcp = _MCPServer("gmail")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

_service_lock = threading.Lock()
_service = None


def _credentials_path() -> str:
    path = os.environ.get("GMAIL_CREDENTIALS_PATH")
    if not path:
        raise RuntimeError(
            "GMAIL_CREDENTIALS_PATH is not set. Point it at the OAuth desktop "
            "client_secret.json downloaded from the Google Cloud Console."
        )
    
    if not os.path.isfile(path):
        raise RuntimeError(f"client_secret.json not found at {path!r}")
    return path


def _token_path() -> str:
    """Where the cached OAuth token lives.

    
    Defaults to token.json next to client_secret.json; override with
    GMAIL_TOKEN_PATH.
    """
    override = os.environ.get("GMAIL_TOKEN_PATH")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(_credentials_path())), "token.json")


def _save_token(creds: Credentials, path: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    log.info("cached OAuth token at %s", path)


def _run_consent_flow(client_secret: str) -> Credentials:
    """Interactive desktop consent flow.

    google-auth-oauthlib prints the consent URL to stdout, which would corrupt
    the MCP transport, so stdout is redirected to stderr for the duration.
    """
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    log.info("no valid cached token; starting OAuth consent flow")
    with contextlib.redirect_stdout(sys.stderr):
        creds = flow.run_local_server(
            port=0,
            open_browser=os.environ.get("GMAIL_MCP_OPEN_BROWSER", "1") != "0",
            authorization_prompt_message=(
                "Gmail MCP: open this URL in a browser to authorize access:\n\n{url}\n"
            ),
            success_message=(
                "Gmail MCP authorized. You can close this tab and return to the terminal."
            ),
        )
    return creds


def get_credentials(allow_interactive: bool = True) -> Credentials:
    """Load cached credentials, refreshing silently or re-consenting as needed."""
    client_secret = _credentials_path()
    token_file = _token_path()
    creds: Credentials | None = None

    if os.path.isfile(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("ignoring unreadable token cache %s: %s", token_file, exc)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            log.info("refreshing expired access token")
            creds.refresh(Request())
            _save_token(creds, token_file)
            return creds
        except RefreshError as exc:
            log.warning("token refresh failed (%s); re-running consent flow", exc)
            creds = None

    if not allow_interactive:
        raise RuntimeError(
            "No usable cached token and interactive consent is disabled. "
            "Run `python server.py --selftest` once to authorize."
        )

    creds = _run_consent_flow(client_secret)
    _save_token(creds, token_file)
    return creds


def get_service(allow_interactive: bool = True):
    """Build (and memoize) the Gmail API client."""
    global _service
    with _service_lock:
        if _service is None:
            creds = get_credentials(allow_interactive=allow_interactive)
            _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return _service


# --------------------------------------------------------------------------
# Transport helpers: retries + structured errors
# --------------------------------------------------------------------------

_RETRYABLE_NETWORK_ERRORS = (
    TimeoutError,
    ConnectionError,
    socket.timeout,
    socket.gaierror,
    TransportError,
)


def _status_of(exc: HttpError) -> int | None:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None)


def _is_retryable_status(status: int | None) -> bool:
    return status == 429 or (status is not None and 500 <= status < 600)


def _http_error_message(exc: HttpError) -> str:
    """Pull the human-readable reason out of a Gmail API error payload."""
    try:
        payload = json.loads(exc.content.decode("utf-8", "replace"))
        message = payload.get("error", {}).get("message")
        if message:
            return message
    except Exception:  # noqa: BLE001 - error bodies are not always JSON
        pass
    return str(exc)


def execute(request_factory: Callable[[], Any], *, what: str) -> Any:
    """Run a Gmail API request with exponential backoff on 429/5xx.

    `request_factory` is a callable so every attempt gets a fresh request.
    """
    delay = BASE_BACKOFF_SECONDS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return request_factory().execute()
        except HttpError as exc:
            status = _status_of(exc)
            if _is_retryable_status(status) and attempt < MAX_ATTEMPTS:
                wait = delay + random.uniform(0, 0.25 * delay)
                log.warning(
                    "%s failed with HTTP %s (attempt %d/%d); retrying in %.1fs",
                    what, status, attempt, MAX_ATTEMPTS, wait,
                )
                time.sleep(wait)
                delay *= 2
                continue
            raise
        except _RETRYABLE_NETWORK_ERRORS as exc:
            if attempt < MAX_ATTEMPTS:
                wait = delay + random.uniform(0, 0.25 * delay)
                log.warning(
                    "%s hit a network error (%s) on attempt %d/%d; retrying in %.1fs",
                    what, exc, attempt, MAX_ATTEMPTS, wait,
                )
                time.sleep(wait)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"{what}: exhausted {MAX_ATTEMPTS} attempts")  # unreachable


def error_result(exc: Exception, *, what: str) -> dict[str, Any]:
    """Turn any exception into {"error": ..., "retryable": bool}."""
    if isinstance(exc, HttpError):
        status = _status_of(exc)
        result: dict[str, Any] = {
            "error": f"{what} failed: {_http_error_message(exc)}",
            "retryable": _is_retryable_status(status),
        }
        if status is not None:
            result["status"] = status
        log.error("%s failed with HTTP %s: %s", what, status, result["error"])
        return result

    retryable = isinstance(exc, _RETRYABLE_NETWORK_ERRORS)
    if isinstance(exc, (RefreshError, GoogleAuthError)) and not retryable:
        message = (
            f"{what} failed: authentication problem: {exc}. "
            "Delete token.json and re-run `python server.py --selftest` to re-authorize."
        )
    else:
        message = f"{what} failed: {exc}"
    log.error("%s", message, exc_info=log.isEnabledFor(logging.DEBUG))
    return {"error": message, "retryable": retryable}


# --------------------------------------------------------------------------
# Message parsing
# --------------------------------------------------------------------------


class _HTMLToText(HTMLParser):
    """Minimal HTML -> text fallback for messages with no text/plain part."""

    _BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "ul", "ol", "blockquote", "section", "article", "header", "footer",
    }
    _SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        out: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def _strip_html(markup: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed HTML must not kill the tool
        log.warning("HTML parse failed (%s); falling back to unescaped markup", exc)
        return html.unescape(markup)
    return parser.text()


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", "replace")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}


def _collect_bodies(part: dict[str, Any], out: dict[str, list[str]]) -> None:
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if not filename and body.get("data"):
        if mime == "text/plain":
            out["plain"].append(_decode(body["data"]))
        elif mime == "text/html":
            out["html"].append(_decode(body["data"]))
    for sub in part.get("parts") or []:
        _collect_bodies(sub, out)


def _extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to stripped text/html."""
    found: dict[str, list[str]] = {"plain": [], "html": []}
    _collect_bodies(payload, found)
    if found["plain"]:
        return "\n".join(found["plain"]).strip()
    if found["html"]:
        return _strip_html("\n".join(found["html"])).strip()
    return ""


def _summarize(message: dict[str, Any]) -> dict[str, Any]:
    hdrs = _headers(message.get("payload") or {})
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": hdrs.get("from", ""),
        "subject": hdrs.get("subject", ""),
        "date": hdrs.get("date", ""),
        "snippet": html.unescape(message.get("snippet") or ""),
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
def list_messages(query: str = "is:unread", max_results: int = 10) -> Any:
    """Search the mailbox and return message headers (no bodies).

    `query` takes raw Gmail search syntax, exactly what you would type into the
    Gmail search box: `from:alice@example.com`, `is:unread newer_than:2d`,
    `has:attachment subject:invoice`, `label:work -in:chats`, and so on.

    Returns a list of {id, thread_id, from, subject, date, snippet}. Bodies are
    never included - call `get_message` with an id to read one.

    On failure returns {"error": str, "retryable": bool} instead of raising.
    """
    try:
        max_results = max(1, min(int(max_results), 100))
        service = get_service()
        listing = execute(
            lambda: service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ),
            what="messages.list",
        )
        results = []
        for ref in listing.get("messages", []) or []:
            message = execute(
                lambda mid=ref["id"]: service.users().messages().get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ),
                what="messages.get",
            )
            results.append(_summarize(message))
        return results
    except Exception as exc:  # noqa: BLE001 - tools return errors, never raise
        return error_result(exc, what="list_messages")


@mcp.tool()
def get_message(message_id: str) -> Any:
    """Fetch one message in full, including its body.

    Returns {from, to, cc, subject, date, body_text, thread_id}. The body is
    the text/plain part when present, otherwise the text/html part with tags
    stripped. Bodies longer than 10,000 characters are cut off and the result
    carries "truncated": true plus the full "body_length".

    Pass the returned `thread_id` to `create_draft` when replying.

    On failure returns {"error": str, "retryable": bool} instead of raising.
    """
    try:
        service = get_service()
        message = execute(
            lambda: service.users().messages().get(
                userId="me", id=message_id, format="full"
            ),
            what="messages.get",
        )
        payload = message.get("payload") or {}
        hdrs = _headers(payload)
        body = _extract_body(payload)
        result: dict[str, Any] = {
            "from": hdrs.get("from", ""),
            "to": hdrs.get("to", ""),
            "cc": hdrs.get("cc", ""),
            "subject": hdrs.get("subject", ""),
            "date": hdrs.get("date", ""),
            "body_text": body,
            "thread_id": message.get("threadId"),
            "truncated": False,
        }
        if len(body) > MAX_BODY_CHARS:
            result["body_text"] = body[:MAX_BODY_CHARS]
            result["truncated"] = True
            result["body_length"] = len(body)
        return result
    except Exception as exc:  # noqa: BLE001
        return error_result(exc, what="get_message")


def _reply_headers(service, thread_id: str) -> dict[str, str]:
    """Best-effort In-Reply-To/References/Subject so a reply threads properly."""
    try:
        thread = execute(
            lambda: service.users().threads().get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References", "Subject"],
            ),
            what="threads.get",
        )
    except Exception as exc:  # noqa: BLE001 - threading is a nicety, not a requirement
        log.warning("could not read thread %s for reply headers: %s", thread_id, exc)
        return {}

    messages = thread.get("messages") or []
    if not messages:
        return {}
    hdrs = _headers(messages[-1].get("payload") or {})
    parent_id = hdrs.get("message-id", "")
    references = " ".join(x for x in (hdrs.get("references", ""), parent_id) if x).strip()
    return {
        "in_reply_to": parent_id,
        "references": references,
        "subject": hdrs.get("subject", ""),
    }


@mcp.tool()
def create_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] = [],
    thread_id: str | None = None,
) -> Any:
    """Compose a Gmail draft. This does NOT send anything.

    Sending mail is a deliberate two-step flow: `create_draft` saves the draft
    into the user's Gmail account, where a human can open it and check the
    recipients and the wording, and `send_draft` is the only tool that actually
    puts it on the wire. There is no tool that sends arbitrary text in one
    step. After creating a draft, report the draft_id and let the user decide
    whether to send it.

    `to` and `cc` are lists of addresses ("Name <a@b.com>" is fine). Pass
    `thread_id` (from `list_messages` or `get_message`) to file the draft as a
    reply in that thread; the subject is then prefixed with "Re: " unless it
    already starts with one.

    Returns {draft_id, message_id, thread_id, subject}. On failure returns
    {"error": str, "retryable": bool} instead of raising.
    """
    try:
        recipients = [addr for addr in (to or []) if addr and addr.strip()]
        if not recipients:
            return {
                "error": "create_draft failed: `to` must contain at least one address",
                "retryable": False,
            }
        cc_list = [addr for addr in (cc or []) if addr and addr.strip()]

        service = get_service()
        final_subject = subject or ""
        reply = _reply_headers(service, thread_id) if thread_id else {}
        if thread_id:
            if not final_subject and reply.get("subject"):
                final_subject = reply["subject"]
            if not final_subject.strip().lower().startswith("re:"):
                final_subject = f"Re: {final_subject}".rstrip()

        mime = EmailMessage()
        mime["To"] = ", ".join(recipients)
        if cc_list:
            mime["Cc"] = ", ".join(cc_list)
        mime["Subject"] = final_subject
        if reply.get("in_reply_to"):
            mime["In-Reply-To"] = reply["in_reply_to"]
        if reply.get("references"):
            mime["References"] = reply["references"]
        mime.set_content(body or "")

        draft_body: dict[str, Any] = {
            "message": {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        }
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = execute(
            lambda: service.users().drafts().create(userId="me", body=draft_body),
            what="drafts.create",
        )
        return {
            "draft_id": draft.get("id"),
            "message_id": (draft.get("message") or {}).get("id"),
            "thread_id": (draft.get("message") or {}).get("threadId"),
            "subject": final_subject,
            "note": "Draft saved but NOT sent. Call send_draft with this draft_id to send it.",
        }
    except Exception as exc:  # noqa: BLE001
        return error_result(exc, what="create_draft")


@mcp.tool()
def send_draft(draft_id: str) -> Any:
    """Send an existing draft. This is the only tool that sends mail.

    Takes the `draft_id` returned by `create_draft`. The draft goes out exactly
    as it currently stands in Gmail, so any edits the user made there are
    included. Sending is irreversible, so confirm with the user first.

    Returns {message_id, thread_id, label_ids}. On failure returns
    {"error": str, "retryable": bool} instead of raising.
    """
    try:
        if not draft_id or not draft_id.strip():
            return {"error": "send_draft failed: draft_id is required", "retryable": False}
        service = get_service()
        sent = execute(
            lambda: service.users().drafts().send(userId="me", body={"id": draft_id}),
            what="drafts.send",
        )
        log.info("sent draft %s as message %s", draft_id, sent.get("id"))
        return {
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "label_ids": sent.get("labelIds", []),
            "sent": True,
        }
    except Exception as exc:  # noqa: BLE001
        return error_result(exc, what="send_draft")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def selftest() -> int:
    """Authenticate, list 3 messages, exit. Verifies setup without an MCP client."""
    print("gmail-mcp selftest: authenticating...", file=sys.stderr)
    try:
        service = get_service()
        profile = execute(
            lambda: service.users().getProfile(userId="me"), what="users.getProfile"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {error_result(exc, what='selftest')['error']}", file=sys.stderr)
        return 1

    print(
        f"authenticated as {profile.get('emailAddress')} "
        f"({profile.get('messagesTotal')} messages total)",
        file=sys.stderr,
    )
    print(f"token cached at {_token_path()}", file=sys.stderr)

    result = list_messages(query="in:inbox", max_results=3)
    if isinstance(result, dict) and "error" in result:
        print(f"FAILED: {result['error']}", file=sys.stderr)
        return 1

    print(f"listed {len(result)} message(s):", file=sys.stderr)
    for item in result:
        print(json.dumps(item, ensure_ascii=False), file=sys.stderr)
    print("selftest OK", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Gmail MCP server (stdio transport)")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="authenticate, list 3 messages, and exit (no MCP client needed)",
    )
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    log.info("starting Gmail MCP server on stdio")
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
