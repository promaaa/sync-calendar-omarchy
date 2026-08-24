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
import urllib.request
import urllib.parse
import urllib.error
import base64
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor

CONFIG_PATH = os.path.expanduser("~/.config/omarchy/calendars.json")
STATE_DIR = os.path.expanduser("~/.local/state/omarchy")
OUTPUT_PATH = os.path.join(STATE_DIR, "calendar-events.json")
TRANSLATION_CACHE_PATH = os.path.join(STATE_DIR, "translation-cache.json")

# Standard browser user-agent to ensure compatibility with calendar providers (Apple iCloud, Proton, Google, Outlook, Nextcloud, etc.)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 (OmarchyCalendar/1.0)"

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

_translation_cache = {}

MAX_ICAL_BYTES = 10 * 1024 * 1024   # 10 MB limit for calendar .ics content
MAX_API_BYTES = 5 * 1024 * 1024     # 5 MB limit for API JSON responses
MAX_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MB limit for local config files
MAX_OUTPUT_JSON_BYTES = 25 * 1024 * 1024  # 25 MB limit for generated event state


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


def parse_datetime_value(val_str, params=None):
    """
    Parse an iCal date or datetime string.
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

    # Try datetime formats: 20260816T143000Z or 20260816T143000
    cleaned = re.sub(r"[+-]\d\d:?\d\d$", "", val_str).rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return False, datetime.strptime(cleaned[:19], fmt)
        except ValueError:
            pass

    try:
        d = datetime.strptime(val_str[:8], "%Y%m%d").date()
        return True, datetime(d.year, d.month, d.day, 0, 0, 0)
    except Exception:
        return True, datetime.now()


def parse_rrule(rrule_str):
    """Parse a basic RRULE string into key-value pairs."""
    rule = {}
    for part in rrule_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.upper()] = v
    return rule


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
            url = m.group(0).rstrip(";,)>]\"'")
            return url, name

    # Check if location contains any valid HTTP/HTTPS URL
    loc_url_m = re.search(r'https?://[^\s<>"\'\)\]]+', location or "")
    if loc_url_m:
        return loc_url_m.group(0).rstrip(".,;)>]\"'"), "Meeting Link"

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
    interval = max(1, int(rrule.get("INTERVAL", 1)))
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

    while count < max_count:
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

                if count >= max_count:
                    break

        cur_week_start += timedelta(weeks=interval)

    return instances


def expand_daily(event, window_start, window_end, rrule, until_dt, max_count):
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    duration = end_dt - start_dt
    interval = max(1, int(rrule.get("INTERVAL", 1)))
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

    while count < max_count and cur_dt <= window_end:
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
    interval = max(1, int(rrule.get("INTERVAL", 1)))
    exdates = set(event.get("exdates", []))

    instances = []
    cur_year = start_dt.year
    cur_month = start_dt.month
    count = 0

    while count < max_count:
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
            if count >= max_count:
                break

        total_months = (cur_year * 12 + cur_month - 1) + interval
        cur_year = total_months // 12
        cur_month = (total_months % 12) + 1

    return instances


def expand_yearly(event, window_start, window_end, rrule, until_dt, max_count):
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    duration = end_dt - start_dt
    interval = max(1, int(rrule.get("INTERVAL", 1)))
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

    while count < max_count:
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
                if count >= max_count:
                    break

        cur_year += interval

    return instances


def expand_recurring_event(event, window_start, window_end):
    """
    Expands a recurring VEVENT within [window_start, window_end].
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

    max_count = int(count_str) if count_str and count_str.isdigit() else 1000

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
    """
    start_dt = event["start_dt"]
    end_dt = event["end_dt"]
    all_day = event.get("all_day", False)

    start_date = start_dt.date()
    # RFC 5545 specifies DTEND for all-day is exclusive
    if all_day:
        end_date = end_dt.date() - timedelta(days=1)
        if end_date < start_date:
            end_date = start_date
    else:
        end_date = end_dt.date()

    if start_date == end_date:
        event["date_key"] = start_date.strftime("%Y-%m-%d")
        return [event]

    instances = []
    cur_date = start_date
    while cur_date <= end_date:
        cur_dt_midnight = datetime(cur_date.year, cur_date.month, cur_date.day)
        if window_start <= cur_dt_midnight <= window_end:
            inst = dict(event)
            inst["date_key"] = cur_date.strftime("%Y-%m-%d")
            instances.append(inst)
        cur_date += timedelta(days=1)

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


def get_google_access_token():
    """Retrieve or refresh Google OAuth2 access token."""
    try:
        auth_data = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES)
        if not auth_data:
            return None

        now = time.time()
        if auth_data.get("access_token") and auth_data.get("expires_at", 0) > now + 60:
            return auth_data["access_token"]

        refresh_token = auth_data.get("refresh_token")
        client_id = auth_data.get("client_id")
        client_secret = auth_data.get("client_secret")

        if not refresh_token or not client_id or not client_secret:
            return None

        url = "https://oauth2.googleapis.com/token"
        payload = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = safe_read_bytes(resp, max_bytes=MAX_API_BYTES)
            data = json.loads(raw.decode("utf-8"))
            access_token = data.get("access_token")
            auth_data["access_token"] = access_token
            auth_data["expires_at"] = int(now) + data.get("expires_in", 3600)
            auth_data["updated_at"] = int(now)

            write_secure_json(AUTH_FILE, auth_data, mode=0o600)

            return access_token
    except Exception:
        return None


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
            "status": "auth_required: run google-auth.py",
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
                dt_str = start_info["dateTime"]
                cleaned = re.sub(r"[+-]\d\d:\d\d$", "", dt_str).rstrip("Z")
                start_dt = datetime.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S")
                if "dateTime" in end_info:
                    end_cleaned = re.sub(r"[+-]\d\d:\d\d$", "", end_info["dateTime"]).rstrip("Z")
                    end_dt = datetime.strptime(end_cleaned[:19], "%Y-%m-%dT%H:%M:%S")
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
            meeting_url = item.get("hangoutLink") or ""
            meeting_provider = "Google Meet" if meeting_url else ""

            if not meeting_url:
                conf_data = item.get("conferenceData", {})
                for ep in conf_data.get("entryPoints", []):
                    if ep.get("uri"):
                        meeting_url = ep["uri"]
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

    username = cal_info.get("username")
    password = cal_info.get("password")

    headers = {
        "User-Agent": USER_AGENT
    }

    if username and password:
        auth_str = f"{username}:{password}"
        auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {auth_b64}"

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
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = safe_read_bytes(resp, max_bytes=MAX_ICAL_BYTES)
                content = raw.decode("utf-8", errors="ignore")

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


def parse_jmap_datetime(dt_str):
    """
    Parses ISO 8601 date or datetime string into naive datetime.
    """
    if not dt_str:
        return datetime.now()
    cleaned = re.sub(r"[+-]\d\d:\d\d$", "", str(dt_str)).rstrip("Z")
    if len(cleaned) == 10:  # YYYY-MM-DD
        try:
            return datetime.strptime(cleaned, "%Y-%m-%d")
        except Exception:
            pass
    if len(cleaned) >= 19:
        try:
            return datetime.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            pass
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
                        u = vl.get("uri")
                        _, prov = extract_meeting_info(u, "", "")
                        if prov:
                            meeting_url = u
                            meeting_provider = prov
                            break
                        elif not meeting_url:
                            meeting_url = u
                            meeting_provider = "Online Meeting"

            if not meeting_url:
                meeting_url, meeting_provider = extract_meeting_info(location, description, title)

            all_day = bool(item.get("showWithoutTime"))
            start_str = item.get("start")
            if not start_str:
                continue

            start_dt = parse_jmap_datetime(start_str)

            if item.get("duration"):
                dur = parse_iso_duration(item.get("duration"))
                end_dt = start_dt + dur
            elif item.get("end"):
                end_dt = parse_jmap_datetime(item.get("end"))
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
    if cal_type == "jmap" or cal_info.get("jmapToken") or ("jmap" in cal_info.get("url", "").lower() and "jmapToken" in cal_info):
        return fetch_jmap_calendar(cal_info, window_start, window_end)
    elif cal_info.get("googleCalendarId") or (cal_info.get("calendarId") and not cal_info.get("jmapToken")):
        return fetch_google_api_calendar(cal_info, window_start, window_end)
    else:
        return fetch_calendar(cal_info, window_start, window_end)


def main():
    ensure_config_exists()
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    load_translation_cache()

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--save-config" and len(sys.argv) > 2:
            try:
                new_config = json.loads(sys.argv[2])
                write_secure_json(CONFIG_PATH, new_config, mode=0o600)
                print(json.dumps({"status": "success"}))
            except Exception as e:
                print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
        elif arg == "--get-config":
            ensure_config_exists()
            content = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES)
            print(json.dumps(content, ensure_ascii=False, indent=2))
            sys.exit(0)
        elif arg == "--auth-status":
            auth_ok = False
            auth_data = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES)
            if auth_data and auth_data.get("refresh_token") and auth_data.get("client_id"):
                auth_ok = True
            print(json.dumps({"authenticated": auth_ok}))
            sys.exit(0)

    try:
        calendars = safe_load_json(CONFIG_PATH, max_bytes=MAX_CONFIG_BYTES) or []
    except Exception:
        calendars = []

    # Time window: 45 days ago to 90 days in the future
    now = datetime.now()
    window_start = now - timedelta(days=45)
    window_end = now + timedelta(days=90)

    enabled_cals = [
        c for c in calendars
        if c.get("enabled", True) and (
            c.get("url") or
            c.get("googleCalendarId") or
            c.get("calendarId") or
            c.get("jmapToken") or
            c.get("type") == "jmap"
        )
    ]

    all_events = []
    cal_statuses = []

    if enabled_cals:
        with ThreadPoolExecutor(max_workers=min(8, len(enabled_cals))) as executor:
            futures = [
                executor.submit(fetch_calendar_item, c, window_start, window_end)
                for c in enabled_cals
            ]
            for f in futures:
                res = f.result()
                all_events.extend(res["events"])
                cal_statuses.append({
                    "name": res["name"],
                    "color": res["color"],
                    "status": res["status"],
                    "count": res["count"],
                })

    # Group events by date key ("YYYY-MM-DD")
    events_by_date = {}
    for evt in all_events:
        d_key = evt["date_key"]
        if d_key not in events_by_date:
            events_by_date[d_key] = []

        start_time_str = evt["start_dt"].strftime("%H:%M")
        end_time_str = evt["end_dt"].strftime("%H:%M")

        events_by_date[d_key].append({
            "id": evt["id"],
            "title": evt["title"],
            "calendar": evt["calendar"],
            "color": evt["color"],
            "allDay": evt["all_day"],
            "startTime": start_time_str if not evt["all_day"] else "All Day",
            "endTime": end_time_str if not evt["all_day"] else "",
            "location": evt["location"],
            "startIso": evt["start_dt"].isoformat(),
            "meetingUrl": evt.get("meetingUrl") or "",
            "meetingProvider": evt.get("meetingProvider") or "",
        })

    # Sort events in each day: All day events first, then chronological
    for d_key in events_by_date:
        events_by_date[d_key].sort(
            key=lambda x: (0 if x["allDay"] else 1, x["startTime"], x["title"])
        )

    auth_ok = False
    try:
        auth_d = safe_load_json(AUTH_FILE, max_bytes=MAX_CONFIG_BYTES)
        if auth_d:
            auth_ok = bool(auth_d.get("refresh_token") and auth_d.get("client_id"))
    except Exception:
        pass

    output_data = {
        "lastSynced": int(time.time()),
        "lastSyncedFormatted": now.strftime("%H:%M"),
        "totalEvents": len(all_events),
        "configuredCount": len(enabled_cals),
        "authenticated": auth_ok,
        "calendars": cal_statuses,
        "eventsByDate": events_by_date,
    }

    write_secure_json(OUTPUT_PATH, output_data, mode=0o600, max_bytes=MAX_OUTPUT_JSON_BYTES)
    save_translation_cache()

    print(json.dumps({
        "status": "success",
        "totalEvents": len(all_events),
        "calendars": len(cal_statuses),
    }))


if __name__ == "__main__":
    main()
