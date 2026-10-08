#!/usr/bin/env python3
"""
Lightweight iCalendar (.ics / webcal) fetcher & parser for Omarchy Calendar Plugin.
Fetches configured calendars from ~/.config/omarchy/calendars.json
and writes parsed events to ~/.local/state/omarchy/calendar-events.json
"""

import os
import sys
import json
import re
import base64
import hashlib
import secrets
import stat
import time
import calendar
import shutil
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, time as dt_time, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

CONFIG_PATH = os.path.expanduser("~/.config/omarchy/calendars.json")
STATE_DIR = os.path.expanduser("~/.local/state/omarchy")
OUTPUT_PATH = os.path.join(STATE_DIR, "calendar-events.json")
TRANSLATION_CACHE_PATH = os.path.join(STATE_DIR, "translation-cache.json")
LOCAL_EVENTS_PATH = os.path.join(STATE_DIR, "local-events.json")

# Standard browser user-agent to ensure compatibility with calendar providers (Apple iCloud, Proton, Google, Outlook, Nextcloud, etc.)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 (OmarchyCalendar/1.0)"

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

_translation_cache = {}

MAX_ICAL_BYTES = 10 * 1024 * 1024   # 10 MB limit for calendar .ics content
MAX_API_BYTES = 5 * 1024 * 1024     # 5 MB limit for API JSON responses
MAX_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MB limit for local config files
MAX_OUTPUT_JSON_BYTES = 25 * 1024 * 1024  # 25 MB limit for generated event state
MAX_CACHE_BYTES = 2 * MAX_ICAL_BYTES  # one cached feed, JSON-escaped
MAX_RECURRENCE_ITERATIONS = 2000    # CPU-work ceiling for expanding recurrence rules
MAX_EXPANDED_INSTANCES = 500        # Maximum instances generated per recurring/multiday event


def safe_read_bytes(stream, max_bytes=MAX_ICAL_BYTES):
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


def safe_read_text(stream, max_bytes=MAX_ICAL_BYTES):
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
    dir_name = os.path.dirname(os.path.realpath(file_path))
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
    dir_name = os.path.dirname(os.path.realpath(path))
    file_name = os.path.basename(path)
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


def load_translation_cache():
    global _translation_cache
    try:
        data = safe_load_json(TRANSLATION_CACHE_PATH, max_bytes=MAX_CONFIG_BYTES)
        _translation_cache = data if isinstance(data, dict) else {}
    except Exception:
        _translation_cache = {}


def save_translation_cache():
    try:
        write_secure_json(TRANSLATION_CACHE_PATH, _translation_cache, mode=0o600)
    except Exception:
        pass


def has_korean(text):
    if not text:
        return False
    return any(
        (0xAC00 <= ord(c) <= 0xD7AF) or (0x1100 <= ord(c) <= 0x11FF) or (0x3130 <= ord(c) <= 0x318F)
        for c in text
    )


def translate_korean_to_english(text):
    if not text or not has_korean(text):
        return text

    if text in _translation_cache:
        return _translation_cache[text]

    url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=ko&tl=en&dt=t&q=" + urllib.parse.quote(text)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
            data = json.loads(raw.decode("utf-8"))
            translated = "".join([part[0] for part in data[0] if part[0]]).strip()
            if translated:
                _translation_cache[text] = translated
                return translated
    except Exception:
        pass

    return text


def ensure_config_exists():
    """Create a default sample config if it does not exist."""
    try:
        existing = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES)
    except (json.JSONDecodeError, OSError, ValueError):
        return
    if existing is None:
        sample = [
            {
                "name": "Personal Calendar",
                "url": "",
                "color": "#4A90E2",
                "enabled": True,
            }
        ]
        write_secure_json(CONFIG_PATH, sample, mode=0o600)


# Feeds and API answers are reused while the server says nothing changed.
SYNC_CACHE_DIR = os.path.join(STATE_DIR, "sync-cache")

# Secrets live in the desktop keyring (Secret Service, through libsecret's
# secret-tool) when one runs. The JSON file then holds this marker instead.
KEYRING_MARK = "@keyring"
KEYRING_APP = "chronica"
CALENDAR_SECRET_FIELDS = ("password", "jmapToken")
GOOGLE_SECRET_FIELDS = ("refresh_token", "client_secret")
# Windowed queries read this far past the window, so a cached answer still
# covers the window as it moves forward during the next days.
SYNC_REFRESH_MARGIN = timedelta(days=7)


def sync_cache_path(*parts):
    """One cache file per calendar source; the name does not reveal the source."""
    digest = hashlib.sha256("\0".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]
    return os.path.join(SYNC_CACHE_DIR, digest + ".json")


def sync_cache_load(path):
    try:
        data = safe_load_json(path, max_bytes=MAX_CACHE_BYTES)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def sync_cache_save(path, data):
    # A failed write only costs a full download next time.
    try:
        os.makedirs(SYNC_CACHE_DIR, mode=0o700, exist_ok=True)
        write_secure_json(path, data, max_bytes=MAX_CACHE_BYTES)
    except Exception:
        pass


def sync_cache_drop(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _secret_tool(args, value=None):
    """Run secret-tool; returns stdout, or None when it fails or is missing."""
    if not shutil.which("secret-tool"):
        return None
    try:
        proc = subprocess.run(["secret-tool"] + args, input=value, capture_output=True,
                              text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def stash_secrets(entry, fields, secret_id=None):
    """
    Move the plaintext secrets of one entry into the keyring, in place, and
    put KEYRING_MARK in their place. A secret the keyring refuses stays in
    the entry: it is never dropped. Returns True when the entry changed.
    """
    changed = False
    for field in fields:
        value = entry.get(field)
        if not isinstance(value, str) or not value or value == KEYRING_MARK:
            continue
        sid = secret_id or entry.get("secretId") or secrets.token_hex(8)
        stored = _secret_tool(["store", "--label", f"Chronica {entry.get('name') or sid} {field}",
                               "application", KEYRING_APP, "secret-id", sid, "field", field], value)
        if stored is not None:
            if not secret_id:
                entry["secretId"] = sid
            entry[field] = KEYRING_MARK
            changed = True
    return changed


def reveal_secrets(entry, fields, secret_id=None):
    """A copy of the entry with its KEYRING_MARK values read back ("" if gone)."""
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    sid = secret_id or entry.get("secretId")
    for field in fields:
        if out.get(field) == KEYRING_MARK:
            found = _secret_tool(["lookup", "application", KEYRING_APP, "secret-id", str(sid), "field", field]) if sid else None
            out[field] = found or ""
    return out


def forget_unused_secrets(calendars):
    """Clear keyring items of calendars that were removed from the config."""
    listing = _secret_tool(["search", "--all", "application", KEYRING_APP])
    if not listing:
        return
    keep = {str(c.get("secretId")) for c in calendars if isinstance(c, dict) and c.get("secretId")}
    keep.add("google")
    for sid in set(re.findall(r"^attribute\.secret-id = (\S+)$", listing, re.M)) - keep:
        _secret_tool(["clear", "application", KEYRING_APP, "secret-id", sid])


def load_calendars():
    """calendars.json with its keyring secrets filled in, for the fetchers."""
    try:
        calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES) or []
    except Exception:
        return []
    if not isinstance(calendars, list):
        return []
    return [reveal_secrets(c, CALENDAR_SECRET_FIELDS) for c in calendars]


def save_calendars(calendars):
    """Write calendars.json with the secrets moved to the keyring when possible."""
    for entry in calendars:
        if isinstance(entry, dict):
            stash_secrets(entry, CALENDAR_SECRET_FIELDS)
    write_secure_json(CONFIG_PATH, calendars, mode=0o600)
    forget_unused_secrets(calendars)


def migrate_secrets_to_keyring():
    """Move plaintext secrets of older configs into the keyring, once."""
    try:
        calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES)
        if isinstance(calendars, list) and any(
                isinstance(c, dict) and any(isinstance(c.get(f), str) and c.get(f) not in ("", KEYRING_MARK)
                                            for f in CALENDAR_SECRET_FIELDS)
                for c in calendars):
            if any(stash_secrets(c, CALENDAR_SECRET_FIELDS) for c in calendars if isinstance(c, dict)):
                write_secure_json(CONFIG_PATH, calendars, mode=0o600)
        auth = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES)
        if isinstance(auth, dict) and stash_secrets(auth, GOOGLE_SECRET_FIELDS, secret_id="google"):
            write_secure_json(AUTH_FILE, auth, mode=0o600)
    except Exception:
        pass


def unfold_lines(raw_text):
    """Unfold lines in an iCalendar stream according to RFC 5545."""
    lines = []
    for line in raw_text.splitlines():
        if not line:
            continue
        if (line.startswith(" ") or line.startswith("\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def unescape_ical_text(val):
    if not val:
        return ""
    # One pass, so an escaped backslash followed by "n" stays a backslash + n.
    escapes = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}
    val = re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), val)
    return val.strip()


# Common Windows/Exchange TZID names that are not IANA identifiers.
WINDOWS_TZ_ALIASES = {
    "EASTERN STANDARD TIME": "America/New_York",
    "CENTRAL STANDARD TIME": "America/Chicago",
    "MOUNTAIN STANDARD TIME": "America/Denver",
    "US MOUNTAIN STANDARD TIME": "America/Phoenix",
    "PACIFIC STANDARD TIME": "America/Los_Angeles",
    "ALASKAN STANDARD TIME": "America/Anchorage",
    "HAWAIIAN STANDARD TIME": "Pacific/Honolulu",
    "ATLANTIC STANDARD TIME": "America/Halifax",
    "GMT STANDARD TIME": "Europe/London",
    "GREENWICH STANDARD TIME": "Atlantic/Reykjavik",
    "W. EUROPE STANDARD TIME": "Europe/Berlin",
    "CENTRAL EUROPE STANDARD TIME": "Europe/Budapest",
    "CENTRAL EUROPEAN STANDARD TIME": "Europe/Warsaw",
    "ROMANCE STANDARD TIME": "Europe/Paris",
    "E. EUROPE STANDARD TIME": "Europe/Bucharest",
    "FLE STANDARD TIME": "Europe/Kiev",
    "GTB STANDARD TIME": "Europe/Athens",
    "RUSSIAN STANDARD TIME": "Europe/Moscow",
    "INDIA STANDARD TIME": "Asia/Kolkata",
    "CHINA STANDARD TIME": "Asia/Shanghai",
    "SINGAPORE STANDARD TIME": "Asia/Singapore",
    "TOKYO STANDARD TIME": "Asia/Tokyo",
    "KOREA STANDARD TIME": "Asia/Seoul",
    "AUS EASTERN STANDARD TIME": "Australia/Sydney",
    "NEW ZEALAND STANDARD TIME": "Pacific/Auckland",
    "UTC": "UTC",
    # The rest of CLDR windowsZones.xml (territory 001), canonical tzdata names.
    "AFGHANISTAN STANDARD TIME": "Asia/Kabul",
    "ALEUTIAN STANDARD TIME": "America/Adak",
    "ALTAI STANDARD TIME": "Asia/Barnaul",
    "ARAB STANDARD TIME": "Asia/Riyadh",
    "ARABIAN STANDARD TIME": "Asia/Dubai",
    "ARABIC STANDARD TIME": "Asia/Baghdad",
    "ARGENTINA STANDARD TIME": "America/Argentina/Buenos_Aires",
    "ASTRAKHAN STANDARD TIME": "Europe/Astrakhan",
    "AUS CENTRAL STANDARD TIME": "Australia/Darwin",
    "AUS CENTRAL W. STANDARD TIME": "Australia/Eucla",
    "AZERBAIJAN STANDARD TIME": "Asia/Baku",
    "AZORES STANDARD TIME": "Atlantic/Azores",
    "BAHIA STANDARD TIME": "America/Bahia",
    "BANGLADESH STANDARD TIME": "Asia/Dhaka",
    "BELARUS STANDARD TIME": "Europe/Minsk",
    "BOUGAINVILLE STANDARD TIME": "Pacific/Bougainville",
    "CANADA CENTRAL STANDARD TIME": "America/Regina",
    "CAPE VERDE STANDARD TIME": "Atlantic/Cape_Verde",
    "CAUCASUS STANDARD TIME": "Asia/Yerevan",
    "CEN. AUSTRALIA STANDARD TIME": "Australia/Adelaide",
    "CENTRAL AMERICA STANDARD TIME": "America/Guatemala",
    "CENTRAL ASIA STANDARD TIME": "Asia/Bishkek",
    "CENTRAL BRAZILIAN STANDARD TIME": "America/Cuiaba",
    "CENTRAL PACIFIC STANDARD TIME": "Pacific/Guadalcanal",
    "CENTRAL STANDARD TIME (MEXICO)": "America/Mexico_City",
    "CHATHAM ISLANDS STANDARD TIME": "Pacific/Chatham",
    "CUBA STANDARD TIME": "America/Havana",
    "DATELINE STANDARD TIME": "Etc/GMT+12",
    "E. AFRICA STANDARD TIME": "Africa/Nairobi",
    "E. AUSTRALIA STANDARD TIME": "Australia/Brisbane",
    "E. SOUTH AMERICA STANDARD TIME": "America/Sao_Paulo",
    "EASTER ISLAND STANDARD TIME": "Pacific/Easter",
    "EASTERN STANDARD TIME (MEXICO)": "America/Cancun",
    "EGYPT STANDARD TIME": "Africa/Cairo",
    "EKATERINBURG STANDARD TIME": "Asia/Yekaterinburg",
    "FIJI STANDARD TIME": "Pacific/Fiji",
    "GEORGIAN STANDARD TIME": "Asia/Tbilisi",
    "GREENLAND STANDARD TIME": "America/Nuuk",
    "HAITI STANDARD TIME": "America/Port-au-Prince",
    "IRAN STANDARD TIME": "Asia/Tehran",
    "ISRAEL STANDARD TIME": "Asia/Jerusalem",
    "JORDAN STANDARD TIME": "Asia/Amman",
    "KALININGRAD STANDARD TIME": "Europe/Kaliningrad",
    "LIBYA STANDARD TIME": "Africa/Tripoli",
    "LINE ISLANDS STANDARD TIME": "Pacific/Kiritimati",
    "LORD HOWE STANDARD TIME": "Australia/Lord_Howe",
    "MAGADAN STANDARD TIME": "Asia/Magadan",
    "MAGALLANES STANDARD TIME": "America/Punta_Arenas",
    "MARQUESAS STANDARD TIME": "Pacific/Marquesas",
    "MAURITIUS STANDARD TIME": "Indian/Mauritius",
    "MIDDLE EAST STANDARD TIME": "Asia/Beirut",
    "MONTEVIDEO STANDARD TIME": "America/Montevideo",
    "MOROCCO STANDARD TIME": "Africa/Casablanca",
    "MOUNTAIN STANDARD TIME (MEXICO)": "America/Mazatlan",
    "MYANMAR STANDARD TIME": "Asia/Yangon",
    "N. CENTRAL ASIA STANDARD TIME": "Asia/Novosibirsk",
    "NAMIBIA STANDARD TIME": "Africa/Windhoek",
    "NEPAL STANDARD TIME": "Asia/Kathmandu",
    "NEWFOUNDLAND STANDARD TIME": "America/St_Johns",
    "NORFOLK STANDARD TIME": "Pacific/Norfolk",
    "NORTH ASIA EAST STANDARD TIME": "Asia/Irkutsk",
    "NORTH ASIA STANDARD TIME": "Asia/Krasnoyarsk",
    "NORTH KOREA STANDARD TIME": "Asia/Pyongyang",
    "OMSK STANDARD TIME": "Asia/Omsk",
    "PACIFIC SA STANDARD TIME": "America/Santiago",
    "PACIFIC STANDARD TIME (MEXICO)": "America/Tijuana",
    "PAKISTAN STANDARD TIME": "Asia/Karachi",
    "PARAGUAY STANDARD TIME": "America/Asuncion",
    "QYZYLORDA STANDARD TIME": "Asia/Qyzylorda",
    "RUSSIA TIME ZONE 10": "Asia/Srednekolymsk",
    "RUSSIA TIME ZONE 11": "Asia/Kamchatka",
    "RUSSIA TIME ZONE 3": "Europe/Samara",
    "SA EASTERN STANDARD TIME": "America/Cayenne",
    "SA PACIFIC STANDARD TIME": "America/Bogota",
    "SA WESTERN STANDARD TIME": "America/La_Paz",
    "SAINT PIERRE STANDARD TIME": "America/Miquelon",
    "SAKHALIN STANDARD TIME": "Asia/Sakhalin",
    "SAMOA STANDARD TIME": "Pacific/Apia",
    "SAO TOME STANDARD TIME": "Africa/Sao_Tome",
    "SARATOV STANDARD TIME": "Europe/Saratov",
    "SE ASIA STANDARD TIME": "Asia/Bangkok",
    "SOUTH AFRICA STANDARD TIME": "Africa/Johannesburg",
    "SOUTH SUDAN STANDARD TIME": "Africa/Juba",
    "SRI LANKA STANDARD TIME": "Asia/Colombo",
    "SUDAN STANDARD TIME": "Africa/Khartoum",
    "SYRIA STANDARD TIME": "Asia/Damascus",
    "TAIPEI STANDARD TIME": "Asia/Taipei",
    "TASMANIA STANDARD TIME": "Australia/Hobart",
    "TOCANTINS STANDARD TIME": "America/Araguaina",
    "TOMSK STANDARD TIME": "Asia/Tomsk",
    "TONGA STANDARD TIME": "Pacific/Tongatapu",
    "TRANSBAIKAL STANDARD TIME": "Asia/Chita",
    "TURKEY STANDARD TIME": "Europe/Istanbul",
    "TURKS AND CAICOS STANDARD TIME": "America/Grand_Turk",
    "ULAANBAATAR STANDARD TIME": "Asia/Ulaanbaatar",
    "US EASTERN STANDARD TIME": "America/Indiana/Indianapolis",
    "UTC+12": "Etc/GMT-12",
    "UTC+13": "Etc/GMT-13",
    "UTC-02": "Etc/GMT+2",
    "UTC-08": "Etc/GMT+8",
    "UTC-09": "Etc/GMT+9",
    "UTC-11": "Etc/GMT+11",
    "VENEZUELA STANDARD TIME": "America/Caracas",
    "VLADIVOSTOK STANDARD TIME": "Asia/Vladivostok",
    "VOLGOGRAD STANDARD TIME": "Europe/Volgograd",
    "W. AUSTRALIA STANDARD TIME": "Australia/Perth",
    "W. CENTRAL AFRICA STANDARD TIME": "Africa/Lagos",
    "W. MONGOLIA STANDARD TIME": "Asia/Hovd",
    "WEST ASIA STANDARD TIME": "Asia/Tashkent",
    "WEST BANK STANDARD TIME": "Asia/Hebron",
    "WEST PACIFIC STANDARD TIME": "Pacific/Port_Moresby",
    "YAKUTSK STANDARD TIME": "Asia/Yakutsk",
    "YUKON STANDARD TIME": "America/Whitehorse",
}

_zone_cache = {}


def resolve_timezone(tzid):
    """
    Resolve an iCal TZID value to a tzinfo object, or None when it is unknown.
    Handles quoted names, prefixed forms (/mozilla.org/.../America/New_York)
    and the common Windows/Exchange zone names.
    """
    if not tzid or ZoneInfo is None:
        return None

    key = tzid.strip().strip('"')
    if not key:
        return None
    if key in _zone_cache:
        return _zone_cache[key]

    candidates = [key]
    if "/" in key:
        parts = [part for part in key.split("/") if part]
        if len(parts) >= 2:
            candidates.append("/".join(parts[-2:]))
        if parts:
            candidates.append(parts[-1])
    alias = WINDOWS_TZ_ALIASES.get(key.upper())
    if alias:
        candidates.append(alias)

    zone = None
    for cand in candidates:
        try:
            zone = ZoneInfo(cand)
            break
        except Exception:
            continue

    _zone_cache[key] = zone
    return zone


def to_local_naive(dt, tz):
    """Interpret naive dt as being in tz, then re-express it as local wall time."""
    try:
        return dt.replace(tzinfo=tz).astimezone().replace(tzinfo=None)
    except Exception:
        return dt


def extract_tzid(params):
    """Return the TZID parameter value from a property's parameter list."""
    for param in params or []:
        if param.upper().startswith("TZID="):
            return param.split("=", 1)[1]
    return None


def value_zone(val_str, params=None):
    """
    Return the tzinfo an iCal DATE-TIME value is written in: UTC for a trailing Z,
    a fixed offset for +HH:MM / -HHMM, the TZID parameter's zone, or None for
    floating values and unknown zones.
    """
    val_str = val_str.strip()
    if val_str.endswith("Z"):
        return timezone.utc
    offset_match = re.search(r"([+-])(\d\d):?(\d\d)$", val_str)
    if offset_match:
        sign = -1 if offset_match.group(1) == "-" else 1
        delta = timedelta(hours=int(offset_match.group(2)), minutes=int(offset_match.group(3)))
        return timezone(sign * delta)
    return resolve_timezone(extract_tzid(params))


def parse_datetime_value(val_str, params=None):
    """
    Parse an iCal date or datetime string into local wall time.

    UTC values (trailing Z), explicit numeric offsets and TZID=... parameters are
    all converted to the system timezone. Floating values (no zone information)
    are kept as-is, per RFC 5545.
    Returns: (is_all_day: bool, dt: datetime)
    """
    val_str = val_str.strip()
    if params and any(p.strip().upper() == "VALUE=DATE" for p in params):
        # e.g. 20260816
        try:
            d = datetime.strptime(val_str[:8], "%Y%m%d").date()
            return True, datetime(d.year, d.month, d.day, 0, 0, 0)
        except ValueError:
            pass

    if len(val_str) == 8 and val_str.isdigit():
        try:
            d = datetime.strptime(val_str, "%Y%m%d").date()
            return True, datetime(d.year, d.month, d.day, 0, 0, 0)
        except ValueError:
            pass

    # Try datetime formats: 20260816T143000Z or 20260816T143000
    cleaned = re.sub(r"[+-]\d\d:?\d\d$", "", val_str).rstrip("Z")
    # Strip subsecond fractions if present (e.g. .000 or .123456)
    cleaned = re.sub(r"\.\d+", "", cleaned)
    for fmt in (
        "%Y%m%dT%H%M%S", "%Y%m%dT%H%M",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"
    ):
        try:
            dt = datetime.strptime(cleaned[:19], fmt)
        except ValueError:
            continue

        zone = value_zone(val_str, params)
        return False, (to_local_naive(dt, zone) if zone is not None else dt)

    try:
        d = datetime.strptime(val_str[:8], "%Y%m%d").date()
        return True, datetime(d.year, d.month, d.day, 0, 0, 0)
    except Exception:
        return True, datetime.now()


def safe_int_param(val, default=1):
    """Safely extract an integer from a parameter string or integer without throwing."""
    if val is None:
        return default
    try:
        cleaned = str(val).strip()
        m = re.match(r"^[+-]?\d+", cleaned)
        if m:
            return int(m.group(0))
        return default
    except (ValueError, TypeError):
        return default


def parse_rrule(rrule_str):
    """Parse a basic RRULE string into key-value pairs."""
    rule = {}
    for part in rrule_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.upper()] = v
    return rule


def validate_meeting_url(url):
    """
    Validates and sanitizes meeting/conference URLs.
    Accepts only valid http:// or https:// URLs with well-formed hostnames.
    Rejects javascript:, file:, data:, HTML strings, control chars, quotes, and malformed URLs.
    Returns sanitized URL string or '' if invalid/unsafe.
    """
    if not isinstance(url, str) or not url:
        return ""
    url = url.strip().rstrip(";,)>]\"'")
    if not url:
        return ""
    # Reject strings with any control characters, whitespace, newlines, or HTML delimiters (<, >, ", ', `)
    if any(ord(c) < 0x21 or ord(c) > 0x7E or c in '<>"\'`' for c in url):
        return ""
    if not re.match(r"^https?://[a-zA-Z0-9.\-]+(?::\d+)?(?:/[^\s<>'\"`]*)?$", url, re.IGNORECASE):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        if not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        # Validate hostname encoding
        parsed.hostname.rstrip(".").encode("idna").decode("ascii")
        return url
    except Exception:
        return ""


def extract_meeting_info(location, description, summary):
    """
    Scans text fields for video conference / meeting URLs and identifies the provider.
    Returns: (meeting_url: str, meeting_provider: str) or (None, None)
    """
    combined = f"{location}\n{description}\n{summary}"
    if not combined.strip():
        return None, None

    patterns = [
        (r'https?://meet\.google\.com/[a-zA-Z0-9\-?=_&%.\-/#+~]+', "Google Meet"),
        (r'https?://(?:[a-zA-Z0-9-]+\.)?zoom\.us/(?:j/|my/|w/|wc/join/)[a-zA-Z0-9?=_&%.\-/#+~]+', "Zoom"),
        (r'https?://(?:teams\.microsoft\.com|teams\.live\.com)/(?:l/meetup-join|meet)/[a-zA-Z0-9?=_&%.\-/#+~]+', "Teams"),
        (r'https?://[a-zA-Z0-9-]+\.webex\.com/(?:meet|join|m)/[a-zA-Z0-9?=_&%.\-/#+~]+', "Webex"),
        (r'https?://meet\.jit\.si/[a-zA-Z0-9?=_&%.\-/#+~]+', "Jitsi"),
        (r'https?://whereby\.com/[a-zA-Z0-9?=_&%.\-/#+~]+', "Whereby"),
        (r'https?://chime\.aws/[a-zA-Z0-9?=_&%.\-/#+~]+', "Amazon Chime"),
    ]

    for pat, name in patterns:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            url = validate_meeting_url(m.group(0))
            if url:
                return url, name

    # Check if location contains any valid HTTP/HTTPS URL
    loc_url_m = re.search(r'https?://[^\s<>"\'\)\]]+', location or "")
    if loc_url_m:
        url = validate_meeting_url(loc_url_m.group(0))
        if url:
            return url, "Meeting Link"

    return None, None


def _rule_ints(rule, key, lo, hi, signed=False):
    """Integers of a comma list (BYMONTH=1,-1). Values out of range are dropped."""
    out = []
    for part in str(rule.get(key) or "").split(","):
        part = part.strip()
        if not re.fullmatch(r"[+-]?\d+", part):
            continue
        n = int(part)
        if lo <= n <= hi or (signed and -hi <= n <= -1):
            out.append(n)
    return out


def _rule_byday(rule):
    """BYDAY=MO,-1FR,2TU as [(ordinal or None, weekday index)]."""
    out = []
    for part in str(rule.get("BYDAY") or "").split(","):
        m = re.fullmatch(r"\s*([+-]?\d{1,2})?([A-Za-z]{2})\s*", part)
        if m and m.group(2).upper() in WEEKDAYS:
            ordinal = int(m.group(1)) if m.group(1) else None
            out.append((ordinal or None, WEEKDAYS.index(m.group(2).upper())))
    return out


def _week1_start(year, wkst):
    """First day of week 1: the first WKST-based week with 4+ days in `year`."""
    jan1 = date(year, 1, 1)
    offset = (jan1.weekday() - wkst) % 7
    return jan1 - timedelta(days=offset) if 7 - offset >= 4 else jan1 + timedelta(days=7 - offset)


def _week_number(d, wkst):
    """(week-numbering year, week number, weeks in that year) of a date."""
    year = d.year
    start = _week1_start(year, wkst)
    if d < start:
        year -= 1
        start = _week1_start(year, wkst)
    elif d >= _week1_start(year + 1, wkst):
        year += 1
        start = _week1_start(year, wkst)
    weeks = (_week1_start(year + 1, wkst) - start).days // 7
    return year, (d - start).days // 7 + 1, weeks


def _resolve(values, size):
    """Turn negative positions (-1 = last) into positive ones for a set of `size`."""
    return {v if v > 0 else size + 1 + v for v in values}


def rrule_occurrences(dtstart, rule, skip_to=None, stop_after=None):
    """
    Yield the start of every occurrence of an RRULE (RFC 5545 3.3.10), in order,
    as naive datetimes on DTSTART's wall clock. DTSTART is the first occurrence.

    Each period (a year, month, week or day, every INTERVAL) gives candidate
    days. The BYxxx parts keep the days that match all of them, BYHOUR,
    BYMINUTE and BYSECOND give the times, and BYSETPOS picks positions in the
    period. `skip_to` jumps whole periods forward (only without COUNT, which
    must count from DTSTART); `stop_after` ends the walk.
    """
    freq = str(rule.get("FREQ") or "").upper()
    if freq not in ("YEARLY", "MONTHLY", "WEEKLY", "DAILY"):
        # ponytail: HOURLY/MINUTELY/SECONDLY yield DTSTART only; calendar feeds
        # do not use them. Add a sub-day period here if one ever does.
        yield dtstart
        return

    interval = max(1, safe_int_param(rule.get("INTERVAL"), 1))
    wkst_code = str(rule.get("WKST") or "MO").upper()
    wkst = WEEKDAYS.index(wkst_code) if wkst_code in WEEKDAYS else 0
    bymonth = set(_rule_ints(rule, "BYMONTH", 1, 12))
    bymonthday = _rule_ints(rule, "BYMONTHDAY", 1, 31, signed=True)
    byyearday = _rule_ints(rule, "BYYEARDAY", 1, 366, signed=True)
    byweekno = _rule_ints(rule, "BYWEEKNO", 1, 53, signed=True)
    byday = _rule_byday(rule)
    bysetpos = _rule_ints(rule, "BYSETPOS", 1, 366, signed=True)
    byhour = sorted(set(_rule_ints(rule, "BYHOUR", 0, 23)))
    byminute = sorted(set(_rule_ints(rule, "BYMINUTE", 0, 59)))
    bysecond = sorted(set(_rule_ints(rule, "BYSECOND", 0, 59)))

    # Parts the rule leaves out come from DTSTART.
    if freq == "YEARLY" and not (byweekno or byyearday or bymonthday or byday):
        bymonth = bymonth or {dtstart.month}
        bymonthday = [dtstart.day]
    elif freq == "MONTHLY" and not (bymonthday or byday):
        bymonthday = [dtstart.day]
    elif freq == "WEEKLY" and not byday:
        byday = [(None, dtstart.weekday())]

    times = [dt_time(h, m, s)
             for h in (byhour or [dtstart.hour])
             for m in (byminute or [dtstart.minute])
             for s in (bysecond or [dtstart.second])]

    weekdays_any = {wd for ordinal, wd in byday if ordinal is None}
    ordinals = [(o, wd) for o, wd in byday if o is not None]
    # RFC 5545: ordinals count within the month for MONTHLY (and YEARLY with
    # BYMONTH), within the year for YEARLY, and mean nothing for WEEKLY/DAILY.
    if freq in ("WEEKLY", "DAILY") or (freq == "YEARLY" and byweekno):
        weekdays_any |= {wd for _, wd in ordinals}
        ordinals = []
    month_scope = freq == "MONTHLY" or (freq == "YEARLY" and bool(bymonth))

    def day_matches(d):
        if bymonth and d.month not in bymonth:
            return False
        if bymonthday and d.day not in _resolve(bymonthday, calendar.monthrange(d.year, d.month)[1]):
            return False
        year_len = 366 if calendar.isleap(d.year) else 365
        doy = d.timetuple().tm_yday
        if byyearday and doy not in _resolve(byyearday, year_len):
            return False
        if byweekno:
            _, week, weeks = _week_number(d, wkst)
            if week not in _resolve(byweekno, weeks):
                return False
        if byday:
            wd = d.weekday()
            if wd in weekdays_any:
                return True
            if month_scope:
                pos, size = d.day, calendar.monthrange(d.year, d.month)[1]
            else:
                pos, size = doy, year_len
            nth, nth_last = (pos - 1) // 7 + 1, -((size - pos) // 7 + 1)
            return any(wd == w and o in (nth, nth_last) for o, w in ordinals)
        return True

    def period_days(k):
        if freq == "YEARLY":
            year = dtstart.year + k
            months = sorted(bymonth) if bymonth else range(1, 13)
            return [date(year, m, d) for m in months for d in range(1, calendar.monthrange(year, m)[1] + 1)]
        if freq == "MONTHLY":
            year, month0 = divmod(dtstart.month - 1 + k, 12)
            year += dtstart.year
            return [date(year, month0 + 1, d) for d in range(1, calendar.monthrange(year, month0 + 1)[1] + 1)]
        if freq == "WEEKLY":
            first = dtstart.date() - timedelta(days=(dtstart.weekday() - wkst) % 7) + timedelta(weeks=k)
            return [first + timedelta(days=i) for i in range(7)]
        return [dtstart.date() + timedelta(days=k)]

    def periods_until(target):
        """Whole periods between DTSTART's period and the one holding `target`."""
        if freq == "YEARLY":
            return target.year - dtstart.year
        if freq == "MONTHLY":
            return (target.year - dtstart.year) * 12 + target.month - dtstart.month
        if freq == "WEEKLY":
            first = dtstart.date() - timedelta(days=(dtstart.weekday() - wkst) % 7)
            return (target.date() - first).days // 7
        return (target.date() - dtstart.date()).days

    k = 0
    if skip_to is not None and skip_to > dtstart:
        k = max(0, (periods_until(skip_to) - 1) // interval * interval)
    else:
        yield dtstart

    for _ in range(MAX_RECURRENCE_ITERATIONS):
        try:
            days = period_days(k)
        except (ValueError, OverflowError):
            return  # past year 9999
        if stop_after is not None and days and datetime.combine(days[0], dt_time()) > stop_after:
            return
        found = [datetime.combine(d, t) for d in days if day_matches(d) for t in times]
        if bysetpos:
            found = [found[p - 1] for p in sorted(_resolve(bysetpos, len(found))) if 1 <= p <= len(found)]
        for occurrence in found:
            if occurrence > dtstart:
                yield occurrence
        k += interval



def expand_recurring_event(event, window_start, window_end):
    """
    Expands a recurring VEVENT within [window_start, window_end] (local wall time).
    Bounded by MAX_RECURRENCE_ITERATIONS and MAX_EXPANDED_INSTANCES.

    RRULE parts (BYDAY, BYMONTHDAY, the time of day) are defined in DTSTART's own
    zone, so a zoned series is expanded in that zone and each occurrence is then
    converted to local time. Expanding the local copy instead puts a Wednesday
    15:00 New York meeting on Wednesday in Seoul, a day early, and drifts it an
    hour whenever only one of the two zones changes DST.
    """
    if not event.get("rrule"):
        return [event]

    zone = event.get("tz")
    if zone is None:
        return expand_rrule_in_wall_time(event, window_start, window_end)

    def to_source(local_dt):
        return local_dt.astimezone(zone).replace(tzinfo=None)

    source = dict(event)
    source["start_dt"] = to_source(event["start_dt"])
    source["end_dt"] = to_source(event["end_dt"])
    # EXDATE and override keys are local dates, so they are applied after
    # conversion rather than against source-zone dates.
    source["exdates"] = []
    slack = timedelta(days=1)
    occurrences = expand_rrule_in_wall_time(
        source, to_source(window_start) - slack, to_source(window_end) + slack, zone
    )

    exdates = set(event.get("exdates", []))
    instances = []
    for inst in occurrences:
        start_dt = to_local_naive(inst["start_dt"], zone)
        date_key = start_dt.strftime("%Y-%m-%d")
        if not window_start <= start_dt <= window_end or date_key in exdates:
            continue
        inst["start_dt"] = start_dt
        inst["end_dt"] = to_local_naive(inst["end_dt"], zone)
        inst["date_key"] = date_key
        inst["exdates"] = event.get("exdates", [])
        instances.append(inst)
    return instances


def expand_rrule_in_wall_time(event, window_start, window_end, zone=None):
    """
    Expands an RRULE on naive datetimes that share one wall clock. `zone` is the
    clock's tzinfo when it is not local time, so a UTC UNTIL can be moved onto it;
    a floating UNTIL is already on DTSTART's clock (RFC 5545 3.3.10).
    """
    rrule = event["rrule"]
    until_str = rrule.get("UNTIL")
    count_str = rrule.get("COUNT")

    until_dt = None
    if until_str:
        is_all_day_until, parsed_until = parse_datetime_value(until_str)
        if is_all_day_until:
            until_dt = datetime(parsed_until.year, parsed_until.month, parsed_until.day, 23, 59, 59)
        elif zone is not None and value_zone(until_str) is not None:
            until_dt = parsed_until.astimezone(zone).replace(tzinfo=None)
        else:
            until_dt = parsed_until
        if until_dt < window_start:
            return []

    count = safe_int_param(count_str, 0) if count_str else 0
    start_dt = event["start_dt"]
    duration = event["end_dt"] - start_dt
    exdates = set(event.get("exdates", []))
    stop_after = min(window_end, until_dt) if until_dt else window_end

    instances = []
    seen = 0
    for occurrence in rrule_occurrences(start_dt, rrule,
                                        skip_to=None if count else window_start,
                                        stop_after=stop_after):
        if occurrence > stop_after:
            break
        seen += 1
        if count and seen > count:
            break
        date_key = occurrence.strftime("%Y-%m-%d")
        if occurrence >= window_start and date_key not in exdates:
            inst = dict(event)
            inst["start_dt"] = occurrence
            inst["end_dt"] = occurrence + duration
            inst["date_key"] = date_key
            instances.append(inst)
            if len(instances) >= MAX_EXPANDED_INSTANCES:
                break
    return instances


def expand_multiday_event(event, window_start, window_end):
    """
    Expands a multi-day event across all affected calendar days within the window.
    Strictly clamped to window bounds to enforce an immediate CPU work ceiling.
    """
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    all_day = event.get("all_day", False)

    start_date = start_dt.date()
    # RFC 5545 specifies DTEND is exclusive
    if all_day:
        end_date = end_dt.date() - timedelta(days=1)
        if end_date < start_date:
            end_date = start_date
    elif end_dt > start_dt and end_dt.time() == datetime.min.time():
        end_date = end_dt.date() - timedelta(days=1)
        if end_date < start_date:
            end_date = start_date
    else:
        end_date = end_dt.date()

    if start_date == end_date:
        event["date_key"] = start_date.strftime("%Y-%m-%d")
        return [event]

    w_start_d = window_start.date()
    w_end_d = window_end.date()

    # Drop immediately if entirely outside the time window
    if end_date < w_start_d or start_date > w_end_d:
        return []

    # Clamp iteration range to the time window to enforce an immediate CPU work ceiling
    effective_start = max(start_date, w_start_d)
    effective_end = min(end_date, w_end_d)

    instances = []
    cur_date = effective_start
    max_days = (w_end_d - w_start_d).days + 10
    iterations = 0

    while cur_date <= effective_end and iterations < max_days and len(instances) < MAX_EXPANDED_INSTANCES:
        inst = dict(event)
        inst["date_key"] = cur_date.strftime("%Y-%m-%d")
        instances.append(inst)
        cur_date += timedelta(days=1)
        iterations += 1

    return instances if instances else [event]


def parse_ics(content, cal_info, window_start, window_end):
    """
    Parses an ICS file string into structured events within the time window.
    """
    lines = unfold_lines(content)
    raw_events = []
    # UID -> date keys of master occurrences replaced by an override VEVENT
    # (same UID plus RECURRENCE-ID). Date-keyed like EXDATE.
    overridden = {}
    in_vevent = False
    current = {}
    # Depth of nested components (VALARM) inside the VEVENT: their DESCRIPTION,
    # SUMMARY or DURATION belong to the alarm, not to the event.
    nested = 0

    for line in lines:
        if in_vevent and line.startswith("BEGIN:") and line != "BEGIN:VEVENT":
            nested += 1
            continue
        if nested:
            if line.startswith("END:"):
                nested -= 1
            continue
        if line == "BEGIN:VEVENT":
            in_vevent = True
            current = {"exdates": [], "rdates": []}
            continue
        elif line == "END:VEVENT":
            if in_vevent and "DTSTART" in current:
                # Record overrides before the cancelled check: a cancelled
                # override still removes the occurrence it names.
                if "RECURRENCE-ID" in current and "UID" in current:
                    overridden.setdefault(current["UID"], set()).add(current["RECURRENCE-ID"])
                # Skip cancelled events
                if current.get("STATUS", "").upper() != "CANCELLED":
                    raw_events.append(current)
            in_vevent = False
            current = {}
            continue

        if not in_vevent:
            continue

        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        key_part, val_part = parts[0], parts[1]

        prop_parts = key_part.split(";")
        prop_name = prop_parts[0].upper()
        prop_params = prop_parts[1:] if len(prop_parts) > 1 else []

        if prop_name == "DTSTART":
            all_day, dt = parse_datetime_value(val_part, prop_params)
            current["DTSTART"] = dt
            current["all_day"] = all_day
            # The zone the series is defined in; recurring events expand there.
            current["TZ"] = None if all_day else value_zone(val_part, prop_params)
        elif prop_name == "DTEND":
            _, dt = parse_datetime_value(val_part, prop_params)
            current["DTEND"] = dt
        elif prop_name == "DURATION":
            # RFC 5545 §3.3.6: VEVENT may use DURATION instead of DTEND
            # (e.g. Sisu / university feeds emit DTSTART + DURATION only).
            current["DURATION"] = val_part.strip()
        elif prop_name == "SUMMARY":
            current["SUMMARY"] = unescape_ical_text(val_part)
        elif prop_name == "LOCATION":
            current["LOCATION"] = unescape_ical_text(val_part)
        elif prop_name == "DESCRIPTION":
            current["DESCRIPTION"] = unescape_ical_text(val_part)
        elif prop_name == "UID":
            current["UID"] = val_part.strip()
        elif prop_name == "STATUS":
            current["STATUS"] = val_part.strip().upper()
        elif prop_name == "URL":
            current["URL"] = val_part.strip()
        elif prop_name == "RRULE":
            current["RRULE"] = parse_rrule(val_part)
        elif prop_name == "RECURRENCE-ID":
            _, rid_dt = parse_datetime_value(val_part.strip(), prop_params)
            current["RECURRENCE-ID"] = rid_dt.strftime("%Y-%m-%d")
        elif prop_name == "RDATE":
            # Extra occurrences. A PERIOD value ("start/end") keeps its start.
            for rd_val in val_part.split(","):
                rd_val = rd_val.split("/", 1)[0].strip()
                if rd_val:
                    _, rd_dt = parse_datetime_value(rd_val, [p for p in prop_params if not p.upper().startswith("VALUE=")])
                    current["rdates"].append(rd_dt)
        elif prop_name == "EXDATE":
            for ex_val in val_part.split(","):
                ex_val = ex_val.strip()
                if ex_val:
                    _, ex_dt = parse_datetime_value(ex_val, prop_params)
                    current["exdates"].append(ex_dt.strftime("%Y-%m-%d"))

    auto_translate = cal_info.get("translateKorean", False)
    # A subscription feed is read-only; the same calendar becomes writable as
    # soon as the entry also carries CalDAV credentials for the collection.
    can_write = has_caldav_write(cal_info)
    normalized = []
    for raw in raw_events:
        start_dt = raw.get("DTSTART")
        if not start_dt:
            continue
        all_day = raw.get("all_day", False)
        end_dt = raw.get("DTEND")
        if end_dt is None and raw.get("DURATION"):
            # RFC 5545: DTEND and DURATION MUST NOT co-occur; DTEND wins if present.
            end_dt = start_dt + parse_iso_duration(raw["DURATION"])
        if end_dt is None:
            end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
        if end_dt < start_dt:
            end_dt = start_dt

        title = raw.get("SUMMARY", "(Untitled Event)")
        location = raw.get("LOCATION", "")
        description = raw.get("DESCRIPTION", "")
        raw_url = raw.get("URL", "")

        if auto_translate:
            title = translate_korean_to_english(title)
            location = translate_korean_to_english(location)

        meeting_url, meeting_provider = extract_meeting_info(
            f"{location} {raw_url}", description, title
        )

        evt = {
            "id": raw.get("UID", f"evt_{int(start_dt.timestamp())}"),
            "title": title,
            "location": location,
            "description": description,
            "calendar": cal_info.get("name", "Calendar"),
            "calendarId": cal_info.get("caldavUrl", "") if can_write else "",
            "calendarType": "caldav" if can_write else "ical",
            "writable": can_write,
            "color": cal_info.get("color", "#4A90E2"),
            "all_day": all_day,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "date_key": start_dt.strftime("%Y-%m-%d"),
            "meetingUrl": meeting_url or "",
            "meetingProvider": meeting_provider or "",
            "rrule": raw.get("RRULE"),
            # Overrides of one occurrence have no RRULE of their own.
            "recurring": bool(raw.get("RRULE") or raw.get("rdates")) or "RECURRENCE-ID" in raw,
            "tz": raw.get("TZ"),
            "exdates": raw.get("exdates", []),
        }

        if evt["rrule"] and "RECURRENCE-ID" not in raw:
            evt["exdates"] = evt["exdates"] + sorted(overridden.get(raw.get("UID"), ()))

        if evt["rrule"]:
            occurrences = expand_recurring_event(evt, window_start, window_end)
        elif start_dt.strftime("%Y-%m-%d") not in evt["exdates"]:
            occurrences = [evt]
        else:
            occurrences = []

        # RDATE adds occurrences next to (or instead of) the RRULE ones.
        taken = {occ["start_dt"] for occ in occurrences}
        for rd_dt in raw.get("rdates", []):
            date_key = rd_dt.strftime("%Y-%m-%d")
            if rd_dt in taken or date_key in evt["exdates"] or not window_start <= rd_dt <= window_end:
                continue
            taken.add(rd_dt)
            inst = dict(evt)
            inst["start_dt"] = rd_dt
            inst["end_dt"] = rd_dt + (end_dt - start_dt)
            inst["date_key"] = date_key
            occurrences.append(inst)

        for occ in occurrences:
            for inst in expand_multiday_event(occ, window_start, window_end):
                inst_dt = datetime.strptime(inst["date_key"], "%Y-%m-%d")
                if window_start <= inst_dt <= window_end:
                    normalized.append(inst)

    return normalized


AUTH_FILE = os.path.join(STATE_DIR, "google-auth.json")

# Google answers "invalid_grant" when a refresh token can never be used again.
# The usual cause is an OAuth app left in "Testing" publishing status: Google
# expires those refresh tokens after 7 days, so the calendar silently drops
# out about once a week. Publishing the app ("In production") stops that.
GOOGLE_AUTH_STATUS_MISSING = "auth_required: run google-auth.py"
GOOGLE_AUTH_STATUS_EXPIRED = "auth_expired: Google login expired or revoked - reconnect in Settings"
GOOGLE_AUTH_EXPIRED_HINT = (
    "Google rejected the saved login (token expired or revoked). "
    "Open the calendar panel and click Reconnect. If this happens every "
    "week, publish your OAuth app (In production) in Google Cloud Console."
)


def _notify_desktop(title, body):
    """Best-effort desktop notification; never raises, never blocks the sync."""
    try:
        exe = shutil.which("notify-send")
        if not exe:
            return
        subprocess.run(
            [exe, "-a", "Omarchy Calendar", "-i", "x-office-calendar", "-u", "critical",
             str(title), str(body)],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _record_google_refresh_error(auth_data, detail):
    """Persist a permanent refresh failure so the UI can offer a reconnect."""
    first_time = not auth_data.get("refresh_error")
    auth_data["refresh_error"] = "invalid_grant"
    auth_data["refresh_error_detail"] = str(detail)[:200]
    auth_data["refresh_error_at"] = int(time.time())
    auth_data.pop("access_token", None)
    auth_data.pop("expires_at", None)
    try:
        write_secure_json(AUTH_FILE, auth_data, mode=0o600)
    except Exception:
        pass
    if first_time:
        _notify_desktop("Google Calendar disconnected", GOOGLE_AUTH_EXPIRED_HINT)


def resolve_google_access_token():
    """
    Return (access_token, state, detail).

    state is one of:
      "ok"      - token usable
      "missing" - no credentials saved yet (google-auth.py never run)
      "revoked" - Google permanently rejected the refresh token (invalid_grant)
      "error"   - transient failure (network, 5xx); credentials still valid
    """
    try:
        auth_data = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES)
    except Exception as exc:
        return None, "error", f"cannot read auth file: {exc}"
    if not auth_data:
        return None, "missing", "no google-auth.json"

    now = time.time()
    if auth_data.get("access_token") and auth_data.get("expires_at", 0) > now + 60:
        return auth_data["access_token"], "ok", ""

    # auth_data is written back as is, so it keeps the keyring markers.
    revealed = reveal_secrets(auth_data, GOOGLE_SECRET_FIELDS, secret_id="google")
    refresh_token = revealed.get("refresh_token")
    client_id = auth_data.get("client_id")
    client_secret = revealed.get("client_secret")
    if not refresh_token or not client_id or not client_secret:
        return None, "missing", "incomplete credentials"

    url = "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        data = json.loads(raw.decode("utf-8"))
        access_token = data.get("access_token")
        if not access_token:
            return None, "error", "token endpoint returned no access_token"
        auth_data["access_token"] = access_token
        auth_data["expires_at"] = int(now) + int(data.get("expires_in", 3600) or 3600)
        auth_data["updated_at"] = int(now)
        for key in ("refresh_error", "refresh_error_detail", "refresh_error_at"):
            auth_data.pop(key, None)
        try:
            write_secure_json(AUTH_FILE, auth_data, mode=0o600)
        except Exception:
            pass
        return access_token, "ok", ""
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = safe_read_bytes(exc, max_bytes=64 * 1024).decode("utf-8", "replace")
        except Exception:
            pass
        err_code = ""
        err_desc = ""
        try:
            parsed = json.loads(body) if body else {}
            err_code = str(parsed.get("error", ""))
            err_desc = str(parsed.get("error_description", ""))
        except Exception:
            pass
        if exc.code in (400, 401) and err_code in ("invalid_grant", "invalid_client", "unauthorized_client"):
            detail = f"{err_code}: {err_desc}".strip(": ")
            _record_google_refresh_error(auth_data, detail)
            return None, "revoked", detail
        return None, "error", f"HTTP {exc.code} {err_code or ''}".strip()
    except Exception as exc:
        return None, "error", str(exc) or exc.__class__.__name__


# Why the last token lookup failed, for callers that only get None back.
_LAST_GOOGLE_AUTH = {"state": "missing", "detail": ""}


def get_google_access_token():
    """Retrieve or refresh Google OAuth2 access token (None when unavailable)."""
    try:
        token, state, detail = resolve_google_access_token()
    except Exception as exc:
        token, state, detail = None, "error", str(exc) or exc.__class__.__name__
    _LAST_GOOGLE_AUTH["state"] = state
    _LAST_GOOGLE_AUTH["detail"] = detail
    return token


def google_auth_failure_status():
    """Calendar status string explaining why get_google_access_token() returned None."""
    return google_auth_status_string(_LAST_GOOGLE_AUTH["state"], _LAST_GOOGLE_AUTH["detail"])


def google_auth_summary():
    """Describe the saved Google login for the UI without touching the network."""
    summary = {"authenticated": False, "state": "missing", "detail": ""}
    try:
        auth_d = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES)
    except Exception:
        return summary
    if not auth_d or not (auth_d.get("refresh_token") and auth_d.get("client_id")):
        return summary
    if auth_d.get("refresh_error"):
        summary["state"] = "revoked"
        summary["detail"] = str(auth_d.get("refresh_error_detail") or auth_d.get("refresh_error"))
        summary["since"] = int(auth_d.get("refresh_error_at") or 0)
        return summary
    summary["authenticated"] = True
    summary["state"] = "ok"
    return summary


def google_auth_status_string(state, detail):
    if state == "missing":
        return GOOGLE_AUTH_STATUS_MISSING
    if state == "revoked":
        return GOOGLE_AUTH_STATUS_EXPIRED
    return f"error: Google token refresh failed ({detail or 'unknown'})"


def fetch_google_api_calendar(cal_info, window_start, window_end):
    """Fetch events directly from Google Calendar API v3."""
    name = cal_info.get("name", "Google Calendar")
    cal_id = cal_info.get("googleCalendarId") or cal_info.get("calendarId")
    if not cal_id:
        return {"name": name, "color": cal_info.get("color", "#4A90E2"), "events": [], "status": "no_calendar_id", "count": 0}

    access_token = get_google_access_token()
    if not access_token:
        return {
            "name": name,
            "color": cal_info.get("color", "#4A90E2"),
            "events": [],
            "status": google_auth_failure_status(),
            "count": 0,
        }

    encoded_cal_id = urllib.parse.quote(cal_id, safe="")
    time_min = window_start.strftime("%Y-%m-%dT00:00:00Z")
    time_max = window_end.strftime("%Y-%m-%dT23:59:59Z")

    params = urllib.parse.urlencode({
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
    })

    base_url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal_id}/events?{params}"

    try:
        # The list URL is the same all day, so its ETag lets Google answer
        # 304 when nothing changed.
        cache_path = sync_cache_path("google", cal_id)
        cached = sync_cache_load(cache_path)
        if cached.get("url") != base_url or not isinstance(cached.get("items"), list):
            cached = {}

        items = []
        page_token = None
        data = {}
        # A page holds at most 250 events: follow nextPageToken (bounded).
        for _ in range(20):
            url = base_url
            headers = {"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT}
            if page_token:
                url += "&" + urllib.parse.urlencode({"pageToken": page_token})
            elif cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
                    data = json.loads(raw.decode("utf-8"))
                    etag = (getattr(resp, "headers", None) or {}).get("ETag")
            except urllib.error.HTTPError as exc:
                if exc.code != 304 or page_token:
                    raise
                items, data, etag = cached["items"], {"accessRole": cached.get("accessRole")}, None
                break
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                # Only a one-page answer is cached: its ETag covers all of it.
                if etag and len(items) == len(data.get("items", [])):
                    sync_cache_save(cache_path, {"url": base_url, "etag": etag, "items": items,
                                                 "accessRole": data.get("accessRole")})
                break

        # Shared calendars can be read-only for this account.
        writable = (data.get("accessRole") or "owner") in ("owner", "writer")
        auto_translate = cal_info.get("translateKorean", False)
        events = []

        for item in items:
            if item.get("status") == "cancelled":
                continue

            start_info = item.get("start", {})
            end_info = item.get("end", {})

            if "date" in start_info:
                all_day = True
                d_str = start_info["date"]
                start_dt = datetime.strptime(d_str[:10], "%Y-%m-%d")
                end_dt = datetime.strptime(end_info.get("date", d_str)[:10], "%Y-%m-%d") if "date" in end_info else start_dt + timedelta(days=1)
            elif "dateTime" in start_info:
                all_day = False
                start_params = ["TZID=" + start_info["timeZone"]] if start_info.get("timeZone") else None
                _, start_dt = parse_datetime_value(start_info["dateTime"], start_params)
                if "dateTime" in end_info:
                    end_params = ["TZID=" + end_info["timeZone"]] if end_info.get("timeZone") else None
                    _, end_dt = parse_datetime_value(end_info["dateTime"], end_params)
                else:
                    end_dt = start_dt + timedelta(hours=1)
            else:
                continue

            title = item.get("summary", "(Untitled Event)")
            location = item.get("location", "")
            description = item.get("description", "")

            if auto_translate:
                title = translate_korean_to_english(title)
                location = translate_korean_to_english(location)

            # Direct Google Meet link check
            meeting_url = validate_meeting_url(item.get("hangoutLink") or "")
            meeting_provider = "Google Meet" if meeting_url else ""

            if not meeting_url:
                conf_data = item.get("conferenceData", {})
                for ep in conf_data.get("entryPoints", []):
                    if ep.get("uri"):
                        u = validate_meeting_url(ep.get("uri"))
                        if u:
                            meeting_url = u
                            meeting_provider = "Google Meet" if "meet.google" in meeting_url else "Meeting"
                            break

            if not meeting_url:
                meeting_url, meeting_provider = extract_meeting_info(location, description, title)

            evt = {
                "id": item.get("id", f"evt_{int(start_dt.timestamp())}"),
                "title": title,
                "location": location,
                "description": description,
                "calendar": cal_info.get("name", "Google Calendar"),
                "calendarId": cal_id,
                "calendarType": "google",
                "writable": writable,
                "color": cal_info.get("color", "#4A90E2"),
                "all_day": all_day,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "date_key": start_dt.strftime("%Y-%m-%d"),
                "meetingUrl": meeting_url or "",
                "meetingProvider": meeting_provider or "",
                "rrule": None,
                "exdates": [],
            }

            multidays = expand_multiday_event(evt, window_start, window_end)
            events.extend(multidays)

        return {
            "name": name,
            "color": cal_info.get("color", "#4A90E2"),
            "type": "google",
            "writable": writable,
            "events": events,
            "status": "ok",
            "count": len(events),
        }
    except Exception as e:
        return {
            "name": name,
            "color": cal_info.get("color", "#4A90E2"),
            "events": [],
            "status": f"error: {str(e)}",
            "count": 0,
        }


def fetch_calendar(cal_info, window_start, window_end):
    """Fetch single calendar from URL or local file."""
    name = cal_info.get("name", "Calendar")
    raw_url = cal_info.get("url", "").strip()

    if not raw_url:
        return {"name": name, "color": cal_info.get("color", "#4A90E2"), "events": [], "status": "no_url", "count": 0}

    username = cal_info.get("username")
    password = cal_info.get("password")

    # Convert webcal:// or webcals:// to https://
    if raw_url.startswith("webcal://"):
        url = "https://" + raw_url[9:]
    elif raw_url.startswith("webcals://"):
        url = "https://" + raw_url[10:]
    elif raw_url.startswith("http://") or raw_url.startswith("https://") or raw_url.startswith("file://"):
        url = raw_url
    else:
        # Fallback to https:// or local path
        if os.path.exists(os.path.expanduser(raw_url)):
            url = os.path.expanduser(raw_url)
        else:
            url = "https://" + raw_url

    headers = {"User-Agent": USER_AGENT}
    auth_header = None

    try:
        if url.startswith("file://") or url.startswith("/"):
            path = url[7:] if url.startswith("file://") else url
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = safe_read_text(f, max_bytes=MAX_ICAL_BYTES)
        else:
            # Extract credentials embedded in URL if not already provided
            parsed = urllib.parse.urlsplit(url)
            if parsed.username and not username:
                username = urllib.parse.unquote(parsed.username)
            if parsed.password and password is None:
                password = urllib.parse.unquote(parsed.password)

            if parsed.username or parsed.password:
                if parsed.hostname:
                    host = f"[{parsed.hostname}]" if ":" in parsed.hostname and not parsed.hostname.startswith("[") else parsed.hostname
                    netloc = f"{host}:{parsed.port}" if parsed.port else host
                else:
                    netloc = parsed.netloc
                url = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

            if username and password is not None:
                user_str = str(username).strip()
                pass_str = str(password)
                if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in user_str + pass_str):
                    raise ValueError("Calendar username or password contains invalid characters")
                if urllib.parse.urlsplit(url).scheme != "https":
                    raise ValueError("Refusing to send the calendar password over plain http: use an https:// URL")
                auth_str = f"{user_str}:{pass_str}"
                auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
                auth_header = f"Basic {auth_b64}"

            # Ask the server to answer 304 when the feed did not change.
            cache_path = sync_cache_path("ics", url, username or "")
            cached = sync_cache_load(cache_path)
            if isinstance(cached.get("body"), str):
                if cached.get("etag"):
                    headers["If-None-Match"] = cached["etag"]
                if cached.get("lastModified"):
                    headers["If-Modified-Since"] = cached["lastModified"]

            req = urllib.request.Request(url, headers=headers)
            if auth_header:
                # urllib copies normal headers to every redirect, even to
                # another host; an unredirected header stays with this request.
                # ponytail: a same-host redirect also drops it (the server then
                # answers 401); follow redirects by hand if a feed needs that.
                req.add_unredirected_header("Authorization", auth_header)
            resp_content = None
            # Retry transient connection resets / throttling (common on Apple iCloud CalDAV)
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        raw = safe_read_bytes(resp, max_bytes=MAX_ICAL_BYTES)
                        resp_content = raw.decode("utf-8", errors="ignore")
                        resp_headers = getattr(resp, "headers", None) or {}
                        etag = resp_headers.get("ETag")
                        last_modified = resp_headers.get("Last-Modified")
                    if etag or last_modified:
                        sync_cache_save(cache_path, {"etag": etag, "lastModified": last_modified, "body": resp_content})
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 304 and isinstance(cached.get("body"), str):
                        resp_content = cached["body"]
                        break
                    # A 4xx (wrong URL, wrong password) will not change on a retry.
                    if attempt == 0 and (exc.code >= 500 or exc.code == 429):
                        time.sleep(0.5)
                        continue
                    raise
                except (urllib.error.URLError, TimeoutError, OSError, ConnectionError):
                    if attempt == 0:
                        time.sleep(0.5)
                        continue
                    raise
            content = resp_content or ""

        events = parse_ics(content, cal_info, window_start, window_end)
        return {
            "name": name,
            "color": cal_info.get("color", "#4A90E2"),
            "type": "caldav" if has_caldav_write(cal_info) else "ical",
            "writable": has_caldav_write(cal_info),
            "events": events,
            "status": "ok",
            "count": len(events),
        }
    except Exception as e:
        return {
            "name": name,
            "color": cal_info.get("color", "#4A90E2"),
            "events": [],
            "status": f"error: {str(e)}",
            "count": 0,
        }


def parse_iso_duration(duration_str):
    """
    Parses ISO 8601 duration strings like 'PT1H30M', 'P1D', 'PT45M', etc. into a timedelta.
    """
    if not duration_str or not isinstance(duration_str, str):
        return timedelta(hours=1)

    match = re.match(
        r'^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$',
        duration_str
    )
    if match:
        parts = match.groupdict()
        days = int(parts["days"]) if parts.get("days") else 0
        hours = int(parts["hours"]) if parts.get("hours") else 0
        minutes = int(parts["minutes"]) if parts.get("minutes") else 0
        seconds = int(parts["seconds"]) if parts.get("seconds") else 0
        td = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if td.total_seconds() > 0:
            return td

    w_match = re.match(r'^P(?P<weeks>\d+)W$', duration_str)
    if w_match:
        return timedelta(weeks=int(w_match.group("weeks")))

    return timedelta(hours=1)


def parse_jmap_datetime(dt_str, tzid=None):
    """
    Parses a JSCalendar LocalDateTime (or ISO 8601 value) into local wall time.
    JMAP carries the zone separately in the event's timeZone property; a null
    timeZone means a floating time and is left untouched.
    """
    if not dt_str:
        return datetime.now()
    cleaned = re.sub(r"[+-]\d\d:?\d\d$", "", str(dt_str)).rstrip("Z")
    if len(cleaned) == 10:  # YYYY-MM-DD
        try:
            return datetime.strptime(cleaned, "%Y-%m-%d")
        except Exception:
            pass

    params = ["TZID=" + tzid] if tzid else None
    is_all_day, dt = parse_datetime_value(str(dt_str), params)
    if not is_all_day:
        return dt

    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        return datetime.now()


def validate_jmap_https_url(url, trusted_origin=None, label="JMAP"):
    """Return a validated credential-free HTTPS URL and its canonical origin."""
    if not isinstance(url, str) or not url or any(ord(char) < 0x20 for char in url) or "\\" in url:
        raise ValueError(f"{label} URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} URL is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(f"{label} URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} URL must not contain credentials")
    if parsed.fragment:
        raise ValueError(f"{label} URL must not contain a fragment")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"{label} URL hostname is invalid") from exc
    origin = ("https", hostname, port or 443)
    if trusted_origin is not None and origin != trusted_origin:
        raise ValueError(f"{label} URL must remain on the configured session origin")
    return url, origin


class JmapSameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow bearer-authenticated redirects only within the trusted HTTPS origin."""

    def __init__(self, trusted_origin):
        super().__init__()
        self.trusted_origin = trusted_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_jmap_https_url(newurl, self.trusted_origin)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_trusted_jmap(opener, request, trusted_origin, timeout):
    """Open a JMAP request and verify the transport's final URL before use."""
    response = opener.open(request, timeout=timeout)
    try:
        validate_jmap_https_url(response.geturl(), trusted_origin)
    except Exception:
        response.close()
        raise
    return response


def fetch_jmap_calendar(cal_info, window_start, window_end):
    """
    Fetches calendar events from a JMAP server (RFC 8620, RFC 9670, RFC 8984 JSCalendar).
    Compatible with Fastmail, Stalwart, Cyrus IMAP, Apache James, and generic JMAP servers.
    """
    name = cal_info.get("name", "JMAP Calendar")
    color = cal_info.get("color", "#ff7700")
    token = (cal_info.get("jmapToken") or cal_info.get("token") or cal_info.get("bearerToken") or "").strip()
    if not token:
        return {
            "name": name,
            "color": color,
            "events": [],
            "status": "auth_required: no JMAP token configured",
            "count": 0,
        }

    cache_path = None
    try:
        cal_id = cal_info.get("jmapCalendarId") or cal_info.get("calendarId")
        cache_path = sync_cache_path("jmap", jmap_session_url(cal_info), cal_id or "",
                                     hashlib.sha256(token.encode("utf-8")).hexdigest())
        cached = sync_cache_load(cache_path)
        # Step 1: Session Discovery. The session rarely changes: reused for a day.
        session = jmap_session(cal_info, cached)
        api_url, account_id, jmap_using = session["api_url"], session["account_id"], session["using"]

        def jmap_call(method_calls):
            body = json.dumps({"using": jmap_using, "methodCalls": method_calls}).encode("utf-8")
            req = urllib.request.Request(api_url, data=body, headers=session["headers"], method="POST")
            with open_trusted_jmap(session["opener"], req, session["origin"], timeout=15) as resp:
                raw_data = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
                return json.loads(raw_data.decode("utf-8")).get("methodResponses", [])

        # Step 2a: when no event changed since the last sync, its list still holds.
        raw_events = None
        if (cached.get("state") and isinstance(cached.get("list"), list)
                and cached.get("start", "~") <= window_start.isoformat()
                and cached.get("end", "") >= window_end.isoformat()):
            changes = jmap_call([
                ["CalendarEvent/changes", {"accountId": account_id, "sinceState": cached["state"]}, "c0"],
            ])
            if changes and changes[0][0] == "CalendarEvent/changes":
                delta = changes[0][1]
                if not (delta.get("created") or delta.get("updated") or delta.get("destroyed") or delta.get("hasMoreChanges")):
                    raw_events = cached["list"]

        # Step 2b: Query and Get Events
        if raw_events is None:
            query_end = window_end + SYNC_REFRESH_MARGIN
            cal_filter = {
                "after": window_start.strftime("%Y-%m-%dT00:00:00Z"),
                "before": query_end.strftime("%Y-%m-%dT23:59:59Z"),
            }
            if cal_id and cal_id != "primary":
                cal_filter["inCalendars"] = [cal_id]

            method_responses = jmap_call([
                ["CalendarEvent/query", {"accountId": account_id, "filter": cal_filter, "expandRecurrences": True}, "q0"],
                ["CalendarEvent/get", {
                    "accountId": account_id,
                    "#ids": {"resultOf": "q0", "name": "CalendarEvent/query", "path": "/ids"},
                }, "get0"],
            ])
            raw_events = []
            for resp_name, resp_args, resp_call_id in method_responses:
                if resp_name == "CalendarEvent/get":
                    raw_events = resp_args.get("list", [])
                    cached.update({"state": resp_args.get("state"), "list": raw_events,
                                   "start": window_start.isoformat(), "end": query_end.isoformat()})
                    break
                elif resp_name == "error":
                    return {
                        "name": name,
                        "color": color,
                        "events": [],
                        "status": f"jmap_error: {resp_args.get('type', 'unknown')}",
                        "count": 0,
                    }
        sync_cache_save(cache_path, cached)

        # Step 3: Parse JSCalendar (RFC 8984) events
        auto_translate = cal_info.get("translateKorean", False)
        events = []

        for item in raw_events:
            if item.get("status") == "cancelled":
                continue

            title = item.get("title") or item.get("summary") or "(Untitled Event)"
            description = item.get("description") or ""

            # Parse location(s)
            location = ""
            locs = item.get("locations", {})
            if isinstance(locs, dict):
                loc_names = [v.get("name", "") for v in locs.values() if isinstance(v, dict) and v.get("name")]
                location = ", ".join(filter(None, loc_names))
            elif isinstance(locs, str):
                location = locs

            if auto_translate:
                title = translate_korean_to_english(title)
                location = translate_korean_to_english(location)

            # Detect meeting URL from virtualLocations or text
            meeting_url = ""
            meeting_provider = ""
            vlocs = item.get("virtualLocations", {})
            if isinstance(vlocs, dict):
                for vl in vlocs.values():
                    if isinstance(vl, dict) and vl.get("uri"):
                        raw_u = str(vl.get("uri")).strip()
                        safe_u = validate_meeting_url(raw_u)
                        if not safe_u:
                            continue
                        _, prov = extract_meeting_info(safe_u, "", "")
                        if prov:
                            meeting_url = safe_u
                            meeting_provider = prov
                            break
                        elif not meeting_url:
                            meeting_url = safe_u
                            meeting_provider = "Online Meeting"

            if not meeting_url:
                meeting_url, meeting_provider = extract_meeting_info(location, description, title)

            all_day = bool(item.get("showWithoutTime"))
            start_str = item.get("start")
            if not start_str:
                continue

            start_dt = parse_jmap_datetime(start_str, item.get("timeZone"))

            if item.get("duration"):
                dur = parse_iso_duration(item.get("duration"))
                end_dt = start_dt + dur
            elif item.get("end"):
                end_dt = parse_jmap_datetime(item.get("end"), item.get("timeZone"))
            elif all_day:
                end_dt = start_dt + timedelta(days=1)
            else:
                end_dt = start_dt + timedelta(hours=1)

            evt = {
                "id": item.get("id", f"jmap_{int(start_dt.timestamp())}"),
                "title": title,
                "location": location,
                "description": description,
                "calendar": name,
                "calendarId": cal_info.get("jmapCalendarId") or cal_info.get("calendarId") or "",
                "calendarType": "jmap",
                "writable": True,
                "color": color,
                "all_day": all_day,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "date_key": start_dt.strftime("%Y-%m-%d"),
                "meetingUrl": meeting_url or "",
                "meetingProvider": meeting_provider or "",
                "rrule": None,
                # expandRecurrences hands back occurrences marked by recurrenceId.
                "recurring": any(item.get(k) for k in (
                    "recurrenceId", "recurrenceRule", "recurrenceRules", "recurrenceOverrides"
                )),
                "exdates": [],
            }

            multidays = expand_multiday_event(evt, window_start, window_end)
            events.extend(multidays)

        return {
            "name": name,
            "color": color,
            "type": "jmap",
            "writable": True,
            "events": events,
            "status": "ok",
            "count": len(events),
        }

    except urllib.error.HTTPError as e:
        # A stale session (moved apiUrl, new token) must not stick around.
        if cache_path:
            sync_cache_drop(cache_path)
        status_msg = f"auth_failed ({e.code})" if e.code in (401, 403) else f"http_error ({e.code})"
        return {
            "name": name,
            "color": color,
            "events": [],
            "status": status_msg,
            "count": 0,
        }
    except Exception as e:
        if cache_path:
            sync_cache_drop(cache_path)
        return {
            "name": name,
            "color": color,
            "events": [],
            "status": f"error: {str(e)}",
            "count": 0,
        }


def calendar_kind(cal_info):
    """
    The one place that decides which backend serves a calendars.json entry:
    "local", "jmap", "google", "caldav" (credentials, read and write), "ics"
    (a read-only feed), or None when the entry names no source.
    """
    if not isinstance(cal_info, dict):
        return None
    cal_type = str(cal_info.get("type", "")).lower()
    if cal_type == "local":
        return "local"
    if cal_type == "jmap" or "jmapToken" in cal_info:
        return "jmap"
    if cal_info.get("googleCalendarId") or (cal_info.get("calendarId") and not cal_info.get("url")):
        return "google"
    if has_caldav_write(cal_info):
        return "caldav"
    if str(cal_info.get("url") or "").strip():
        return "ics"
    return None


def fetch_calendar_item(cal_info, window_start, window_end):
    fetcher = {
        "local": fetch_local_calendar,
        "jmap": fetch_jmap_calendar,
        "google": fetch_google_api_calendar,
        "caldav": fetch_caldav_calendar,
    }.get(calendar_kind(cal_info), fetch_calendar)
    return fetcher(cal_info, window_start, window_end)


def purge_plugin_data():
    """
    Securely removes token-bearing configuration, OAuth credentials, and cached state:
    - CONFIG_PATH (~/.config/omarchy/calendars.json)
    - AUTH_FILE (~/.local/state/omarchy/google-auth.json)
    - OUTPUT_PATH (~/.local/state/omarchy/calendar-events.json)
    - TRANSLATION_CACHE_PATH (~/.local/state/omarchy/translation-cache.json)
    - LOCAL_EVENTS_PATH (~/.local/state/omarchy/local-events.json)
    - SYNC_CACHE_DIR (~/.local/state/omarchy/sync-cache/)
    """
    removed = []
    errors = []
    targets = [
        CONFIG_PATH,
        AUTH_FILE,
        OUTPUT_PATH,
        TRANSLATION_CACHE_PATH,
        LOCAL_EVENTS_PATH,
    ]
    for target in targets:
        try:
            if os.path.exists(target) or os.path.islink(target):
                os.unlink(target)
                removed.append(target)
        except Exception as exc:
            errors.append(f"{target}: {exc}")

    if _secret_tool(["search", "--all", "application", KEYRING_APP]):
        if _secret_tool(["clear", "application", KEYRING_APP]) is not None:
            removed.append("keyring: application=" + KEYRING_APP)

    try:
        if os.path.isdir(SYNC_CACHE_DIR) and not os.path.islink(SYNC_CACHE_DIR):
            shutil.rmtree(SYNC_CACHE_DIR)
            removed.append(SYNC_CACHE_DIR)
    except Exception as exc:
        errors.append(f"{SYNC_CACHE_DIR}: {exc}")

    try:
        if os.path.exists(STATE_DIR) and not os.listdir(STATE_DIR):
            os.rmdir(STATE_DIR)
            removed.append(STATE_DIR)
    except Exception:
        pass

    return {
        "status": "success" if not errors else "partial",
        "removed": removed,
        "errors": errors,
    }


def format_duration_iso(seconds):
    """Format duration in seconds to ISO 8601 duration (e.g. PT1H, PT30M)."""
    if seconds <= 0:
        return "PT0S"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    res = "PT"
    if hours > 0:
        res += f"{hours}H"
    if minutes > 0:
        res += f"{minutes}M"
    if secs > 0 or res == "PT":
        res += f"{secs}S"
    return res


def get_local_tz_name():
    """Detect local IANA timezone name."""
    try:
        if os.path.exists("/etc/localtime") and os.path.islink("/etc/localtime"):
            target = os.readlink("/etc/localtime")
            parts = target.split("zoneinfo/")
            if len(parts) > 1:
                return parts[1]
    except Exception:
        pass
    try:
        return time.tzname[0]
    except Exception:
        return "UTC"


def parse_local_timestamp(val_str):
    """Parse an ISO 8601 or local timestamp string into datetime, or None."""
    clean_str = str(val_str or "").strip()
    if not clean_str:
        return None
    if clean_str.endswith("Z"):
        clean_str = clean_str[:-1]
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(clean_str, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(clean_str)
    except ValueError:
        return None


def parse_iso_or_local(val_str):
    """Parse a stored timestamp, falling back to now for unreadable values."""
    dt = parse_local_timestamp(val_str)
    return dt if dt is not None else datetime.now()


def validate_event_times(event_data):
    """Reject unreadable start/end values instead of silently booking "now"."""
    parsed = {}
    for key in ("start", "end"):
        raw = str(event_data.get(key) or "").strip()
        if raw:
            parsed[key] = parse_local_timestamp(raw)
            if parsed[key] is None:
                raise ValueError(f"Invalid event {key} '{raw}': use a time like 14:30 or 2:30pm")
    if (not event_data.get("allDay") and parsed.get("start") is not None
            and parsed.get("end") is not None and parsed["end"] <= parsed["start"]):
        raise ValueError("The end time must be after the start time")


def fetch_local_calendar(cal_info, window_start, window_end):
    """Fetch events stored locally in ~/.local/state/omarchy/local-events.json"""
    name = cal_info.get("name", "Local Calendar")
    color = cal_info.get("color", "#a6e3a1")
    try:
        raw_events = safe_load_json(LOCAL_EVENTS_PATH, max_bytes=MAX_OUTPUT_JSON_BYTES) or []
        if not isinstance(raw_events, list):
            raw_events = []

        auto_translate = cal_info.get("translateKorean", False)
        events = []

        for item in raw_events:
            title = item.get("title") or "(Untitled Event)"
            description = item.get("description") or ""
            location = item.get("location") or ""
            all_day = bool(item.get("allDay", False))

            if auto_translate:
                title = translate_korean_to_english(title)
                location = translate_korean_to_english(location)

            meeting_url, meeting_provider = extract_meeting_info(location, description, title)

            start_str = item.get("start")
            if not start_str:
                continue

            start_dt = parse_iso_or_local(start_str)
            end_str = item.get("end")
            if end_str:
                end_dt = parse_iso_or_local(end_str)
            elif all_day:
                end_dt = start_dt + timedelta(days=1)
            else:
                end_dt = start_dt + timedelta(hours=1)

            evt = {
                "id": str(item.get("id", f"local_{int(start_dt.timestamp())}")),
                "title": title,
                "location": location,
                "description": description,
                "calendar": name,
                "calendarId": "local",
                "calendarType": "local",
                "writable": True,
                "color": color,
                "all_day": all_day,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "date_key": start_dt.strftime("%Y-%m-%d"),
                "meetingUrl": meeting_url or "",
                "meetingProvider": meeting_provider or "",
                "rrule": None,
                "exdates": [],
            }

            multidays = expand_multiday_event(evt, window_start, window_end)
            for inst in multidays:
                inst_dt = datetime.strptime(inst["date_key"], "%Y-%m-%d")
                if window_start <= inst_dt <= window_end:
                    events.append(inst)

        return {
            "name": name,
            "color": color,
            "type": "local",
            "writable": True,
            "events": events,
            "status": "ok",
            "count": len(events),
        }
    except Exception as e:
        return {
            "name": name,
            "color": color,
            "type": "local",
            "writable": True,
            "events": [],
            "status": f"error: {str(e)}",
            "count": 0,
        }


def create_local_event(cal_info, event_data):
    """Create a local event in ~/.local/state/omarchy/local-events.json"""
    events = safe_load_json(LOCAL_EVENTS_PATH, max_bytes=MAX_OUTPUT_JSON_BYTES) or []
    if not isinstance(events, list):
        events = []

    title = str(event_data.get("title", "")).strip() or "(Untitled Event)"
    location = str(event_data.get("location", "")).strip()
    description = str(event_data.get("description", "")).strip()
    all_day = bool(event_data.get("allDay", False))
    start_str = str(event_data.get("start", "")).strip()
    end_str = str(event_data.get("end", "")).strip()

    if not start_str:
        raise ValueError("Event must have a start date/time")

    event_id = f"loc_{int(time.time())}_{secrets.token_hex(4)}"
    new_evt = {
        "id": event_id,
        "title": title,
        "start": start_str,
        "end": end_str,
        "allDay": all_day,
        "location": location,
        "description": description,
        "calendar": cal_info.get("name", "Local Calendar"),
        "createdAt": int(time.time()),
    }
    events.append(new_evt)
    write_secure_json(LOCAL_EVENTS_PATH, events, mode=0o600, max_bytes=MAX_OUTPUT_JSON_BYTES)
    return {"status": "success", "id": event_id, "event": new_evt}


def delete_local_event(cal_info, event_id):
    """Delete a local event from ~/.local/state/omarchy/local-events.json"""
    events = safe_load_json(LOCAL_EVENTS_PATH, max_bytes=MAX_OUTPUT_JSON_BYTES) or []
    if not isinstance(events, list):
        events = []

    filtered = [e for e in events if str(e.get("id")) != str(event_id)]
    if len(filtered) == len(events):
        return {"status": "error", "message": f"Event '{event_id}' not found in local calendar"}

    write_secure_json(LOCAL_EVENTS_PATH, filtered, mode=0o600, max_bytes=MAX_OUTPUT_JSON_BYTES)
    return {"status": "success", "id": event_id}


def update_local_event(cal_info, event_id, event_data):
    """Rewrite the form fields of a local event in place, keeping its id."""
    events = safe_load_json(LOCAL_EVENTS_PATH, max_bytes=MAX_OUTPUT_JSON_BYTES) or []
    if not isinstance(events, list):
        events = []

    start_str = str(event_data.get("start", "")).strip()
    if not start_str:
        raise ValueError("Event must have a start date/time")

    for evt in events:
        if isinstance(evt, dict) and str(evt.get("id")) == str(event_id):
            evt.update({
                "title": str(event_data.get("title", "")).strip() or "(Untitled Event)",
                "start": start_str,
                "end": str(event_data.get("end", "")).strip(),
                "allDay": bool(event_data.get("allDay", False)),
                "location": str(event_data.get("location", "")).strip(),
                "description": str(event_data.get("description", "")).strip(),
                "updatedAt": int(time.time()),
            })
            write_secure_json(LOCAL_EVENTS_PATH, events, mode=0o600, max_bytes=MAX_OUTPUT_JSON_BYTES)
            return {"status": "success", "id": event_id, "event": evt}

    return {"status": "error", "message": f"Event '{event_id}' not found in local calendar"}


def google_events_url(cal_info):
    """Events collection URL of a configured Google calendar plus a fresh access token."""
    cal_id = cal_info.get("googleCalendarId") or cal_info.get("calendarId")
    if not cal_id:
        raise ValueError("Google calendar has no calendar ID configured")

    access_token = get_google_access_token()
    if not access_token:
        auth_state = _LAST_GOOGLE_AUTH["state"]
        if auth_state == "revoked":
            raise ValueError("Google login expired or revoked: reconnect Google in Settings")
        if auth_state == "error":
            raise ValueError(f"Google token refresh failed: {_LAST_GOOGLE_AUTH['detail']}")
        raise ValueError("Google authentication required: run google-auth.py")

    encoded_cal_id = urllib.parse.quote(cal_id, safe="")
    return f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal_id}/events", access_token


def google_request(method, url, access_token, body=None):
    """One Google Calendar API call; returns the decoded JSON reply (or {})."""
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
    return json.loads(raw.decode("utf-8")) if raw.strip() else {}


def google_event_body(event_data, patch=False):
    """
    Google event resource for the panel's form fields.

    A PATCH merges nested objects, so switching between timed and all-day has to
    null out the other form of start/end, and emptied text fields are sent as ""
    so they are cleared rather than left as they were.
    """
    title = str(event_data.get("title", "")).strip() or "(Untitled Event)"
    location = str(event_data.get("location", "")).strip()
    description = str(event_data.get("description", "")).strip()
    all_day = bool(event_data.get("allDay", False))

    start_val = str(event_data.get("start", "")).strip()
    end_val = str(event_data.get("end", "")).strip()
    if not start_val:
        raise ValueError("Event must have a start date/time")

    body = {"summary": title}
    if description or patch:
        body["description"] = description
    if location or patch:
        body["location"] = location

    if all_day:
        d_start = start_val[:10]
        d_end = end_val[:10] if end_val else d_start
        try:
            end_dt = datetime.strptime(d_end, "%Y-%m-%d") + timedelta(days=1)
            end_date_str = end_dt.strftime("%Y-%m-%d")
        except Exception:
            end_date_str = d_start
        body["start"] = {"date": d_start}
        body["end"] = {"date": end_date_str}
        if patch:
            for key in ("start", "end"):
                body[key].update({"dateTime": None, "timeZone": None})
    else:
        start_dt = parse_iso_or_local(start_val)
        if end_val:
            end_dt = parse_iso_or_local(end_val)
        else:
            end_dt = start_dt + timedelta(hours=1)

        body["start"] = {"dateTime": start_dt.astimezone().isoformat()}
        body["end"] = {"dateTime": end_dt.astimezone().isoformat()}
        tz_name = get_local_tz_name()
        if tz_name:
            body["start"]["timeZone"] = tz_name
            body["end"]["timeZone"] = tz_name
        if patch:
            for key in ("start", "end"):
                body[key]["date"] = None
    return body


def create_google_event(cal_info, event_data):
    """Create an event on Google Calendar using Google Calendar API v3."""
    url, access_token = google_events_url(cal_info)
    body = google_event_body(event_data)
    created_data = google_request("POST", url, access_token, body)
    return {"status": "success", "id": created_data.get("id"), "event": created_data}


def update_google_event(cal_info, event_id, event_data):
    """
    Patch an event on Google Calendar. Events are fetched with singleEvents, so
    the id of a recurring occurrence changes that occurrence only.
    """
    url, access_token = google_events_url(cal_info)
    body = google_event_body(event_data, patch=True)
    encoded_evt_id = urllib.parse.quote(str(event_id), safe="")
    updated = google_request("PATCH", f"{url}/{encoded_evt_id}", access_token, body)
    return {"status": "success", "id": updated.get("id", event_id), "event": updated}


def delete_google_event(cal_info, event_id):
    """Delete an event from Google Calendar API v3."""
    url, access_token = google_events_url(cal_info)
    encoded_evt_id = urllib.parse.quote(str(event_id), safe="")
    google_request("DELETE", f"{url}/{encoded_evt_id}", access_token)
    return {"status": "success", "id": event_id}


def jmap_session(cal_info, cached=None):
    """
    Discover the JMAP API endpoint and calendar account of an entry. `cached`
    (a sync-cache dict) holds a discovery less than a day old, which is reused,
    and receives a new one otherwise.
    """
    token = (cal_info.get("jmapToken") or cal_info.get("token") or cal_info.get("bearerToken") or "").strip()
    if not token:
        raise ValueError("No JMAP bearer token configured")
    session_url, trusted_origin = validate_jmap_https_url(jmap_session_url(cal_info))
    opener = urllib.request.build_opener(JmapSameOriginRedirectHandler(trusted_origin))
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    session = {"opener": opener, "headers": headers, "origin": trusted_origin}

    if cached and cached.get("apiUrl") and cached.get("accountId") and time.time() - cached.get("sessionAt", 0) < 86400:
        api_url, _ = validate_jmap_https_url(cached["apiUrl"], trusted_origin)
        session.update(api_url=api_url, account_id=cached["accountId"], using=cached["using"])
        return session

    req = urllib.request.Request(session_url, headers=headers, method="GET")
    with open_trusted_jmap(opener, req, trusted_origin, timeout=12) as resp:
        raw_session = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        session_data = json.loads(raw_session.decode("utf-8"))

    api_url = session_data.get("apiUrl")
    if not api_url:
        raise ValueError("no apiUrl in JMAP session response")
    api_url, _ = validate_jmap_https_url(api_url, trusted_origin)

    accounts = session_data.get("accounts", {})
    primary_accounts = session_data.get("primaryAccounts", {})
    account_id = primary_accounts.get("urn:ietf:params:jmap:calendars")
    if not account_id:
        for acc_id, acc_val in accounts.items():
            caps = acc_val.get("accountCapabilities", {})
            if any("calendar" in k.lower() for k in caps.keys()):
                account_id = acc_id
                break
    if not account_id and accounts:
        account_id = next(iter(accounts.keys()))
    if not account_id:
        raise ValueError("no JMAP calendar account found")

    jmap_using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]
    for cap in session_data.get("capabilities", {}):
        if "calendar" in cap.lower() and cap not in jmap_using:
            jmap_using.append(cap)

    if cached is not None:
        cached.clear()
        cached.update(apiUrl=api_url, accountId=account_id, using=jmap_using, sessionAt=time.time())
    session.update(api_url=api_url, account_id=account_id, using=jmap_using)
    return session


def jmap_session_url(cal_info):
    """The entry's JMAP session URL; a bare host means its /.well-known/jmap."""
    session_url = (cal_info.get("jmapUrl") or cal_info.get("sessionUrl") or cal_info.get("url") or "").strip()
    if not session_url:
        session_url = "https://api.fastmail.com/jmap/session"
    elif "://" not in session_url:
        session_url = "https://" + session_url
    parsed = urllib.parse.urlsplit(session_url)
    if not parsed.path or parsed.path == "/":
        session_url = urllib.parse.urlunsplit(parsed._replace(path="/.well-known/jmap"))
    return session_url


def jmap_event_set(session, call_id, **changes):
    """Run one CalendarEvent/set call; returns its response arguments."""
    payload = {
        "using": session["using"],
        "methodCalls": [
            ["CalendarEvent/set", dict(accountId=session["account_id"], **changes), call_id],
        ],
    }
    post_req = urllib.request.Request(
        session["api_url"], data=json.dumps(payload).encode("utf-8"),
        headers=session["headers"], method="POST",
    )
    with open_trusted_jmap(session["opener"], post_req, session["origin"], timeout=15) as resp:
        raw_data = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        response_data = json.loads(raw_data.decode("utf-8"))

    for resp_name, resp_args, _ in response_data.get("methodResponses", []):
        if resp_name == "CalendarEvent/set":
            return resp_args
        if resp_name == "error":
            raise ValueError(f"JMAP error: {resp_args.get('type')}")
    return {}


def jmap_set_error(entry):
    return entry.get("description") or entry.get("type")


def jmap_event_fields(event_data):
    """
    JSCalendar properties for the panel's form fields. None marks a property the
    event must not have (a removed location, a time zone on an all-day event).
    """
    title = str(event_data.get("title", "")).strip() or "(Untitled Event)"
    location = str(event_data.get("location", "")).strip()
    description = str(event_data.get("description", "")).strip()
    all_day = bool(event_data.get("allDay", False))

    start_val = str(event_data.get("start", "")).strip()
    end_val = str(event_data.get("end", "")).strip()
    if not start_val:
        raise ValueError("Event must have a start date/time")

    fields = {
        "title": title,
        "description": description,
        "showWithoutTime": all_day,
        "locations": {"loc1": {"@type": "Location", "name": location}} if location else None,
    }

    if all_day:
        fields["start"] = start_val[:10]
        fields["duration"] = "P1D"
        fields["timeZone"] = None
    else:
        start_dt = parse_iso_or_local(start_val)
        if end_val:
            end_dt = parse_iso_or_local(end_val)
        else:
            end_dt = start_dt + timedelta(hours=1)
        dur_seconds = max(60, int((end_dt - start_dt).total_seconds()))
        fields["start"] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        fields["duration"] = format_duration_iso(dur_seconds)
        fields["timeZone"] = get_local_tz_name() or None
    return fields


def create_jmap_event(cal_info, event_data):
    """Create an event on a JMAP server (RFC 8620, RFC 9670, RFC 8984 JSCalendar)."""
    session = jmap_session(cal_info)
    jsevent = {"@type": "Event"}
    jsevent.update({k: v for k, v in jmap_event_fields(event_data).items() if v is not None})

    cal_id = cal_info.get("jmapCalendarId") or cal_info.get("calendarId")
    if cal_id and cal_id != "primary":
        jsevent["calendarIds"] = {cal_id: True}

    creation_id = f"c_{secrets.token_hex(4)}"
    result = jmap_event_set(session, "set0", create={creation_id: jsevent})
    if creation_id in result.get("created", {}):
        created_evt = result["created"][creation_id]
        return {"status": "success", "id": created_evt.get("id"), "event": created_evt}
    if creation_id in result.get("notCreated", {}):
        raise ValueError(f"JMAP event creation rejected: {jmap_set_error(result['notCreated'][creation_id])}")
    return {"status": "success", "id": creation_id}


def update_jmap_event(cal_info, event_id, event_data):
    """Replace the form-editable properties of a JMAP event (CalendarEvent/set update)."""
    session = jmap_session(cal_info)
    event_id = str(event_id)
    result = jmap_event_set(session, "upd0", update={event_id: jmap_event_fields(event_data)})
    if event_id in result.get("notUpdated", {}):
        raise ValueError(f"JMAP event update rejected: {jmap_set_error(result['notUpdated'][event_id])}")
    return {"status": "success", "id": event_id}


def delete_jmap_event(cal_info, event_id):
    """Delete an event on a JMAP server using CalendarEvent/set destroy."""
    session = jmap_session(cal_info)
    event_id = str(event_id)
    result = jmap_event_set(session, "del0", destroy=[event_id])
    if event_id in result.get("notDestroyed", {}):
        raise ValueError(f"JMAP event deletion rejected: {jmap_set_error(result['notDestroyed'][event_id])}")
    return {"status": "success", "id": event_id}


# ---- CalDAV (RFC 4791) push -------------------------------------------------
# Reading an iCloud / Nextcloud / Radicale calendar only needs its published
# .ics feed, which is anonymous and read-only. Writing needs the real collection
# URL plus credentials, so an entry becomes a push target only once "caldavUrl",
# "username" and "password" are all set. Apple wants an app-specific password
# there (appleid.apple.com), never the Apple ID password itself.

CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CALDAV_PRODID = "-//Omarchy//Chronica//EN"
CALDAV_DEFAULT_HOST = "https://caldav.icloud.com/"


def has_caldav_write(cal_info):
    """True when a calendar entry carries everything needed to push events."""
    if not isinstance(cal_info, dict):
        return False
    return bool(
        str(cal_info.get("caldavUrl") or "").strip()
        and str(cal_info.get("username") or "").strip()
        and str(cal_info.get("password") or "")
    )


def caldav_auth(cal_info, raw_url):
    """Validate one account + URL into (url, origin, authorization header)."""
    user = str(cal_info.get("username") or "").strip()
    password = str(cal_info.get("password") or "")
    if not user or not password:
        raise ValueError(
            f"Calendar '{cal_info.get('name')}' has no CalDAV credentials: set "
            '"username" and "password" on it to push events.'
        )
    # Basic auth is one header line, and ":" separates its two halves, so a
    # crafted config must not be able to smuggle either past the boundary.
    if ":" in user or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in user + password):
        raise ValueError("CalDAV username or password contains an invalid character")

    url = str(raw_url or "").strip()
    if "://" not in url:
        url = "https://" + url
    url, origin = validate_jmap_https_url(url, label="CalDAV")
    if not url.endswith("/"):
        url += "/"
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return url, origin, "Basic " + token


def caldav_credentials(cal_info):
    """Same as caldav_auth, against the configured collection URL."""
    raw_url = str(cal_info.get("caldavUrl") or "").strip()
    if not raw_url:
        raise ValueError(
            f"Calendar '{cal_info.get('name')}' is a read-only subscription feed. "
            'Set "caldavUrl" (run --caldav-discover to list yours) to push events to it.'
        )
    return caldav_auth(cal_info, raw_url)


def caldav_request(origin, auth_header, method, url, body=None, headers=None):
    """One authenticated CalDAV request, pinned to the account's own origin; returns (body, ETag)."""
    url, _ = validate_jmap_https_url(url, origin, label="CalDAV")
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Authorization", auth_header)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    opener = urllib.request.build_opener(JmapSameOriginRedirectHandler(origin))
    with open_trusted_jmap(opener, req, origin, timeout=15) as resp:
        raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        resp_headers = getattr(resp, "headers", None)
        etag = resp_headers.get("ETag") if resp_headers is not None else None
        return raw.decode("utf-8", errors="ignore"), etag


def caldav_error(exc, cal_info):
    """Turn an HTTP failure into something a panel user can act on."""
    name = cal_info.get("name", "CalDAV calendar")
    if exc.code in (401, 403):
        return (f"{name} rejected the credentials. Apple iCloud needs an app-specific "
                "password from appleid.apple.com, not your Apple ID password.")
    if exc.code == 404:
        return f'{name}: CalDAV collection not found - check "caldavUrl".'
    if exc.code == 507:
        return f"{name}: the server has no storage left for this calendar."
    return f"{name}: CalDAV server returned HTTP {exc.code}."


def ics_escape(value):
    """Escape a text value for an iCalendar content line (RFC 5545 3.3.11)."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def ics_fold(line):
    """Fold a content line to the 75-octet limit, never mid-character."""
    if len(line.encode("utf-8")) <= 75:
        return line
    folded = []
    current = ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            folded.append(current)
            current = " " + char
        else:
            current += char
    folded.append(current)
    return "\r\n".join(folded)


def ics_utc_stamp(dt):
    """Naive local (or aware) datetime -> iCalendar UTC stamp, DST included."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def vevent_form_lines(event_data):
    """The VEVENT properties the panel's form owns, in serialization order."""
    start = parse_iso_or_local(event_data.get("start"))
    end = parse_iso_or_local(event_data.get("end") or event_data.get("start"))

    if bool(event_data.get("allDay", False)):
        # DTEND is exclusive for DATE values while the panel sends the last day
        # the event covers, so the stored end is always one day further out.
        if end.date() < start.date():
            end = start
        end = end + timedelta(days=1)
        when = [
            "DTSTART;VALUE=DATE:" + start.strftime("%Y%m%d"),
            "DTEND;VALUE=DATE:" + end.strftime("%Y%m%d"),
        ]
    else:
        if end <= start:
            end = start + timedelta(hours=1)
        # UTC stamps keep the event unambiguous without shipping a VTIMEZONE.
        when = ["DTSTART:" + ics_utc_stamp(start), "DTEND:" + ics_utc_stamp(end)]

    title = str(event_data.get("title", "")).strip() or "(Untitled Event)"
    lines = [
        "DTSTAMP:" + ics_utc_stamp(datetime.now(timezone.utc)),
        "SUMMARY:" + ics_escape(title),
    ]
    lines.extend(when)
    location = ics_escape(str(event_data.get("location", "")).strip())
    description = ics_escape(str(event_data.get("description", "")).strip())
    if location:
        lines.append("LOCATION:" + location)
    if description:
        lines.append("DESCRIPTION:" + description)
    return lines


def build_vevent(event_data, uid):
    """Serialize one event as a single-VEVENT iCalendar object."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + CALDAV_PRODID,
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        "UID:" + uid,
    ]
    lines.extend(vevent_form_lines(event_data))
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


# Properties an edit replaces. Everything else (UID, alarms, attendees,
# categories, the organizer...) is carried over untouched. Apple's structured
# location is derived from LOCATION, so it would go stale and is dropped.
VEVENT_EDITED_PROPS = {
    "DTSTAMP", "SUMMARY", "DTSTART", "DTEND", "DURATION", "LOCATION",
    "DESCRIPTION", "SEQUENCE", "LAST-MODIFIED", "X-APPLE-STRUCTURED-LOCATION",
}
VEVENT_RECURRENCE_PROPS = {"RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"}


def ics_prop_name(line):
    match = re.match(r"[A-Za-z0-9-]+", line)
    return match.group(0).upper() if match else ""


def rewrite_vevent(ics_text, event_data):
    """Apply the form fields to an existing single-VEVENT calendar object."""
    lines = unfold_lines(ics_text)
    if sum(1 for line in lines if line.strip().upper() == "BEGIN:VEVENT") != 1:
        raise ValueError("Recurring events with exceptions cannot be edited from the panel yet")

    out = []
    depth = 0  # 1 directly inside the VEVENT, deeper inside its VALARMs
    sequence = 0
    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT" or (depth and upper.startswith("BEGIN:")):
            depth += 1
            out.append(line)
            if depth == 1:
                out.extend(vevent_form_lines(event_data))
            continue
        if depth and upper.startswith("END:"):
            depth -= 1
            if depth == 0:
                out.append(f"SEQUENCE:{sequence + 1}")
                out.append("LAST-MODIFIED:" + ics_utc_stamp(datetime.now(timezone.utc)))
        elif depth == 1:
            name = ics_prop_name(line)
            if name in VEVENT_RECURRENCE_PROPS:
                raise ValueError("Recurring events cannot be edited from the panel yet")
            if name == "SEQUENCE":
                sequence = safe_int_param(line.split(":", 1)[-1], 0)
            if name in VEVENT_EDITED_PROPS:
                continue
        out.append(line)
    return "\r\n".join(ics_fold(line) for line in out) + "\r\n"


def create_caldav_event(cal_info, event_data):
    """Create an event on a CalDAV collection (Apple iCloud, Nextcloud, ...)."""
    if not str(event_data.get("start", "")).strip():
        raise ValueError("Event must have a start date/time")

    url, origin, auth = caldav_credentials(cal_info)
    uid = f"omarchy-{int(time.time())}-{secrets.token_hex(8)}"
    href = url + urllib.parse.quote(uid, safe="") + ".ics"
    try:
        caldav_request(
            origin, auth, "PUT", href, build_vevent(event_data, uid),
            headers={
                "Content-Type": "text/calendar; charset=utf-8",
                "If-None-Match": "*",
            },
        )
    except urllib.error.HTTPError as exc:
        raise ValueError(caldav_error(exc, cal_info)) from exc

    return {
        "status": "success",
        "id": uid,
        "event": {
            "id": uid,
            "title": str(event_data.get("title", "")).strip() or "(Untitled Event)",
            "start": str(event_data.get("start", "")),
            "end": str(event_data.get("end", "")),
            "allDay": bool(event_data.get("allDay", False)),
            "location": str(event_data.get("location", "")).strip(),
            "description": str(event_data.get("description", "")).strip(),
            "calendar": cal_info.get("name", "CalDAV Calendar"),
        },
    }


CALDAV_UID_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="{ns}">
  <d:prop><d:getetag/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:prop-filter name="UID"><c:text-match>{uid}</c:text-match></c:prop-filter>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


def caldav_find_href(url, origin, auth, uid):
    """Locate an event whose resource is not named after its UID."""
    body = CALDAV_UID_QUERY.format(ns=CALDAV_NS, uid=xml_escape(str(uid)))
    try:
        text, _ = caldav_request(
            origin, auth, "REPORT", url, body,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        tree = ET.fromstring(text)
    except (urllib.error.HTTPError, ET.ParseError):
        return None
    for href in tree.iter("{DAV:}href"):
        found = (href.text or "").strip()
        if found.lower().endswith(".ics"):
            return urllib.parse.urljoin(url, found)
    return None


CALDAV_RANGE_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="{ns}">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start}" end="{end}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


CALDAV_TAG_PROPS = '<cs:getctag xmlns:cs="http://calendarserver.org/ns/"/><d:sync-token/>'
CALDAV_TAG_NAMES = ("{http://calendarserver.org/ns/}getctag", "{DAV:}sync-token")


def fetch_caldav_calendar(cal_info, window_start, window_end):
    """
    Read a CalDAV collection with one calendar-query REPORT over the window.
    The server returns every resource with an occurrence in the window, each
    with its full iCalendar data (the master RRULE included), so the result
    goes through the same parser as an .ics feed. If the REPORT fails and the
    entry also has a published .ics URL, that feed is read instead.
    """
    name = cal_info.get("name", "Calendar")
    color = cal_info.get("color", "#4A90E2")
    try:
        url, origin, auth = caldav_credentials(cal_info)

        def utc(dt):
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # The collection's ctag / sync-token change whenever any event does:
        # one small PROPFIND decides whether the full REPORT is needed.
        cache_path = sync_cache_path("caldav", url, cal_info.get("username"))
        cached = sync_cache_load(cache_path)
        try:
            tags = caldav_propfind(url, origin, auth, CALDAV_TAG_PROPS)
            tag = "|".join((node.text or "").strip() for node in tags.iter()
                           if node.tag in CALDAV_TAG_NAMES and (node.text or "").strip())
        except Exception:
            tag = ""
        if (tag and cached.get("tag") == tag and isinstance(cached.get("content"), str)
                and cached.get("start", "~") <= window_start.isoformat()
                and cached.get("end", "") >= window_end.isoformat()):
            content = cached["content"]
        else:
            query_end = window_end + SYNC_REFRESH_MARGIN
            body = CALDAV_RANGE_QUERY.format(ns=CALDAV_NS, start=utc(window_start), end=utc(query_end))
            text, _ = caldav_request(
                origin, auth, "REPORT", url, body,
                headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
            )
            tree = ET.fromstring(text)
            content = "\r\n".join(
                node.text for node in tree.iter(f"{{{CALDAV_NS}}}calendar-data") if node.text
            )
            if tag:
                sync_cache_save(cache_path, {"tag": tag, "start": window_start.isoformat(),
                                             "end": query_end.isoformat(), "content": content})
        events = parse_ics(content, cal_info, window_start, window_end)
    except Exception as e:
        if str(cal_info.get("url") or "").strip():
            return fetch_calendar(cal_info, window_start, window_end)
        if isinstance(e, urllib.error.HTTPError):
            e = caldav_error(e, cal_info)
        return {"name": name, "color": color, "events": [], "status": f"error: {e}", "count": 0}
    return {
        "name": name,
        "color": color,
        "type": "caldav",
        "writable": True,
        "events": events,
        "status": "ok",
        "count": len(events),
    }


def delete_caldav_event(cal_info, event_id):
    """Delete an event from a CalDAV collection."""
    url, origin, auth = caldav_credentials(cal_info)
    href = url + urllib.parse.quote(str(event_id), safe="") + ".ics"
    try:
        caldav_request(origin, auth, "DELETE", href)
        return {"status": "success", "id": event_id}
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410):
            raise ValueError(caldav_error(exc, cal_info)) from exc

    # Events written by Apple Calendar itself live at a server-chosen href
    # rather than <uid>.ics, so fall back to asking the server where it is.
    found = caldav_find_href(url, origin, auth, event_id)
    if not found:
        name = cal_info.get("name", "the CalDAV calendar")
        return {"status": "error", "message": f"Event '{event_id}' not found on {name}"}
    try:
        caldav_request(origin, auth, "DELETE", found)
    except urllib.error.HTTPError as exc:
        raise ValueError(caldav_error(exc, cal_info)) from exc
    return {"status": "success", "id": event_id}


def update_caldav_event(cal_info, event_id, event_data):
    """
    Edit an event on a CalDAV collection: fetch its resource, replace the form
    fields, and PUT it back guarded by the ETag so a change made meanwhile on
    another device is reported instead of overwritten.
    """
    url, origin, auth = caldav_credentials(cal_info)
    name = cal_info.get("name", "the CalDAV calendar")
    href = url + urllib.parse.quote(str(event_id), safe="") + ".ics"
    try:
        try:
            current, etag = caldav_request(origin, auth, "GET", href)
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 410):
                raise
            href = caldav_find_href(url, origin, auth, event_id)
            if not href:
                return {"status": "error", "message": f"Event '{event_id}' not found on {name}"}
            current, etag = caldav_request(origin, auth, "GET", href)

        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        caldav_request(origin, auth, "PUT", href, rewrite_vevent(current, event_data), headers=headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise ValueError(f"{name}: the event was changed elsewhere meanwhile. Refresh and edit again.") from exc
        raise ValueError(caldav_error(exc, cal_info)) from exc
    return {"status": "success", "id": event_id}



CALDAV_PROPFIND = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="{ns}"><d:prop>{props}</d:prop></d:propfind>"""


def caldav_propfind(url, origin, auth, props, depth="0"):
    text, _ = caldav_request(
        origin, auth, "PROPFIND", url, CALDAV_PROPFIND.format(ns=CALDAV_NS, props=props),
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": depth},
    )
    return ET.fromstring(text)


def caldav_prop_href(tree, tag, base):
    """Absolute URL of the href nested inside the first matching property."""
    for node in tree.iter(tag):
        for href in node.iter("{DAV:}href"):
            if (href.text or "").strip():
                return urllib.parse.urljoin(base, href.text.strip())
    return None


def caldav_discover(cal_info):
    """List an account's calendar collections, for pasting into "caldavUrl"."""
    base = str(cal_info.get("caldavUrl") or "").strip() or CALDAV_DEFAULT_HOST
    url, origin, auth = caldav_auth(cal_info, base)
    try:
        principal = caldav_prop_href(
            caldav_propfind(url, origin, auth, "<d:current-user-principal/>"),
            "{DAV:}current-user-principal", url,
        )
        if not principal:
            raise ValueError("Server returned no user principal - check the username and password.")
        home = caldav_prop_href(
            caldav_propfind(principal, origin, auth, "<c:calendar-home-set/>"),
            "{%s}calendar-home-set" % CALDAV_NS, principal,
        )
        if not home:
            raise ValueError("Server returned no calendar home for this account.")
        tree = caldav_propfind(home, origin, auth, "<d:displayname/><d:resourcetype/>", depth="1")
    except urllib.error.HTTPError as exc:
        raise ValueError(caldav_error(exc, cal_info)) from exc

    collections = []
    for response in tree.iter("{DAV:}response"):
        if response.find(".//{DAV:}resourcetype/{%s}calendar" % CALDAV_NS) is None:
            continue
        href = response.find("{DAV:}href")
        if href is None or not (href.text or "").strip():
            continue
        name = response.find(".//{DAV:}displayname")
        collections.append({
            "name": (name.text or "").strip() if name is not None else "",
            "caldavUrl": urllib.parse.urljoin(home, href.text.strip()),
        })
    return collections


def find_calendar_config(cal_name_or_id):
    """Find calendar entry matching name or ID from config, or default to local."""
    ensure_config_exists()
    calendars = load_calendars()
    target = str(cal_name_or_id or "").strip().lower()

    if target in ("", "local", "local calendar"):
        for c in calendars:
            if str(c.get("type", "")).lower() == "local":
                return c
        return {"name": "Local Calendar", "type": "local", "color": "#a6e3a1", "enabled": True}

    for c in calendars:
        c_name = str(c.get("name", "")).strip().lower()
        c_gid = str(c.get("googleCalendarId", "")).strip().lower()
        c_id = str(c.get("calendarId", "")).strip().lower()
        if target in (c_name, c_gid, c_id):
            return c

    return {"name": cal_name_or_id or "Local Calendar", "type": "local", "color": "#a6e3a1", "enabled": True}


def create_event(event_data):
    """Dispatcher to create an event on the specified calendar."""
    validate_event_times(event_data)
    cal_target = event_data.get("calendar") or event_data.get("calendarId") or "local"
    cal_info = find_calendar_config(cal_target)
    kind = "local" if str(cal_target).lower() in ("local", "local calendar") else calendar_kind(cal_info)
    creator = {
        "local": create_local_event,
        "jmap": create_jmap_event,
        "caldav": create_caldav_event,
        "google": create_google_event,
    }.get(kind)
    if creator is None:
        raise ValueError(f"Calendar '{cal_info.get('name')}' is a read-only subscription feed and does not accept push events.")
    res = creator(cal_info, event_data)

    sync_all_events()
    return res


def resolve_event_target(data, action):
    """Event id, calendar entry and backend type for an edit or delete payload."""
    event_id = data.get("id")
    if not event_id:
        raise ValueError(f"Missing event ID for {action}")

    cal_target = data.get("calendar") or data.get("calendarId") or data.get("calendarType") or "local"
    cal_type = str(data.get("calendarType", "")).lower()
    cal_info = find_calendar_config(cal_target)

    if not cal_type:
        cal_type = calendar_kind(cal_info) or "local"

    if str(event_id).startswith("loc_") or str(event_id).startswith("local_"):
        cal_type = "local"
    if cal_type not in ("local", "jmap", "caldav", "google"):
        raise ValueError(f"Calendar '{cal_target}' does not support event {action} (read-only feed).")
    return event_id, cal_info, cal_type


def delete_event(delete_data):
    """Dispatcher to delete an event from the specified calendar."""
    event_id, cal_info, cal_type = resolve_event_target(delete_data, "deletion")
    res = {
        "local": delete_local_event,
        "jmap": delete_jmap_event,
        "caldav": delete_caldav_event,
        "google": delete_google_event,
    }[cal_type](cal_info, event_id)
    sync_all_events()
    return res


def update_event(event_data):
    """Dispatcher to edit an event on its own calendar (events never change calendar)."""
    event_id, cal_info, cal_type = resolve_event_target(event_data, "editing")
    validate_event_times(event_data)
    res = {
        "local": update_local_event,
        "jmap": update_jmap_event,
        "caldav": update_caldav_event,
        "google": update_google_event,
    }[cal_type](cal_info, event_id, event_data)
    sync_all_events()
    return res


# CLI flags the panel pipes a JSON event payload into on stdin.
EVENT_COMMANDS = {
    "--create-event": create_event,
    "--update-event": update_event,
    "--delete-event": delete_event,
}


def get_writable_calendars():
    """Returns a list of calendars configured or available for writing events."""
    ensure_config_exists()
    calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES) or []
    writables = []
    has_local = False

    for c in calendars:
        c_type = calendar_kind(c)
        if c_type == "local":
            has_local = True
            writables.append({
                "name": c.get("name", "Local Calendar"),
                "type": "local",
                "color": c.get("color", "#a6e3a1"),
                "calendarId": "local",
                "writable": True,
            })
        elif c_type == "jmap":
            writables.append({
                "name": c.get("name", "JMAP Calendar"),
                "type": "jmap",
                "color": c.get("color", "#ff7700"),
                "calendarId": c.get("jmapCalendarId") or c.get("calendarId") or "primary",
                "writable": True,
            })
        elif c_type == "caldav":
            writables.append({
                "name": c.get("name", "CalDAV Calendar"),
                "type": "caldav",
                "color": c.get("color", "#4A90E2"),
                "calendarId": c.get("caldavUrl", ""),
                "writable": True,
            })
        elif c_type == "google":
            writables.append({
                "name": c.get("name", "Google Calendar"),
                "type": "google",
                "color": c.get("color", "#4285f4"),
                "calendarId": c.get("googleCalendarId") or c.get("calendarId"),
                "writable": True,
            })

    if not has_local:
        writables.append({
            "name": "Local Calendar",
            "type": "local",
            "color": "#a6e3a1",
            "calendarId": "local",
            "writable": True,
        })

    return writables


def sync_all_events():
    """Fetch all configured and local calendars and write calendar-events.json."""
    ensure_config_exists()
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    load_translation_cache()
    migrate_secrets_to_keyring()
    calendars = load_calendars()

    now = datetime.now()
    window_start = now - timedelta(days=45)
    window_end = now + timedelta(days=90)

    enabled_cals = [
        c for c in calendars
        if isinstance(c, dict) and c.get("enabled", True) and calendar_kind(c)
    ]

    has_local = any(str(c.get("type", "")).lower() == "local" for c in enabled_cals)
    if not has_local and os.path.exists(LOCAL_EVENTS_PATH):
        try:
            local_evts = safe_load_json(LOCAL_EVENTS_PATH, max_bytes=MAX_OUTPUT_JSON_BYTES)
            if local_evts and len(local_evts) > 0:
                enabled_cals.append({
                    "name": "Local Calendar",
                    "type": "local",
                    "color": "#a6e3a1",
                    "enabled": True,
                })
        except Exception:
            pass

    all_events = []
    cal_statuses = []

    if enabled_cals:
        # Cap workers at 6 to avoid server-side throttling (e.g. on Apple iCloud CalDAV)
        max_workers = min(6, len(enabled_cals))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(fetch_calendar_item, c, window_start, window_end)
                for c in enabled_cals
            ]
            for f in futures:
                try:
                    res = f.result()
                    if isinstance(res, dict):
                        all_events.extend(res.get("events", []))
                        cal_statuses.append({
                            "name": res.get("name", "Calendar"),
                            "color": res.get("color", "#4A90E2"),
                            "type": res.get("type", "ical"),
                            "writable": bool(res.get("writable", False)),
                            "status": res.get("status", "ok"),
                            "count": res.get("count", len(res.get("events", []))),
                        })
                except Exception as exc:
                    cal_statuses.append({
                        "name": "Calendar",
                        "color": "#4A90E2",
                        "type": "ical",
                        "writable": False,
                        "status": f"error: {str(exc)}",
                        "count": 0,
                    })

    events_by_date = {}
    for evt in all_events:
        if not isinstance(evt, dict):
            continue
        d_key = str(evt.get("date_key") or "")
        if not d_key:
            continue
        if d_key not in events_by_date:
            events_by_date[d_key] = []

        start_dt = evt.get("start_dt")
        end_dt = evt.get("end_dt")

        if isinstance(start_dt, datetime):
            start_time_str = start_dt.strftime("%H:%M")
            start_iso = start_dt.isoformat()
        else:
            start_time_str = "00:00"
            start_iso = str(start_dt or "")

        if isinstance(end_dt, datetime):
            end_time_str = end_dt.strftime("%H:%M")
            end_iso = end_dt.isoformat()
        else:
            end_time_str = "00:00"
            end_iso = str(end_dt or "")

        is_all_day = bool(evt.get("all_day", False))
        writable = bool(evt.get("writable", False))
        # The form edits one day: a time range, or an all-day date. Longer spans
        # and CalDAV/JMAP series would be rewritten wrongly, so they stay read-only.
        editable = (
            writable
            and not (evt.get("rrule") or evt.get("recurring"))
            and isinstance(start_dt, datetime) and isinstance(end_dt, datetime)
            and (
                (end_dt.date() - start_dt.date()).days <= 1 if is_all_day
                else end_dt.date() == start_dt.date()
            )
        )

        events_by_date[d_key].append({
            "id": str(evt.get("id", "")),
            "title": str(evt.get("title") or "(Untitled Event)"),
            "calendar": str(evt.get("calendar") or "Calendar"),
            "calendarId": str(evt.get("calendarId", "")),
            "calendarType": str(evt.get("calendarType", "ical")),
            "writable": writable,
            "editable": editable,
            "recurring": bool(evt.get("rrule") or evt.get("recurring")),
            "description": str(evt.get("description") or ""),
            "color": str(evt.get("color") or "#4A90E2"),
            "allDay": is_all_day,
            "startTime": start_time_str if not is_all_day else "All Day",
            "endTime": end_time_str if not is_all_day else "",
            "location": str(evt.get("location") or ""),
            "startIso": start_iso,
            "endIso": end_iso,
            "meetingUrl": str(evt.get("meetingUrl") or ""),
            "meetingProvider": str(evt.get("meetingProvider") or ""),
        })

    for d_key in events_by_date:
        events_by_date[d_key].sort(
            key=lambda x: (
                0 if x.get("allDay") else 1,
                str(x.get("startTime") or ""),
                str(x.get("title") or "")
            )
        )

    google_auth = google_auth_summary()

    output_data = {
        "lastSynced": int(time.time()),
        "lastSyncedFormatted": now.strftime("%H:%M"),
        "totalEvents": len(all_events),
        "configuredCount": len(enabled_cals),
        "authenticated": google_auth["authenticated"],
        "googleAuth": google_auth,
        "calendars": cal_statuses,
        "eventsByDate": events_by_date,
    }

    try:
        write_secure_json(OUTPUT_PATH, output_data, mode=0o600, max_bytes=MAX_OUTPUT_JSON_BYTES)
    except Exception:
        pass
    save_translation_cache()

    return {
        "status": "success",
        "totalEvents": len(all_events),
        "calendars": len(cal_statuses),
    }


def read_stdin_payload(max_bytes=MAX_CONFIG_BYTES):
    """Read JSON payload from stdin safely without blocking or deadlock."""
    try:
        line = sys.stdin.readline()
        if line and line.strip():
            return line
    except Exception:
        pass
    try:
        return sys.stdin.read(max_bytes + 1)
    except Exception:
        return ""


def save_config_command(new_config):
    if not isinstance(new_config, list):
        raise ValueError("Config must be a JSON array of calendar entries")
    save_calendars(new_config)
    return {"status": "success"}


def _event_command(handler):
    def run(payload):
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")
        return handler(payload)
    return run


SERVE_COMMANDS = {
    "sync": lambda payload: sync_all_events(),
    "save-config": save_config_command,
    "create-event": _event_command(create_event),
    "update-event": _event_command(update_event),
    "delete-event": _event_command(delete_event),
}


def serve(stdin=None, stdout=None):
    """
    --serve: one long-lived backend for the panel, instead of one process per
    call. Reads one JSON request per line, {"id": 1, "cmd": "sync",
    "payload": ...}, and answers each with one line, {"id": 1, "result": ...},
    in order. One process runs every call in turn, so two writes never race,
    and no call pays Python's start-up. It exits when stdin closes, which is
    when the shell stops or restarts.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    ensure_config_exists()
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    for line in stdin:
        if not line.strip():
            continue
        req_id = None
        try:
            if len(line) > MAX_CONFIG_BYTES:
                raise ValueError(f"Request exceeds maximum size of {MAX_CONFIG_BYTES} bytes")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            req_id = request.get("id")
            handler = SERVE_COMMANDS.get(request.get("cmd"))
            if handler is None:
                raise ValueError(f"Unknown command {request.get('cmd')!r}")
            result = handler(request.get("payload"))
        except Exception as e:
            result = {"status": "error", "message": str(e)}
        stdout.write(json.dumps({"id": req_id, "result": result}, ensure_ascii=False) + "\n")
        stdout.flush()


def main():
    try:
        if len(sys.argv) > 1:
            arg = sys.argv[1]
            if arg in ("--purge-data", "--purge-auth", "--cleanup", "--uninstall"):
                res = purge_plugin_data()
                print(json.dumps(res, indent=2))
                sys.exit(0 if res["status"] == "success" else 1)
            if arg == "--serve":
                serve()
                return

        ensure_config_exists()
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        load_translation_cache()

        if len(sys.argv) > 1:
            arg = sys.argv[1]
            if arg == "--save-config":
                try:
                    raw_input = sys.argv[2] if len(sys.argv) > 2 else read_stdin_payload(MAX_CONFIG_BYTES)
                    if len(raw_input) > MAX_CONFIG_BYTES:
                        raise ValueError(f"Config payload exceeds maximum size of {MAX_CONFIG_BYTES} bytes")
                    print(json.dumps(save_config_command(json.loads(raw_input))))
                    sys.exit(0)
                except Exception as e:
                    print(json.dumps({"status": "error", "message": str(e)}))
                    sys.exit(1)
            elif arg == "--get-config":
                ensure_config_exists()
                content = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES)
                print(json.dumps(content, ensure_ascii=False, indent=2))
                sys.exit(0)
            elif arg == "--auth-status":
                print(json.dumps(google_auth_summary()))
                sys.exit(0)
            elif arg in EVENT_COMMANDS:
                try:
                    raw_input = sys.argv[2] if len(sys.argv) > 2 else read_stdin_payload(MAX_CONFIG_BYTES)
                    if len(raw_input) > MAX_CONFIG_BYTES:
                        raise ValueError(f"Payload exceeds maximum size of {MAX_CONFIG_BYTES} bytes")
                    event_data = json.loads(raw_input)
                    if not isinstance(event_data, dict):
                        raise ValueError("Payload must be a JSON object")
                    res = EVENT_COMMANDS[arg](event_data)
                    print(json.dumps(res, ensure_ascii=False))
                    sys.exit(0 if res.get("status") == "success" else 1)
                except Exception as e:
                    print(json.dumps({"status": "error", "message": str(e)}))
                    sys.exit(1)
            elif arg == "--caldav-discover":
                try:
                    target = sys.argv[2] if len(sys.argv) > 2 else ""
                    print(json.dumps(caldav_discover(find_calendar_config(target)), ensure_ascii=False, indent=2))
                    sys.exit(0)
                except Exception as e:
                    print(json.dumps({"status": "error", "message": str(e)}))
                    sys.exit(1)
            elif arg == "--writable-calendars":
                try:
                    writables = get_writable_calendars()
                    print(json.dumps(writables, ensure_ascii=False, indent=2))
                    sys.exit(0)
                except Exception as e:
                    print(json.dumps({"status": "error", "message": str(e)}))
                    sys.exit(1)

        result = sync_all_events()
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(0)


if __name__ == "__main__":
    main()
