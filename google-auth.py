#!/usr/bin/env python3
"""
Google Calendar OAuth2 Authorization Helper for Omarchy Calendar Plugin.
Acquires and stores a refresh token for accessing private/shared Google Calendars via the API.
"""

import os
import sys
import json
import time
import secrets
import stat
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

STATE_DIR = os.path.expanduser("~/.local/state/omarchy")
AUTH_FILE = os.path.join(STATE_DIR, "google-auth.json")
PORT = 8088
REDIRECT_URI = f"http://127.0.0.1:{PORT}"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

MAX_API_BYTES = 5 * 1024 * 1024     # 5 MB limit for API JSON responses
MAX_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MB limit for config/auth files

auth_code = None
expected_state = None


def safe_read_bytes(stream, max_bytes=MAX_API_BYTES):
    """
    Reads binary content from stream up to max_bytes + 1.
    Raises ValueError if content exceeds max_bytes to prevent unbounded memory consumption.
    """
    chunks = []
    total = 0
    chunk_size = 64 * 1024
    while total <= max_bytes:
        chunk = stream.read(min(chunk_size, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Content size exceeded safety limit of {max_bytes} bytes")
    return b"".join(chunks)


def safe_read_text(stream, max_bytes=MAX_CONFIG_BYTES):
    """
    Reads text content from stream up to max_bytes + 1 chars.
    Raises ValueError if content exceeds max_bytes to prevent unbounded memory consumption.
    """
    chunks = []
    total = 0
    chunk_size = 64 * 1024
    while total <= max_bytes:
        chunk = stream.read(min(chunk_size, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Content size exceeded safety limit of {max_bytes} characters")
    return "".join(chunks)


def safe_load_json(file_path, max_bytes=MAX_CONFIG_BYTES):
    """
    Read JSON from one descriptor, rejecting links, non-files, foreign owners,
    and files larger than the configured limit.
    """
    dir_name = os.path.dirname(os.path.abspath(file_path))
    file_name = os.path.basename(file_path)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        dir_fd = os.open(dir_name, dir_flags)
    except FileNotFoundError:
        return None
    try:
        dir_stat = os.fstat(dir_fd)
        if not stat.S_ISDIR(dir_stat.st_mode) or dir_stat.st_uid != os.getuid():
            raise PermissionError(f"Unsafe JSON directory: {dir_name}")
        try:
            fd = os.open(file_name, file_flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"JSON path is not a regular file: {file_path}")
            if file_stat.st_uid != os.getuid():
                raise PermissionError(f"JSON file is not owned by the current user: {file_path}")
            if file_stat.st_size > max_bytes:
                raise ValueError(f"JSON file exceeds safety limit of {max_bytes} bytes")
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = safe_read_bytes(f, max_bytes=max_bytes)
            return json.loads(raw.decode("utf-8"))
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def write_secure_json(path, data, mode=0o600, max_bytes=MAX_CONFIG_BYTES):
    """Atomically replace an owned regular JSON file through its directory fd."""
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if len(payload) > max_bytes:
        raise ValueError(f"JSON output exceeds safety limit of {max_bytes} bytes")
    abs_path = os.path.abspath(path)
    dir_name = os.path.dirname(abs_path)
    file_name = os.path.basename(abs_path)
    os.makedirs(dir_name, mode=0o700, exist_ok=True)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(dir_name, dir_flags)
    tmp_name = None
    try:
        dir_stat = os.fstat(dir_fd)
        if not stat.S_ISDIR(dir_stat.st_mode) or dir_stat.st_uid != os.getuid():
            raise PermissionError(f"Unsafe JSON directory: {dir_name}")
        try:
            existing = os.stat(file_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError(f"JSON path is not a regular file: {path}")
            if existing.st_uid != os.getuid():
                raise PermissionError(f"JSON file is not owned by the current user: {path}")

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        for _ in range(128):
            candidate = f".{file_name}.tmp-{secrets.token_hex(16)}"
            try:
                fd = os.open(candidate, create_flags, mode, dir_fd=dir_fd)
                tmp_name = candidate
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("Unable to allocate an exclusive JSON temporary file")
        try:
            tmp_stat = os.fstat(fd)
            if not stat.S_ISREG(tmp_stat.st_mode) or tmp_stat.st_uid != os.getuid():
                raise PermissionError("Unsafe JSON temporary file")
            os.fchmod(fd, mode)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, file_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_name = None
        os.fsync(dir_fd)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        os.close(dir_fd)


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code, expected_state
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        received_state = params.get("state", [""])[0]
        if not expected_state or not received_state or not secrets.compare_digest(received_state, expected_state):
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <html>
            <head><title>Authentication Failed</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #181825; color: #cdd6f4;">
                <h1 style="color: #f38ba8;">Authentication Failed</h1>
                <p>Invalid or missing OAuth state parameter (CSRF validation failed).</p>
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
            html = """
            <html>
            <head><title>Omarchy Calendar Auth</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #181825; color: #cdd6f4;">
                <h1 style="color: #a6e3a1;">&#10004; Authentication Successful!</h1>
                <p>You have successfully authenticated your Google account with Omarchy.</p>
                <p>You can close this tab and return to the desktop.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            error = params.get("error", ["Unknown error"])[0]
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = f"""
            <html>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #181825; color: #cdd6f4;">
                <h1 style="color: #f38ba8;">Authentication Failed</h1>
                <p>Error: {error}</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        # Silence standard HTTP request logging
        pass


def exchange_code_for_tokens(client_id, client_secret, code):
    url = "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        return json.loads(raw.decode("utf-8"))


def main():
    global expected_state
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    existing_auth = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES) or {}
    client_id = client_id or existing_auth.get("client_id", "")
    client_secret = client_secret or existing_auth.get("client_secret", "")

    if len(sys.argv) >= 3:
        client_id = sys.argv[1].strip()
        client_secret = sys.argv[2].strip()

    if not client_id or not client_secret:
        downloads_dir = os.path.expanduser("~/Downloads")
        if os.path.exists(downloads_dir):
            for fname in os.listdir(downloads_dir):
                if fname.startswith("client_secret_") and fname.endswith(".json"):
                    try:
                        secret_data = safe_load_json(os.path.join(downloads_dir, fname), max_bytes=MAX_CONFIG_BYTES)
                        if secret_data:
                            inst = secret_data.get("installed") or secret_data.get("web", {})
                            if inst.get("client_id") and inst.get("client_secret"):
                                client_id = inst["client_id"]
                                client_secret = inst["client_secret"]
                                print(f"Found and loaded Google OAuth credentials from: ~/Downloads/{fname}")
                                break
                    except Exception:
                        pass

    if not client_id or not client_secret:
        print("=" * 60)
        print("  Omarchy Calendar - Google OAuth2 Setup")
        print("=" * 60)
        print("To connect calendars that require your Google login:")
        print("1. Go to Google Cloud Console: https://console.cloud.google.com/")
        print("2. Enable the 'Google Calendar API'")
        print("3. Under Credentials -> Create Credentials -> 'OAuth client ID'")
        print("   Application type: 'Desktop App'")
        print("=" * 60)
        client_id = input("Enter your Google OAuth Client ID: ").strip()
        client_secret = input("Enter your Google OAuth Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Error: Client ID and Client Secret are required.")
        sys.exit(1)

    expected_state = secrets.token_urlsafe(32)

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
        })
    )

    print("\nStarting local authentication server on 127.0.0.1:", PORT, "...")
    server = HTTPServer(("127.0.0.1", PORT), OAuthCallbackHandler)
    server.timeout = 600

    print("Opening browser for authorization...")
    print("If it does not open automatically, visit:")
    print(auth_url)
    print()
    webbrowser.open(auth_url)

    print("Waiting for authorization in browser (timeout: 10 minutes)...")
    while not auth_code:
        server.handle_request()

    if not auth_code:
        print("Authentication timed out or failed.")
        sys.exit(1)

    print("Authorization code received! Exchanging for tokens...")
    try:
        tokens = exchange_code_for_tokens(client_id, client_secret, auth_code)
        refresh_token = tokens.get("refresh_token") or existing_auth.get("refresh_token")

        if not refresh_token:
            print("Error: No refresh token returned. Try removing app access from Google Account and authenticating again.")
            sys.exit(1)

        auth_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "access_token": tokens.get("access_token"),
            "expires_at": int(time.time()) + tokens.get("expires_in", 3600),
            "updated_at": int(time.time()),
        }

        write_secure_json(AUTH_FILE, auth_data, mode=0o600)

        print("\n" + "=" * 60)
        print("SUCCESS! Google OAuth credentials saved to:")
        print(f"  {AUTH_FILE}")
        print("=" * 60)
        print()
        print("Tip: if this login stops working after about 7 days, your OAuth app is")
        print("still in 'Testing' publishing status. In Google Cloud Console open")
        print("Google Auth Platform -> Audience and click 'Publish app' (In production).")
        print("Refresh tokens then stop expiring and you will not need to sign in again.")

    except Exception as e:
        print("Failed to exchange tokens:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
