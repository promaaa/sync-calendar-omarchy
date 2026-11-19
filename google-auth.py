#!/usr/bin/env python3
"""
Google Calendar OAuth2 Authorization Helper for Omarchy Calendar Plugin.
Acquires and stores a refresh token for accessing private/shared Google Calendars via the API.
"""

import base64
import hashlib
import importlib.util
import os
import sys
import json
from html import escape as html_escape
import time
import secrets
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

STATE_DIR = os.path.expanduser("~/.local/state/omarchy")
AUTH_FILE = os.path.join(STATE_DIR, "google-auth.json")
PORT = 8088
REDIRECT_URI = f"http://127.0.0.1:{PORT}"
# Read and write events, and list the account's calendars so setup can offer them.
SCOPE = " ".join([
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
])

MAX_API_BYTES = 5 * 1024 * 1024     # 5 MB limit for API JSON responses
MAX_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MB limit for config/auth files

auth_code = None
auth_failed = False
expected_state = None


def _load_backend():
    """fetch-events.py holds the safe file helpers and the keyring code."""
    spec = importlib.util.spec_from_file_location(
        "chronica_backend", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch-events.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend = _load_backend()
safe_read_bytes = backend.safe_read_bytes
safe_load_json = backend.safe_load_json
write_secure_json = backend.write_secure_json
# Display strings follow the backend's UI language; English stays the fallback.
_t = backend._t


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    # Any local account can connect to the port: one that sends nothing must
    # not hold the single-threaded server past the sign-in deadline.
    timeout = 10

    def do_GET(self):
        global auth_code, auth_failed, expected_state
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        received_state = params.get("state", [""])[0]
        if not expected_state or not received_state or not secrets.compare_digest(received_state, expected_state):
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            failed_title = _t("auth.pageFailedTitle", "Authentication Failed")
            html = f"""
            <html>
            <head><title>{failed_title}</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #181825; color: #cdd6f4;">
                <h1 style="color: #f38ba8;">{failed_title}</h1>
                <p>{_t("auth.pageBadState", "Invalid or missing OAuth state parameter (CSRF validation failed).")}</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
            return

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = f"""
            <html>
            <head><title>{_t("auth.pageSuccessTitle", "Omarchy Calendar Auth")}</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #181825; color: #cdd6f4;">
                <h1 style="color: #a6e3a1;">{_t("auth.pageSuccessHeading", "✔ Authentication Successful!")}</h1>
                <p>{_t("auth.pageSuccessBody", "You have successfully authenticated your Google account with Omarchy.")}</p>
                <p>{_t("auth.pageSuccessClose", "You can close this tab and return to the desktop.")}</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            # The state matched, so this is Google's answer: stop waiting.
            auth_failed = True
            error = html_escape(params.get("error", [_t("auth.unknownError", "Unknown error")])[0])
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = f"""
            <html>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #181825; color: #cdd6f4;">
                <h1 style="color: #f38ba8;">{_t("auth.pageFailedTitle", "Authentication Failed")}</h1>
                <p>{_t("auth.pageError", "Error: {error}", error=error)}</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        # Silence standard HTTP request logging
        pass


def pkce_pair():
    """
    (code_verifier, code_challenge) for PKCE (RFC 7636, S256). The sign-in URL,
    with its state, reaches the browser through a command line any local
    account can read; only the verifier, which never leaves this process,
    can turn a code into tokens, so a code someone else injects is useless.
    """
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def exchange_code_for_tokens(client_id, client_secret, code, code_verifier):
    url = "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        return json.loads(raw.decode("utf-8"))


def find_downloaded_client():
    """(client_id, client_secret, filename) from a client_secret_*.json in ~/Downloads, or None."""
    downloads_dir = os.path.expanduser("~/Downloads")
    if not os.path.isdir(downloads_dir):
        return None
    names = [f for f in os.listdir(downloads_dir) if f.startswith("client_secret_") and f.endswith(".json")]
    # Newest first: a fresh download beats a client from an older attempt.
    names.sort(key=lambda f: os.path.getmtime(os.path.join(downloads_dir, f)), reverse=True)
    for fname in names:
        try:
            secret_data = safe_load_json(os.path.join(downloads_dir, fname), max_bytes=MAX_CONFIG_BYTES) or {}
        except Exception:
            continue
        inst = secret_data.get("installed") or secret_data.get("web", {})
        if inst.get("client_id") and inst.get("client_secret"):
            return inst["client_id"], inst["client_secret"], fname
    return None


def load_saved_auth():
    return backend.reveal_secrets(safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES) or {},
                                  backend.GOOGLE_SECRET_FIELDS, secret_id="google")


def authorize(client_id, client_secret):
    """Run the browser sign-in and save the tokens. Raises RuntimeError on failure."""
    global auth_code, auth_failed, expected_state
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    existing_auth = load_saved_auth()
    auth_code = None
    auth_failed = False
    expected_state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = pkce_pair()

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": expected_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
    )

    server = HTTPServer(("127.0.0.1", PORT), OAuthCallbackHandler)
    server.timeout = 600
    try:
        print(_t("auth.browserOpening", "Opening your browser to sign in to Google."))
        print(_t("auth.ifNothingOpens", "If nothing opens, visit:"))
        print(auth_url)
        print()
        print(_t("auth.unverifiedApp", 'If Google says it "hasn\'t verified this app", that is your own app:'))
        print(_t("auth.advancedSteps", "click Advanced, then Go to <your app name>, then Continue."))
        webbrowser.open(auth_url)
        print(_t("auth.waitingBrowser", "Waiting for you to finish in the browser (10 minutes max)..."))
        deadline = time.monotonic() + server.timeout
        while not auth_code and not auth_failed and time.monotonic() < deadline:
            server.timeout = max(1, deadline - time.monotonic())
            server.handle_request()
    finally:
        server.server_close()

    if not auth_code:
        raise RuntimeError(_t("auth.timeout", "Sign-in timed out or was cancelled."))

    try:
        tokens = exchange_code_for_tokens(client_id, client_secret, auth_code, code_verifier)
    except Exception as e:
        raise RuntimeError(_t("auth.tokenExchangeFailed", "Failed to exchange tokens: {error}", error=e)) from e
    refresh_token = tokens.get("refresh_token") or existing_auth.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(_t("auth.noRefreshToken", "No refresh token returned. Remove the app's access in your Google Account and try again."))

    auth_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "access_token": tokens.get("access_token"),
        "expires_at": int(time.time()) + tokens.get("expires_in", 3600),
        "updated_at": int(time.time()),
    }
    backend.stash_secrets(auth_data, backend.GOOGLE_SECRET_FIELDS, secret_id="google")
    write_secure_json(AUTH_FILE, auth_data, mode=0o600)
    return auth_data


def main():
    existing_auth = load_saved_auth()
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "") or existing_auth.get("client_id", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "") or existing_auth.get("client_secret", "")

    if len(sys.argv) >= 3:
        client_id = sys.argv[1].strip()
        client_secret = sys.argv[2].strip()

    if not client_id or not client_secret:
        found = find_downloaded_client()
        if found:
            client_id, client_secret, fname = found
            print(_t("auth.foundCredentials", "Found and loaded Google OAuth credentials from: ~/Downloads/{filename}", filename=fname))

    if not client_id or not client_secret:
        print(_t("auth.noClientSaved", "No Google client saved yet. Run the guided setup instead:"))
        print(f"  python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'setup.py')}")
        sys.exit(1)

    try:
        authorize(client_id, client_secret)
    except RuntimeError as e:
        print(_t("auth.errorPrefix", "Error: {message}", message=e))
        sys.exit(1)
    print("\n" + _t("auth.successHeadline", "SUCCESS! Google OAuth credentials saved to:"))
    print(f"  {AUTH_FILE}")
    print()
    print(_t("auth.tipLine1", "Tip: if this login stops working after about 7 days, your OAuth app is"))
    print(_t("auth.tipLine2", "still in 'Testing' publishing status. In Google Cloud Console open"))
    print(_t("auth.tipLine3", "Google Auth Platform -> Audience and click 'Publish app' (In production)."))
    print(_t("auth.tipLine4", "Refresh tokens then stop expiring and you will not need to sign in again."))


if __name__ == "__main__":
    main()
