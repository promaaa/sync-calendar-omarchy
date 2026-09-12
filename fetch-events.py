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
import secrets
import stat
import time
import calendar
import shutil
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

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
    val = val.replace("\\n", "\n").replace("\\N", "\n")
    val = val.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
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


def parse_datetime_value(val_str, params=None):
    """
    Parse an iCal date or datetime string into local wall time.

    UTC values (trailing Z), explicit numeric offsets and TZID=... parameters are
    all converted to the system timezone. Floating values (no zone information)
    are kept as-is, per RFC 5545.
    Returns: (is_all_day: bool, dt: datetime)
    """
    val_str = val_str.strip()
    if params and any("VALUE=DATE" in p.upper() for p in params):
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

    # Capture the zone marker before it is stripped off for parsing
    is_utc = val_str.endswith("Z")
    offset_match = re.search(r"([+-])(\d\d):?(\d\d)$", val_str)

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

        if is_utc:
            return False, to_local_naive(dt, timezone.utc)
        if offset_match:
            sign = -1 if offset_match.group(1) == "-" else 1
            delta = timedelta(hours=int(offset_match.group(2)), minutes=int(offset_match.group(3)))
            return False, to_local_naive(dt, timezone(sign * delta))
        zone = resolve_timezone(extract_tzid(params))
        if zone is not None:
            return False, to_local_naive(dt, zone)
        return False, dt

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


def get_monthly_dates(year, month, start_dt, rrule):
    byday_str = rrule.get("BYDAY", "")
    bymonthday_str = rrule.get("BYMONTHDAY", "")
    bysetpos_str = rrule.get("BYSETPOS", "")
    num_days = calendar.monthrange(year, month)[1]

    if bymonthday_str:
        dates = []
        for mday_str in bymonthday_str.split(","):
            mday_str = mday_str.strip()
            if not mday_str:
                continue
            try:
                mday = int(mday_str)
                if mday < 0:
                    mday = num_days + 1 + mday
                if 1 <= mday <= num_days:
                    dates.append(datetime(year, month, mday, start_dt.hour, start_dt.minute, start_dt.second))
            except ValueError:
                pass
        return dates

    if byday_str:
        target_days = []
        bysetpos = int(bysetpos_str) if bysetpos_str and bysetpos_str.lstrip("-+").isdigit() else None

        for part in byday_str.split(","):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^([+-]?\d+)?([A-Za-z]{2})$", part)
            if m:
                ord_str, day_code = m.group(1), m.group(2).upper()
                if day_code in WEEKDAYS:
                    w_idx = WEEKDAYS.index(day_code)
                    ordinal = int(ord_str) if ord_str else (bysetpos if bysetpos is not None else None)

                    matching_days = [
                        d for d in range(1, num_days + 1)
                        if datetime(year, month, d).weekday() == w_idx
                    ]

                    if ordinal is not None:
                        if ordinal > 0 and ordinal <= len(matching_days):
                            target_days.append(matching_days[ordinal - 1])
                        elif ordinal < 0 and abs(ordinal) <= len(matching_days):
                            target_days.append(matching_days[ordinal])
                    else:
                        target_days.extend(matching_days)

        target_days = sorted(list(set(target_days)))
        return [datetime(year, month, d, start_dt.hour, start_dt.minute, start_dt.second) for d in target_days]

    day = start_dt.day
    if day <= num_days:
        return [datetime(year, month, day, start_dt.hour, start_dt.minute, start_dt.second)]
    return []


def expand_weekly(event, window_start, window_end, rrule, until_dt, max_count):
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    duration = end_dt - start_dt
    interval = max(1, safe_int_param(rrule.get("INTERVAL"), 1))
    byday_str = rrule.get("BYDAY", "")
    wkst_str = rrule.get("WKST", "MO").upper()
    wkst_idx = WEEKDAYS.index(wkst_str) if wkst_str in WEEKDAYS else 0

    if byday_str:
        target_weekdays = []
        for day_code in byday_str.split(","):
            code = day_code.strip()[-2:].upper()
            if code in WEEKDAYS:
                target_weekdays.append(WEEKDAYS.index(code))
        target_weekdays = sorted(list(set(target_weekdays)), key=lambda d: (d - wkst_idx) % 7)
    else:
        target_weekdays = [start_dt.weekday()]

    exdates = set(event.get("exdates", []))
    instances = []

    days_since_wkst = (start_dt.weekday() - wkst_idx) % 7
    week_start_date = (start_dt - timedelta(days=days_since_wkst)).date()

    count = 0
    cur_week_start = week_start_date
    has_count = bool(rrule.get("COUNT"))

    # Fast forward if event started long before window and has no fixed COUNT
    if not has_count and cur_week_start < (window_start - timedelta(weeks=interval)).date():
        weeks_behind = (window_start.date() - cur_week_start).days // 7
        if weeks_behind > 0:
            cur_week_start += timedelta(weeks=(weeks_behind // interval) * interval)

    iterations = 0
    while count < max_count and iterations < MAX_RECURRENCE_ITERATIONS and len(instances) < MAX_EXPANDED_INSTANCES:
        iterations += 1
        week_start_dt = datetime.combine(cur_week_start, datetime.min.time())
        if week_start_dt > window_end:
            break
        if until_dt and week_start_dt > until_dt:
            break

        for day_offset in range(7):
            cur_date = cur_week_start + timedelta(days=day_offset)
            weekday = cur_date.weekday()
            if weekday in target_weekdays:
                cur_dt = datetime.combine(cur_date, start_dt.time())
                if cur_dt < start_dt:
                    continue
                if until_dt and cur_dt > until_dt:
                    break

                count += 1
                date_key = cur_dt.strftime("%Y-%m-%d")
                if cur_dt >= window_start and cur_dt <= window_end and date_key not in exdates:
                    inst = dict(event)
                    inst["start_dt"] = cur_dt
                    inst["end_dt"] = cur_dt + duration
                    inst["date_key"] = date_key
                    instances.append(inst)

                if count >= max_count or len(instances) >= MAX_EXPANDED_INSTANCES:
                    break

        cur_week_start += timedelta(weeks=interval)

    return instances


def expand_daily(event, window_start, window_end, rrule, until_dt, max_count):
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    duration = end_dt - start_dt
    interval = max(1, safe_int_param(rrule.get("INTERVAL"), 1))
    byday_str = rrule.get("BYDAY", "")

    target_weekdays = None
    if byday_str:
        target_weekdays = []
        for day_code in byday_str.split(","):
            code = day_code.strip()[-2:].upper()
            if code in WEEKDAYS:
                target_weekdays.append(WEEKDAYS.index(code))

    exdates = set(event.get("exdates", []))
    instances = []
    cur_dt = start_dt
    count = 0
    has_count = bool(rrule.get("COUNT"))

    # Fast forward if event started long before window and has no fixed COUNT
    if not has_count and cur_dt < (window_start - timedelta(days=interval)):
        days_behind = (window_start.date() - cur_dt.date()).days
        if days_behind > 0:
            cur_dt += timedelta(days=(days_behind // interval) * interval)

    iterations = 0
    while count < max_count and cur_dt <= window_end and iterations < MAX_RECURRENCE_ITERATIONS and len(instances) < MAX_EXPANDED_INSTANCES:
        iterations += 1
        if until_dt and cur_dt > until_dt:
            break

        match = True
        if target_weekdays is not None:
            match = cur_dt.weekday() in target_weekdays

        if match:
            count += 1
            date_key = cur_dt.strftime("%Y-%m-%d")
            if cur_dt >= window_start and date_key not in exdates:
                inst = dict(event)
                inst["start_dt"] = cur_dt
                inst["end_dt"] = cur_dt + duration
                inst["date_key"] = date_key
                instances.append(inst)

        cur_dt += timedelta(days=interval)

    return instances


def expand_monthly(event, window_start, window_end, rrule, until_dt, max_count):
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    duration = end_dt - start_dt
    interval = max(1, safe_int_param(rrule.get("INTERVAL"), 1))
    exdates = set(event.get("exdates", []))

    instances = []
    cur_year = start_dt.year
    cur_month = start_dt.month
    count = 0
    has_count = bool(rrule.get("COUNT"))

    if not has_count and cur_year < window_start.year - 1:
        years_behind = window_start.year - 1 - cur_year
        cur_year += years_behind

    iterations = 0
    while count < max_count and iterations < MAX_RECURRENCE_ITERATIONS and len(instances) < MAX_EXPANDED_INSTANCES:
        iterations += 1
        month_start_dt = datetime(cur_year, cur_month, 1, 0, 0, 0)
        if until_dt and month_start_dt > until_dt:
            break
        if month_start_dt > window_end:
            break

        cand_dates = get_monthly_dates(cur_year, cur_month, start_dt, rrule)
        for cur_dt in cand_dates:
            if cur_dt < start_dt:
                continue
            if until_dt and cur_dt > until_dt:
                break
            count += 1
            date_key = cur_dt.strftime("%Y-%m-%d")
            if cur_dt >= window_start and cur_dt <= window_end and date_key not in exdates:
                inst = dict(event)
                inst["start_dt"] = cur_dt
                inst["end_dt"] = cur_dt + duration
                inst["date_key"] = date_key
                instances.append(inst)
            if count >= max_count or len(instances) >= MAX_EXPANDED_INSTANCES:
                break

        total_months = (cur_year * 12 + cur_month - 1) + interval
        cur_year = total_months // 12
        cur_month = (total_months % 12) + 1

    return instances


def expand_yearly(event, window_start, window_end, rrule, until_dt, max_count):
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    duration = end_dt - start_dt
    interval = max(1, safe_int_param(rrule.get("INTERVAL"), 1))
    exdates = set(event.get("exdates", []))
    bymonth_str = rrule.get("BYMONTH", "")

    target_months = []
    if bymonth_str:
        for m_str in bymonth_str.split(","):
            if m_str.strip().isdigit():
                m_val = int(m_str.strip())
                if 1 <= m_val <= 12:
                    target_months.append(m_val)
    if not target_months:
        target_months = [start_dt.month]

    instances = []
    cur_year = start_dt.year
    count = 0
    has_count = bool(rrule.get("COUNT"))

    if not has_count and cur_year < window_start.year - 1:
        years_behind = window_start.year - 1 - cur_year
        cur_year += (years_behind // interval) * interval

    iterations = 0
    while count < max_count and iterations < MAX_RECURRENCE_ITERATIONS and len(instances) < MAX_EXPANDED_INSTANCES:
        iterations += 1
        year_start_dt = datetime(cur_year, 1, 1, 0, 0, 0)
        if until_dt and year_start_dt > until_dt:
            break
        if year_start_dt > window_end:
            break

        for month in target_months:
            cand_dates = get_monthly_dates(cur_year, month, start_dt, rrule)
            for cur_dt in cand_dates:
                if cur_dt < start_dt:
                    continue
                if until_dt and cur_dt > until_dt:
                    break
                count += 1
                date_key = cur_dt.strftime("%Y-%m-%d")
                if cur_dt >= window_start and cur_dt <= window_end and date_key not in exdates:
                    inst = dict(event)
                    inst["start_dt"] = cur_dt
                    inst["end_dt"] = cur_dt + duration
                    inst["date_key"] = date_key
                    instances.append(inst)
                if count >= max_count or len(instances) >= MAX_EXPANDED_INSTANCES:
                    break

        cur_year += interval

    return instances


def expand_recurring_event(event, window_start, window_end):
    """
    Expands a recurring VEVENT within [window_start, window_end].
    Bounded by MAX_RECURRENCE_ITERATIONS and MAX_EXPANDED_INSTANCES.
    """
    rrule = event.get("rrule")
    if not rrule:
        return [event]

    freq = rrule.get("FREQ", "").upper()
    until_str = rrule.get("UNTIL")
    count_str = rrule.get("COUNT")

    until_dt = None
    if until_str:
        is_all_day_until, parsed_until = parse_datetime_value(until_str)
        if is_all_day_until:
            until_dt = datetime(parsed_until.year, parsed_until.month, parsed_until.day, 23, 59, 59)
        else:
            until_dt = parsed_until
        if until_dt < window_start:
            return []

    max_count = min(safe_int_param(count_str, 1000), 1000)

    start_dt = event["start_dt"]
    if freq == "WEEKLY":
        return expand_weekly(event, window_start, window_end, rrule, until_dt, max_count)
    elif freq == "DAILY":
        return expand_daily(event, window_start, window_end, rrule, until_dt, max_count)
    elif freq == "MONTHLY":
        return expand_monthly(event, window_start, window_end, rrule, until_dt, max_count)
    elif freq == "YEARLY":
        return expand_yearly(event, window_start, window_end, rrule, until_dt, max_count)
    else:
        if start_dt.strftime("%Y-%m-%d") not in event.get("exdates", []):
            if window_start <= start_dt <= window_end:
                return [event]
        return []


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
    in_vevent = False
    current = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_vevent = True
            current = {"exdates": []}
            continue
        elif line == "END:VEVENT":
            if in_vevent and "DTSTART" in current:
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
        elif prop_name == "DTEND":
            _, dt = parse_datetime_value(val_part, prop_params)
            current["DTEND"] = dt
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
        elif prop_name == "EXDATE":
            for ex_val in val_part.split(","):
                ex_val = ex_val.strip()
                if ex_val:
                    _, ex_dt = parse_datetime_value(ex_val, prop_params)
                    current["exdates"].append(ex_dt.strftime("%Y-%m-%d"))

    auto_translate = cal_info.get("translateKorean", False)
    normalized = []
    for raw in raw_events:
        start_dt = raw.get("DTSTART")
        if not start_dt:
            continue
        all_day = raw.get("all_day", False)
        end_dt = raw.get("DTEND", start_dt + (timedelta(days=1) if all_day else timedelta(hours=1)))
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
            "color": cal_info.get("color", "#4A90E2"),
            "all_day": all_day,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "date_key": start_dt.strftime("%Y-%m-%d"),
            "meetingUrl": meeting_url or "",
            "meetingProvider": meeting_provider or "",
            "rrule": raw.get("RRULE"),
            "exdates": raw.get("exdates", []),
        }

        if evt["rrule"]:
            expanded = expand_recurring_event(evt, window_start, window_end)
            for rec_inst in expanded:
                multidays = expand_multiday_event(rec_inst, window_start, window_end)
                normalized.extend(multidays)
        else:
            if start_dt.strftime("%Y-%m-%d") not in evt["exdates"]:
                multidays = expand_multiday_event(evt, window_start, window_end)
                for inst in multidays:
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

    refresh_token = auth_data.get("refresh_token")
    client_id = auth_data.get("client_id")
    client_secret = auth_data.get("client_secret")
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

    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal_id}/events?{params}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "User-Agent": USER_AGENT,
    })

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
            data = json.loads(raw.decode("utf-8"))

        items = data.get("items", [])
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

    try:
        if url.startswith("file://") or url.startswith("/"):
            path = url[7:] if url.startswith("file://") else url
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = safe_read_text(f, max_bytes=MAX_ICAL_BYTES)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            resp_content = None
            last_error = None
            # Retry transient connection resets / throttling (common on Apple iCloud CalDAV)
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        raw = safe_read_bytes(resp, max_bytes=MAX_ICAL_BYTES)
                        resp_content = raw.decode("utf-8", errors="ignore")
                    break
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ConnectionError) as exc:
                    last_error = exc
                    if attempt == 0:
                        time.sleep(0.5)
                        continue
                    raise
            if resp_content is None and last_error:
                raise last_error
            content = resp_content or ""

        events = parse_ics(content, cal_info, window_start, window_end)
        return {
            "name": name,
            "color": cal_info.get("color", "#4A90E2"),
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


def validate_jmap_https_url(url, trusted_origin=None):
    """Return a validated credential-free HTTPS URL and its canonical origin."""
    if not isinstance(url, str) or not url or any(ord(char) < 0x20 for char in url) or "\\" in url:
        raise ValueError("JMAP URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("JMAP URL is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("JMAP URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("JMAP URL must not contain credentials")
    if parsed.fragment:
        raise ValueError("JMAP URL must not contain a fragment")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("JMAP URL hostname is invalid") from exc
    origin = ("https", hostname, port or 443)
    if trusted_origin is not None and origin != trusted_origin:
        raise ValueError("JMAP URL must remain on the configured session origin")
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
    session_url = (cal_info.get("jmapUrl") or cal_info.get("sessionUrl") or cal_info.get("url") or "").strip()

    if not session_url:
        session_url = "https://api.fastmail.com/jmap/session"
    elif "://" not in session_url:
        session_url = "https://" + session_url

    # Auto-resolve hostnames to .well-known/jmap if no path specified
    parsed = urllib.parse.urlsplit(session_url)
    if not parsed.path or parsed.path == "/":
        session_url = urllib.parse.urlunsplit(parsed._replace(path="/.well-known/jmap"))

    if not token:
        return {
            "name": name,
            "color": color,
            "events": [],
            "status": "auth_required: no JMAP token configured",
            "count": 0,
        }

    try:
        session_url, trusted_origin = validate_jmap_https_url(session_url)
        opener = urllib.request.build_opener(JmapSameOriginRedirectHandler(trusted_origin))
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Step 1: Session Discovery
        req = urllib.request.Request(session_url, headers=headers, method="GET")
        with open_trusted_jmap(opener, req, trusted_origin, timeout=12) as resp:
            raw_session = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
            session_data = json.loads(raw_session.decode("utf-8"))

        api_url = session_data.get("apiUrl")
        if not api_url:
            return {
                "name": name,
                "color": color,
                "events": [],
                "status": "error: no apiUrl in JMAP session response",
                "count": 0,
            }
        api_url, _ = validate_jmap_https_url(api_url, trusted_origin)

        # Find account supporting calendars
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
            return {
                "name": name,
                "color": color,
                "events": [],
                "status": "error: no JMAP calendar account found",
                "count": 0,
            }

        # Step 2: Query and Get Events
        time_min = window_start.strftime("%Y-%m-%dT00:00:00Z")
        time_max = window_end.strftime("%Y-%m-%dT23:59:59Z")

        cal_filter = {
            "after": time_min,
            "before": time_max,
        }

        cal_id = cal_info.get("jmapCalendarId") or cal_info.get("calendarId")
        if cal_id and cal_id != "primary":
            cal_filter["inCalendars"] = [cal_id]

        jmap_using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]
        session_caps = session_data.get("capabilities", {})
        for cap in session_caps:
            if "calendar" in cap.lower() and cap not in jmap_using:
                jmap_using.append(cap)

        query_call = {
            "accountId": account_id,
            "filter": cal_filter,
            "expandRecurrences": True,
        }

        payload = {
            "using": jmap_using,
            "methodCalls": [
                [
                    "CalendarEvent/query",
                    query_call,
                    "q0"
                ],
                [
                    "CalendarEvent/get",
                    {
                        "accountId": account_id,
                        "#ids": {
                            "resultOf": "q0",
                            "name": "CalendarEvent/query",
                            "path": "/ids"
                        }
                    },
                    "get0"
                ]
            ]
        }

        payload_bytes = json.dumps(payload).encode("utf-8")
        post_req = urllib.request.Request(api_url, data=payload_bytes, headers=headers, method="POST")

        with open_trusted_jmap(opener, post_req, trusted_origin, timeout=15) as resp:
            raw_data = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
            response_data = json.loads(raw_data.decode("utf-8"))

        method_responses = response_data.get("methodResponses", [])
        raw_events = []
        for resp_name, resp_args, resp_call_id in method_responses:
            if resp_name == "CalendarEvent/get":
                raw_events = resp_args.get("list", [])
                break
            elif resp_name == "error":
                return {
                    "name": name,
                    "color": color,
                    "events": [],
                    "status": f"jmap_error: {resp_args.get('type', 'unknown')}",
                    "count": 0,
                }

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
            events.extend(multidays)

        return {
            "name": name,
            "color": color,
            "events": events,
            "status": "ok",
            "count": len(events),
        }

    except urllib.error.HTTPError as e:
        status_msg = f"auth_failed ({e.code})" if e.code in (401, 403) else f"http_error ({e.code})"
        return {
            "name": name,
            "color": color,
            "events": [],
            "status": status_msg,
            "count": 0,
        }
    except Exception as e:
        return {
            "name": name,
            "color": color,
            "events": [],
            "status": f"error: {str(e)}",
            "count": 0,
        }


def fetch_calendar_item(cal_info, window_start, window_end):
    cal_type = str(cal_info.get("type", "")).lower()
    if cal_type == "local":
        return fetch_local_calendar(cal_info, window_start, window_end)
    elif cal_type == "jmap" or cal_info.get("jmapToken") or ("jmap" in cal_info.get("url", "").lower() and "jmapToken" in cal_info):
        return fetch_jmap_calendar(cal_info, window_start, window_end)
    elif cal_info.get("googleCalendarId") or (cal_info.get("calendarId") and not cal_info.get("jmapToken")):
        return fetch_google_api_calendar(cal_info, window_start, window_end)
    else:
        return fetch_calendar(cal_info, window_start, window_end)


def purge_plugin_data():
    """
    Securely removes token-bearing configuration, OAuth credentials, and cached state:
    - CONFIG_PATH (~/.config/omarchy/calendars.json)
    - AUTH_FILE (~/.local/state/omarchy/google-auth.json)
    - OUTPUT_PATH (~/.local/state/omarchy/calendar-events.json)
    - TRANSLATION_CACHE_PATH (~/.local/state/omarchy/translation-cache.json)
    - LOCAL_EVENTS_PATH (~/.local/state/omarchy/local-events.json)
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


def parse_iso_or_local(val_str):
    """Parse an ISO 8601 or local timestamp string into datetime."""
    if not val_str:
        return datetime.now()
    clean_str = str(val_str).strip()
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
    except Exception:
        return datetime.now()


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


def create_google_event(cal_info, event_data):
    """Create an event on Google Calendar using Google Calendar API v3."""
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

    title = str(event_data.get("title", "")).strip() or "(Untitled Event)"
    location = str(event_data.get("location", "")).strip()
    description = str(event_data.get("description", "")).strip()
    all_day = bool(event_data.get("allDay", False))

    start_val = str(event_data.get("start", "")).strip()
    end_val = str(event_data.get("end", "")).strip()
    if not start_val:
        raise ValueError("Event must have a start date/time")

    body = {
        "summary": title,
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    tz_name = get_local_tz_name()

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
    else:
        start_dt = parse_iso_or_local(start_val)
        if end_val:
            end_dt = parse_iso_or_local(end_val)
        else:
            end_dt = start_dt + timedelta(hours=1)

        start_iso = start_dt.astimezone().isoformat()
        end_iso = end_dt.astimezone().isoformat()

        body["start"] = {"dateTime": start_iso}
        body["end"] = {"dateTime": end_iso}
        if tz_name:
            body["start"]["timeZone"] = tz_name
            body["end"]["timeZone"] = tz_name

    encoded_cal_id = urllib.parse.quote(cal_id, safe="")
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal_id}/events"
    req_data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=req_data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        created_data = json.loads(raw.decode("utf-8"))
        return {"status": "success", "id": created_data.get("id"), "event": created_data}


def delete_google_event(cal_info, event_id):
    """Delete an event from Google Calendar API v3."""
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
    encoded_evt_id = urllib.parse.quote(str(event_id), safe="")
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal_id}/events/{encoded_evt_id}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        },
        method="DELETE"
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        return {"status": "success", "id": event_id}


def create_jmap_event(cal_info, event_data):
    """Create an event on a JMAP server (RFC 8620, RFC 9670, RFC 8984 JSCalendar)."""
    token = (cal_info.get("jmapToken") or cal_info.get("token") or cal_info.get("bearerToken") or "").strip()
    session_url = (cal_info.get("jmapUrl") or cal_info.get("sessionUrl") or cal_info.get("url") or "").strip()

    if not session_url:
        session_url = "https://api.fastmail.com/jmap/session"
    elif "://" not in session_url:
        session_url = "https://" + session_url

    parsed = urllib.parse.urlsplit(session_url)
    if not parsed.path or parsed.path == "/":
        session_url = urllib.parse.urlunsplit(parsed._replace(path="/.well-known/jmap"))

    if not token:
        raise ValueError("No JMAP bearer token configured")

    session_url, trusted_origin = validate_jmap_https_url(session_url)
    opener = urllib.request.build_opener(JmapSameOriginRedirectHandler(trusted_origin))
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Step 1: Session Discovery
    req = urllib.request.Request(session_url, headers=headers, method="GET")
    with open_trusted_jmap(opener, req, trusted_origin, timeout=12) as resp:
        raw_session = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        session_data = json.loads(raw_session.decode("utf-8"))

    api_url = session_data.get("apiUrl")
    if not api_url:
        raise ValueError("No apiUrl in JMAP session response")
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
        raise ValueError("No JMAP calendar account found")

    title = str(event_data.get("title", "")).strip() or "(Untitled Event)"
    location = str(event_data.get("location", "")).strip()
    description = str(event_data.get("description", "")).strip()
    all_day = bool(event_data.get("allDay", False))

    start_val = str(event_data.get("start", "")).strip()
    end_val = str(event_data.get("end", "")).strip()
    if not start_val:
        raise ValueError("Event must have a start date/time")

    tz_name = get_local_tz_name()

    jsevent = {
        "@type": "Event",
        "title": title,
        "description": description,
        "showWithoutTime": all_day,
    }
    if location:
        jsevent["locations"] = {"loc1": {"@type": "Location", "name": location}}

    if all_day:
        d_start = start_val[:10]
        jsevent["start"] = d_start
        jsevent["duration"] = "P1D"
    else:
        start_dt = parse_iso_or_local(start_val)
        if end_val:
            end_dt = parse_iso_or_local(end_val)
        else:
            end_dt = start_dt + timedelta(hours=1)
        dur_seconds = max(60, int((end_dt - start_dt).total_seconds()))
        dur_str = format_duration_iso(dur_seconds)

        jsevent["start"] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        if tz_name:
            jsevent["timeZone"] = tz_name
        jsevent["duration"] = dur_str

    cal_id = cal_info.get("jmapCalendarId") or cal_info.get("calendarId")
    if cal_id and cal_id != "primary":
        jsevent["calendarIds"] = {cal_id: True}

    jmap_using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]
    session_caps = session_data.get("capabilities", {})
    for cap in session_caps:
        if "calendar" in cap.lower() and cap not in jmap_using:
            jmap_using.append(cap)

    creation_id = f"c_{secrets.token_hex(4)}"
    payload = {
        "using": jmap_using,
        "methodCalls": [
            [
                "CalendarEvent/set",
                {
                    "accountId": account_id,
                    "create": {
                        creation_id: jsevent
                    }
                },
                "set0"
            ]
        ]
    }

    payload_bytes = json.dumps(payload).encode("utf-8")
    post_req = urllib.request.Request(api_url, data=payload_bytes, headers=headers, method="POST")

    with open_trusted_jmap(opener, post_req, trusted_origin, timeout=15) as resp:
        raw_data = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        response_data = json.loads(raw_data.decode("utf-8"))

    method_responses = response_data.get("methodResponses", [])
    for resp_name, resp_args, resp_call_id in method_responses:
        if resp_name == "CalendarEvent/set":
            created_map = resp_args.get("created", {})
            if creation_id in created_map:
                created_evt = created_map[creation_id]
                return {"status": "success", "id": created_evt.get("id"), "event": created_evt}
            not_created = resp_args.get("notCreated", {})
            if creation_id in not_created:
                err_desc = not_created[creation_id].get("description") or not_created[creation_id].get("type")
                raise ValueError(f"JMAP event creation rejected: {err_desc}")
        elif resp_name == "error":
            raise ValueError(f"JMAP error: {resp_args.get('type')}")

    return {"status": "success", "id": creation_id}


def delete_jmap_event(cal_info, event_id):
    """Delete an event on a JMAP server using CalendarEvent/set destroy."""
    token = (cal_info.get("jmapToken") or cal_info.get("token") or cal_info.get("bearerToken") or "").strip()
    session_url = (cal_info.get("jmapUrl") or cal_info.get("sessionUrl") or cal_info.get("url") or "").strip()

    if not session_url:
        session_url = "https://api.fastmail.com/jmap/session"
    elif "://" not in session_url:
        session_url = "https://" + session_url

    parsed = urllib.parse.urlsplit(session_url)
    if not parsed.path or parsed.path == "/":
        session_url = urllib.parse.urlunsplit(parsed._replace(path="/.well-known/jmap"))

    if not token:
        raise ValueError("No JMAP bearer token configured")

    session_url, trusted_origin = validate_jmap_https_url(session_url)
    opener = urllib.request.build_opener(JmapSameOriginRedirectHandler(trusted_origin))
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(session_url, headers=headers, method="GET")
    with open_trusted_jmap(opener, req, trusted_origin, timeout=12) as resp:
        raw_session = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        session_data = json.loads(raw_session.decode("utf-8"))

    api_url = session_data.get("apiUrl")
    if not api_url:
        raise ValueError("No apiUrl in JMAP session response")
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
        raise ValueError("No JMAP calendar account found")

    jmap_using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]
    session_caps = session_data.get("capabilities", {})
    for cap in session_caps:
        if "calendar" in cap.lower() and cap not in jmap_using:
            jmap_using.append(cap)

    payload = {
        "using": jmap_using,
        "methodCalls": [
            [
                "CalendarEvent/set",
                {
                    "accountId": account_id,
                    "destroy": [str(event_id)]
                },
                "del0"
            ]
        ]
    }

    payload_bytes = json.dumps(payload).encode("utf-8")
    post_req = urllib.request.Request(api_url, data=payload_bytes, headers=headers, method="POST")

    with open_trusted_jmap(opener, post_req, trusted_origin, timeout=15) as resp:
        raw_data = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
        response_data = json.loads(raw_data.decode("utf-8"))

    method_responses = response_data.get("methodResponses", [])
    for resp_name, resp_args, resp_call_id in method_responses:
        if resp_name == "CalendarEvent/set":
            destroyed = resp_args.get("destroyed", [])
            if str(event_id) in [str(d) for d in destroyed]:
                return {"status": "success", "id": event_id}
            not_destroyed = resp_args.get("notDestroyed", {})
            if str(event_id) in not_destroyed:
                err_desc = not_destroyed[str(event_id)].get("description") or not_destroyed[str(event_id)].get("type")
                raise ValueError(f"JMAP event deletion rejected: {err_desc}")
        elif resp_name == "error":
            raise ValueError(f"JMAP error: {resp_args.get('type')}")

    return {"status": "success", "id": event_id}


def find_calendar_config(cal_name_or_id):
    """Find calendar entry matching name or ID from config, or default to local."""
    ensure_config_exists()
    calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES) or []
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
    cal_target = event_data.get("calendar") or event_data.get("calendarId") or "local"
    cal_info = find_calendar_config(cal_target)
    cal_type = str(cal_info.get("type", "")).lower()

    if cal_type == "local" or str(cal_target).lower() in ("local", "local calendar"):
        res = create_local_event(cal_info, event_data)
    elif cal_type == "jmap" or cal_info.get("jmapToken"):
        res = create_jmap_event(cal_info, event_data)
    elif cal_info.get("googleCalendarId") or (cal_info.get("calendarId") and not cal_info.get("url")):
        res = create_google_event(cal_info, event_data)
    else:
        raise ValueError(f"Calendar '{cal_info.get('name')}' is a read-only subscription feed and does not accept push events.")

    sync_all_events()
    return res


def delete_event(delete_data):
    """Dispatcher to delete an event from the specified calendar."""
    event_id = delete_data.get("id")
    if not event_id:
        raise ValueError("Missing event ID for deletion")

    cal_target = delete_data.get("calendar") or delete_data.get("calendarId") or delete_data.get("calendarType") or "local"
    cal_type = str(delete_data.get("calendarType", "")).lower()

    if not cal_type:
        cal_info = find_calendar_config(cal_target)
        cal_type = str(cal_info.get("type", "")).lower()
        if not cal_type:
            if cal_info.get("googleCalendarId"):
                cal_type = "google"
            elif cal_info.get("jmapToken"):
                cal_type = "jmap"
            else:
                cal_type = "local"
    else:
        cal_info = find_calendar_config(cal_target)

    if cal_type == "local" or str(event_id).startswith("loc_") or str(event_id).startswith("local_"):
        res = delete_local_event(cal_info, event_id)
    elif cal_type == "jmap":
        res = delete_jmap_event(cal_info, event_id)
    elif cal_type == "google":
        res = delete_google_event(cal_info, event_id)
    else:
        raise ValueError(f"Calendar '{cal_target}' does not support event deletion (read-only feed).")

    sync_all_events()
    return res


def get_writable_calendars():
    """Returns a list of calendars configured or available for writing events."""
    ensure_config_exists()
    calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES) or []
    writables = []
    has_local = False

    for c in calendars:
        c_type = str(c.get("type", "")).lower()
        if c_type == "local":
            has_local = True
            writables.append({
                "name": c.get("name", "Local Calendar"),
                "type": "local",
                "color": c.get("color", "#a6e3a1"),
                "calendarId": "local",
                "writable": True,
            })
        elif c_type == "jmap" or c.get("jmapToken"):
            writables.append({
                "name": c.get("name", "JMAP Calendar"),
                "type": "jmap",
                "color": c.get("color", "#ff7700"),
                "calendarId": c.get("jmapCalendarId") or c.get("calendarId") or "primary",
                "writable": True,
            })
        elif c.get("googleCalendarId") or (c.get("calendarId") and not c.get("url")):
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

    try:
        calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES) or []
    except Exception:
        calendars = []

    now = datetime.now()
    window_start = now - timedelta(days=45)
    window_end = now + timedelta(days=90)

    enabled_cals = [
        c for c in calendars
        if isinstance(c, dict) and c.get("enabled", True) and (
            c.get("url") or
            c.get("googleCalendarId") or
            c.get("calendarId") or
            c.get("jmapToken") or
            str(c.get("type", "")).lower() == "jmap" or
            str(c.get("type", "")).lower() == "local"
        )
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
        else:
            end_time_str = "00:00"

        is_all_day = bool(evt.get("all_day", False))

        events_by_date[d_key].append({
            "id": str(evt.get("id", "")),
            "title": str(evt.get("title") or "(Untitled Event)"),
            "calendar": str(evt.get("calendar") or "Calendar"),
            "calendarId": str(evt.get("calendarId", "")),
            "calendarType": str(evt.get("calendarType", "ical")),
            "writable": bool(evt.get("writable", False)),
            "description": str(evt.get("description") or ""),
            "color": str(evt.get("color") or "#4A90E2"),
            "allDay": is_all_day,
            "startTime": start_time_str if not is_all_day else "All Day",
            "endTime": end_time_str if not is_all_day else "",
            "location": str(evt.get("location") or ""),
            "startIso": start_iso,
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


def main():
    try:
        if len(sys.argv) > 1:
            arg = sys.argv[1]
            if arg in ("--purge-data", "--purge-auth", "--cleanup", "--uninstall"):
                res = purge_plugin_data()
                print(json.dumps(res, indent=2))
                sys.exit(0 if res["status"] == "success" else 1)

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
                    new_config = json.loads(raw_input)
                    if not isinstance(new_config, list):
                        raise ValueError("Config must be a JSON array of calendar entries")
                    write_secure_json(CONFIG_PATH, new_config, mode=0o600)
                    print(json.dumps({"status": "success"}))
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
            elif arg == "--create-event":
                try:
                    raw_input = sys.argv[2] if len(sys.argv) > 2 else read_stdin_payload(MAX_CONFIG_BYTES)
                    if len(raw_input) > MAX_CONFIG_BYTES:
                        raise ValueError(f"Payload exceeds maximum size of {MAX_CONFIG_BYTES} bytes")
                    event_data = json.loads(raw_input)
                    if not isinstance(event_data, dict):
                        raise ValueError("Payload must be a JSON object")
                    res = create_event(event_data)
                    print(json.dumps(res, ensure_ascii=False))
                    sys.exit(0 if res.get("status") == "success" else 1)
                except Exception as e:
                    print(json.dumps({"status": "error", "message": str(e)}))
                    sys.exit(1)
            elif arg == "--delete-event":
                try:
                    raw_input = sys.argv[2] if len(sys.argv) > 2 else read_stdin_payload(MAX_CONFIG_BYTES)
                    if len(raw_input) > MAX_CONFIG_BYTES:
                        raise ValueError(f"Payload exceeds maximum size of {MAX_CONFIG_BYTES} bytes")
                    delete_data = json.loads(raw_input)
                    if not isinstance(delete_data, dict):
                        raise ValueError("Payload must be a JSON object")
                    res = delete_event(delete_data)
                    print(json.dumps(res, ensure_ascii=False))
                    sys.exit(0 if res.get("status") == "success" else 1)
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
