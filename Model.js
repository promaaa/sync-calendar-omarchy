// Pure date and format math for the clock widget and its calendar panel.
// Everything here is locale- and Qt-free so it can be unit tested under node
// (tests/test_model.js); the QML owns month/weekday naming through
// Qt.locale().

var MS_PER_DAY = 86400000

// Weekday indices match both JS Date.getDay() and QML's Locale.Sunday…
// Locale.Saturday, so a locale's firstDayOfWeek can be passed straight in.
var WEEKDAY_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

// ---- Bar label formats. Right-clicking the clock walks these in order and
//      writes the result back to shell.json, so the label the bar shows and
//      the format the config stores are always the same thing.
//
// The locale-shaped time presets are each followed by their 12-hour twin, so
// the walk from a 24-hour label to the same label in AM/PM is a single right
// click rather than a lap of the ring. The ISO preset is deliberately left
// without one: ISO 8601 writes time on a 24-hour clock, so an AM/PM variant
// would contradict the only thing that format is for.
var CLOCK_FORMATS = [
  "dddd HH:mm",
  "dddd h:mm AP",
  "HH:mm",
  "h:mm AP",
  "ddd d MMM HH:mm",
  "ddd d MMM h:mm AP",
  "d MMMM 'W'ww yyyy",
  "yyyy-MM-dd HH:mm"
]

// Vertical bars have room for a few stacked lines and nothing else, so the
// ring stays short. AM/PM costs a fourth line, which is why only the plain
// time carries it here.
var VERTICAL_CLOCK_FORMATS = [
  "HH\n—\nmm",
  "h\n—\nmm\nAP",
  "dd\nMMM\n'W'ww\n''yy",
  "HH\nmm"
]

// Matching 24-hour and 12-hour versions of every time-bearing preset. Keeping
// this mapping separate from the format ring lets Preferences change only the
// hour cycle without discarding the user's chosen date/layout variant.
var CLOCK_FORMAT_PAIRS = [
  ["dddd HH:mm", "dddd h:mm AP"],
  ["HH:mm", "h:mm AP"],
  ["ddd d MMM HH:mm", "ddd d MMM h:mm AP"],
  ["yyyy-MM-dd HH:mm", "yyyy-MM-dd h:mm AP"],
  ["HH\n—\nmm", "h\n—\nmm\nAP"],
  ["HH\nmm", "h\nmm\nAP"]
]

function normalizedHourCycle(value) {
  var text = String(value === undefined || value === null ? "" : value).toLowerCase()
  return text === "12" || text === "12h" || text === "12-hour" ? 12 : 24
}

function clockFormatForHourCycle(format, hourCycle) {
  var current = String(format === undefined || format === null ? "" : format)
  var targetIndex = normalizedHourCycle(hourCycle) === 12 ? 1 : 0
  for (var i = 0; i < CLOCK_FORMAT_PAIRS.length; i++) {
    if (CLOCK_FORMAT_PAIRS[i][0] === current || CLOCK_FORMAT_PAIRS[i][1] === current)
      return CLOCK_FORMAT_PAIRS[i][targetIndex]
  }
  return current
}

function hourCycleForClockFormat(format, fallback) {
  var current = String(format === undefined || format === null ? "" : format)
  for (var i = 0; i < CLOCK_FORMAT_PAIRS.length; i++) {
    if (CLOCK_FORMAT_PAIRS[i][0] === current) return 24
    if (CLOCK_FORMAT_PAIRS[i][1] === current) return 12
  }
  return normalizedHourCycle(fallback)
}

// Synced events are stored as sortable 24-hour HH:mm strings. Format them at
// the presentation boundary so changing this preference never requires a
// calendar refresh or mutates the cache.
function formatEventTime(value, hourCycle) {
  var text = String(value === undefined || value === null ? "" : value)
  if (normalizedHourCycle(hourCycle) !== 12) return text
  var match = /^(\d{1,2}):(\d{2})$/.exec(text)
  if (!match) return text
  var hour = parseInt(match[1], 10)
  if (hour < 0 || hour > 23) return text
  return String(hour % 12 || 12) + ":" + match[2] + (hour < 12 ? " AM" : " PM")
}

function clockFormats(vertical) {
  return vertical ? VERTICAL_CLOCK_FORMATS.slice() : CLOCK_FORMATS.slice()
}

// The presets in a fixed order, plus the configured alternate and current
// format when they are something else. The order must not depend on which
// entry is current: cycling writes the result back to shell.json, and a ring
// that reshuffled itself around the current value would bounce between two
// entries instead of walking.
function clockFormatRing(configured, configuredAlt, presets) {
  var ring = []
  var candidates = (presets || []).concat([configuredAlt, configured])
  for (var i = 0; i < candidates.length; i++) {
    var format = String(candidates[i] === undefined || candidates[i] === null ? "" : candidates[i])
    if (format === "" || ring.indexOf(format) !== -1) continue
    ring.push(format)
  }
  return ring.length > 0 ? ring : ["HH:mm"]
}

// Next entry after `current`. An unknown current format (a hand-written one
// that is not in the ring) starts the walk at the top.
function nextClockFormat(ring, current) {
  if (!ring || ring.length === 0) return ""
  var index = ring.indexOf(String(current === undefined || current === null ? "" : current))
  return ring[(index + 1) % ring.length]
}

// Two-digit ISO week, substituted into a format's 'ww' token before Qt
// formats it -- Qt has no ISO week specifier of its own.
function isoWeekLiteral(year, month, day) {
  return pad2(isoWeek(year, month, day))
}

function pad2(value) {
  var n = Number(value)
  return (n < 10 ? "0" : "") + n
}

// Stable "yyyy-MM-dd" identity for a day, so a grid cell can be compared
// against today without dragging Date objects through bindings.
function dateKey(year, month, day) {
  return year + "-" + pad2(Number(month) + 1) + "-" + pad2(day)
}

function keyForDate(date) {
  return dateKey(date.getFullYear(), date.getMonth(), date.getDate())
}

function coerceWeekStart(value) {
  if (value === undefined || value === null) return null
  if (typeof value === "number")
    return isFinite(value) ? ((Math.round(value) % 7) + 7) % 7 : null

  var text = String(value).replace(/^\s+|\s+$/g, "").toLowerCase()
  if (text === "") return null

  for (var i = 0; i < WEEKDAY_NAMES.length; i++)
    if (WEEKDAY_NAMES[i] === text || WEEKDAY_NAMES[i].substr(0, 3) === text) return i

  var parsed = parseInt(text, 10)
  return isFinite(parsed) ? ((parsed % 7) + 7) % 7 : null
}

// Configured week start, falling back to the locale's own first day when
// the setting is missing or nonsense.
function normalizedWeekStart(value, fallback) {
  var configured = coerceWeekStart(value)
  if (configured !== null) return configured
  var fallbackStart = coerceWeekStart(fallback)
  return fallbackStart === null ? 1 : fallbackStart
}

function weekStartSettingName(index) {
  return WEEKDAY_NAMES[normalizedWeekStart(index, 1)]
}

// The toggle flips between the two conventions people actually switch
// between. A calendar configured to any other start (Saturday, say) is
// shown as-is and lands on Monday the first time it is toggled.
function toggledWeekStart(index) {
  return normalizedWeekStart(index, 1) === 1 ? 0 : 1
}

function weekdayOrder(weekStart) {
  var start = normalizedWeekStart(weekStart, 1)
  var out = []
  for (var i = 0; i < 7; i++) out.push((start + i) % 7)
  return out
}

// ISO-8601 week number: the week owning the Thursday of that date's
// Monday-based week. Mirrors the clock widget's 'ww' format token.
function isoWeek(year, month, day) {
  var date = new Date(Date.UTC(year, month, day))
  var weekday = date.getUTCDay() || 7
  date.setUTCDate(date.getUTCDate() + 4 - weekday)
  var yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1))
  return Math.ceil(((date.getTime() - yearStart.getTime()) / MS_PER_DAY + 1) / 7)
}

function dayOfYear(year, month, day) {
  return Math.round((Date.UTC(year, month, day) - Date.UTC(year, 0, 1)) / MS_PER_DAY) + 1
}

function daysInYear(year) {
  return dayOfYear(year, 11, 31)
}

// Share of the year already behind you: whole days completed over days in
// the year, so January 1 reads 0% and December 31 reads 100%.
function yearProgress(year, month, day) {
  var total = daysInYear(year)
  if (total <= 0) return 0
  return Math.max(0, Math.min(1, (dayOfYear(year, month, day) - 1) / total))
}

function yearProgressPercent(year, month, day) {
  return Math.round(yearProgress(year, month, day) * 100)
}

// Memento mori. The default span is a round number rather than anything from
// an actuarial table: the point of the bar is the reminder, not the
// arithmetic, and whoever wants a different number can say so.
var DEFAULT_LIFE_EXPECTANCY = 90

// A birth year rather than an age, so the bar keeps counting on its own
// instead of going stale the moment it is entered. 0 means "not set", which
// is also what a blank, malformed, future, or implausibly distant year means.
function parseBirthYear(value, currentYear) {
  var now = Math.round(Number(currentYear))
  if (!isFinite(now)) return 0
  var text = String(value === undefined || value === null ? "" : value).replace(/^\s+|\s+$/g, "")
  if (!/^\d{4}$/.test(text)) return 0
  var year = parseInt(text, 10)
  if (!isFinite(year) || year > now || year < now - 120) return 0
  return year
}

// Whole years, the way people say their age: born in 1979 makes you 47 for
// all of 2026, whichever side of your birthday today falls.
function ageFromBirthYear(birthYear, currentYear) {
  var born = parseBirthYear(birthYear, currentYear)
  if (born <= 0) return 0
  return Math.round(Number(currentYear)) - born
}

// 0 means "not set", which is also what a blank, negative, fractional, or
// absurd entry means, the life bar simply stays hidden.
function parseAge(value) {
  var text = String(value === undefined || value === null ? "" : value).replace(/^\s+|\s+$/g, "")
  if (!/^\d+$/.test(text)) return 0
  var years = parseInt(text, 10)
  if (!isFinite(years) || years <= 0 || years > 120) return 0
  return years
}

// Unset or nonsense falls back to the default rather than to zero, so the
// bar always has something to measure against.
function parseLifeExpectancy(value) {
  var text = String(value === undefined || value === null ? "" : value).replace(/^\s+|\s+$/g, "")
  if (!/^\d+$/.test(text)) return DEFAULT_LIFE_EXPECTANCY
  var years = parseInt(text, 10)
  if (!isFinite(years) || years <= 0 || years > 150) return DEFAULT_LIFE_EXPECTANCY
  return years
}

function lifeProgress(age, expectancy) {
  var years = parseAge(age)
  var span = parseLifeExpectancy(expectancy)
  if (years <= 0 || span <= 0) return 0
  return Math.max(0, Math.min(1, years / span))
}

function lifeProgressPercent(age, expectancy) {
  return Math.round(lifeProgress(age, expectancy) * 100)
}

// Always six rows of seven days. A fixed grid keeps the popup exactly the
// same height in every month, so stepping through the year never makes the
// panel jump under the pointer.
function monthGrid(year, month, weekStart, todayKey) {
  var start = normalizedWeekStart(weekStart, 1)
  var leading = (new Date(year, month, 1).getDay() - start + 7) % 7
  var cursor = new Date(year, month, 1 - leading)
  var today = String(todayKey || "")
  var weeks = []

  for (var w = 0; w < 6; w++) {
    var days = []
    var thursday = null
    for (var d = 0; d < 7; d++) {
      var cellYear = cursor.getFullYear()
      var cellMonth = cursor.getMonth()
      var cellDay = cursor.getDate()
      var weekday = cursor.getDay()
      var key = dateKey(cellYear, cellMonth, cellDay)
      if (weekday === 4) thursday = { year: cellYear, month: cellMonth, day: cellDay }
      days.push({
        key: key,
        year: cellYear,
        month: cellMonth,
        day: cellDay,
        weekday: weekday,
        inMonth: cellMonth === month && cellYear === year,
        weekend: weekday === 0 || weekday === 6,
        today: key === today
      })
      cursor.setDate(cursor.getDate() + 1)
    }
    // Number every row by the ISO week owning its Thursday. That is the
    // definition itself for Monday-start weeks, and the only answer that
    // stays stable for the other starts, where a row straddles two ISO
    // weeks but shares all of Monday through Thursday with one of them.
    var anchor = thursday || days[0]
    weeks.push({
      week: isoWeek(anchor.year, anchor.month, anchor.day),
      days: days
    })
  }
  return weeks
}

function stepMonth(year, month, delta) {
  var target = new Date(year, Number(month) + Number(delta), 1)
  return { year: target.getFullYear(), month: target.getMonth() }
}

// Pure date navigation math for grid & Vim keyboard navigation.
function stepDate(dateKeyStr, deltaDays) {
  var parts = String(dateKeyStr || "").split("-")
  var y, m, d
  if (parts.length === 3) {
    y = parseInt(parts[0], 10)
    m = parseInt(parts[1], 10) - 1
    d = parseInt(parts[2], 10)
  }
  if (!isFinite(y) || !isFinite(m) || !isFinite(d)) {
    var now = new Date()
    y = now.getFullYear()
    m = now.getMonth()
    d = now.getDate()
  }
  var dt = new Date(Date.UTC(y, m, d + Number(deltaDays || 0)))
  var resYear = dt.getUTCFullYear()
  var resMonth = dt.getUTCMonth()
  var resDay = dt.getUTCDate()
  return {
    dateKey: dateKey(resYear, resMonth, resDay),
    year: resYear,
    month: resMonth,
    day: resDay
  }
}

function stepToMonthBound(dateKeyStr, bound) {
  var parts = String(dateKeyStr || "").split("-")
  if (parts.length !== 3) return stepDate(dateKeyStr, 0)
  var y = parseInt(parts[0], 10)
  var m = parseInt(parts[1], 10) - 1
  if (!isFinite(y) || !isFinite(m)) return stepDate(dateKeyStr, 0)
  var isStart = bound === "start" || bound === "first" || bound === "top"
  var targetDay = isStart ? 1 : new Date(Date.UTC(y, m + 1, 0)).getUTCDate()
  return {
    dateKey: dateKey(y, m, targetDay),
    year: y,
    month: m,
    day: targetDay
  }
}

function stepToWeekBound(dateKeyStr, bound, weekStart) {
  var parts = String(dateKeyStr || "").split("-")
  if (parts.length !== 3) return stepDate(dateKeyStr, 0)
  var y = parseInt(parts[0], 10)
  var m = parseInt(parts[1], 10) - 1
  var d = parseInt(parts[2], 10)
  if (!isFinite(y) || !isFinite(m) || !isFinite(d)) return stepDate(dateKeyStr, 0)
  var dt = new Date(Date.UTC(y, m, d))
  var dayOfWeek = dt.getUTCDay()
  var start = normalizedWeekStart(weekStart, 1)
  var offsetFromStart = (dayOfWeek - start + 7) % 7
  var isStart = bound === "start" || bound === "first" || bound === "top"
  var delta = isStart ? -offsetFromStart : (6 - offsetFromStart)
  return stepDate(dateKeyStr, delta)
}

function parseEventsFile(text) {
  if (!text || typeof text !== "string") {
    return { eventsByDate: {}, calendars: [], lastSyncedFormatted: "", totalEvents: 0, configuredCount: 0 }
  }
  try {
    var data = JSON.parse(text)
    return {
      eventsByDate: data.eventsByDate || {},
      calendars: data.calendars || [],
      lastSyncedFormatted: data.lastSyncedFormatted || "",
      totalEvents: data.totalEvents || 0,
      configuredCount: data.configuredCount !== undefined ? data.configuredCount : (data.calendars ? data.calendars.length : 0)
    }
  } catch (e) {
    return { eventsByDate: {}, calendars: [], lastSyncedFormatted: "", totalEvents: 0, configuredCount: 0 }
  }
}

function formatSelectedDateLabel(dateKeyStr, todayKeyStr, locale) {
  if (!dateKeyStr) return "TODAY"
  if (dateKeyStr === todayKeyStr) return "TODAY"
  var parts = dateKeyStr.split("-")
  if (parts.length !== 3) return dateKeyStr
  var y = parseInt(parts[0], 10)
  var m = parseInt(parts[1], 10) - 1
  var d = parseInt(parts[2], 10)
  var dt = new Date(y, m, d)
  var dayName = (locale && typeof locale.dayName === "function") ? locale.dayName(dt.getDay(), 1) : WEEKDAY_NAMES[dt.getDay()]
  var monthName = (locale && typeof locale.monthName === "function") ? locale.monthName(dt.getMonth(), 1) : ("" + (m + 1))
  return (dayName + ", " + monthName + " " + d).toUpperCase()
}

var CALENDAR_COLORS = [
  "#4285f4", // Google Blue
  "#6d4aff", // Proton Purple
  "#30d158", // Apple Green
  "#e01b24", // Red
  "#f6c177", // Gold / Orange
  "#eb6f92", // Rose / Pink
  "#9ccfd8", // Cyan
  "#c4a7e7", // Lavender
  "#3584e4"  // Deep Blue
]


function cycleCalendarColor(current) {
  var idx = CALENDAR_COLORS.indexOf(String(current || "").toLowerCase())
  if (idx === -1) idx = 0
  return CALENDAR_COLORS[(idx + 1) % CALENDAR_COLORS.length]
}

function parseCalendarsConfig(text) {
  if (!text || typeof text !== "string") return []
  try {
    var list = JSON.parse(text)
    return Array.isArray(list) ? list : []
  } catch (e) {
    return []
  }
}

// A Google API calendar goes quiet the moment Google rejects the saved OAuth
// login: its events simply vanish from the agenda. The fetcher reports that
// per calendar as an "auth_expired:" / "auth_required:" status; this pairs
// those statuses with the configured Google API calendars so the panel can
// say what happened and offer a reconnect instead of showing an empty day.
function googleAuthIssue(configuredList, activeList) {
  var configured = configuredList || []
  var googleNames = {}
  var hasGoogleApi = false
  for (var i = 0; i < configured.length; i++) {
    var c = configured[i]
    if (!c || c.enabled === false || !c.googleCalendarId) continue
    googleNames[String(c.name || "")] = true
    hasGoogleApi = true
  }
  if (!hasGoogleApi) return null

  var active = activeList || []
  var kind = null
  var names = []
  for (var j = 0; j < active.length; j++) {
    var a = active[j]
    if (!a || !googleNames[String(a.name || "")]) continue
    var status = String(a.status || "")
    var thisKind = null
    if (status.indexOf("auth_expired") === 0) thisKind = "expired"
    else if (status.indexOf("auth_required") === 0) thisKind = "required"
    if (!thisKind) continue
    names.push(String(a.name || "Google Calendar"))
    if (kind !== "expired") kind = thisKind
  }
  if (!kind) return null
  return { kind: kind, names: names }
}

function googleAuthIssueText(issue) {
  if (!issue) return ""
  var who = issue.names.length > 0 ? issue.names.join(", ") : "Google Calendar"
  if (issue.kind === "expired") return "Google login expired for " + who + ". Click to reconnect."
  return who + " needs a Google login. Click to connect."
}

function formatAgendaMarkdown(events, selectedDateLabel, calendarName, hourCycle) {
  if (!events || events.length === 0) return ""
  var header = "### Agenda – " + (selectedDateLabel || "Today")
  if (calendarName && calendarName !== "all") {
    header += " (" + calendarName + ")"
  }
  var lines = [header]
  for (var i = 0; i < events.length; i++) {
    var evt = events[i]
    if (!evt) continue
    var startTime = formatEventTime(evt.startTime, hourCycle)
    var endTime = formatEventTime(evt.endTime, hourCycle)
    var timeStr = evt.allDay ? "All Day" : (startTime + (endTime ? " – " + endTime : ""))
    var line = "- [ ] " + timeStr + " · " + (evt.title || "Untitled Event")
    if (evt.meetingProvider && evt.meetingUrl) {
      line += " ([" + evt.meetingProvider + "](" + evt.meetingUrl + "))"
    } else if (evt.location) {
      line += " (" + evt.location + ")"
    }
    lines.push(line)
  }
  return lines.join("\n")
}

function getWritableCalendars(configuredList) {
  var list = configuredList || []
  var writables = []
  var hasLocal = false
  for (var i = 0; i < list.length; i++) {
    var c = list[i]
    if (!c || c.enabled === false) continue
    var type = String(c.type || "").toLowerCase()
    if (type === "local") {
      hasLocal = true
      writables.push({
        name: c.name || "Local Calendar",
        type: "local",
        color: c.color || "#a6e3a1",
        calendarId: "local",
        writable: true
      })
    } else if (type === "timetree") {
      writables.push({
        name: c.name || "TimeTree",
        type: "timetree",
        color: c.color || "#4a6cf7",
        calendarId: c.calendarId,
        writable: true
      })
    } else if (type === "jmap" || c.jmapToken) {
      writables.push({
        name: c.name || "JMAP",
        type: "jmap",
        color: c.color || "#ff7700",
        calendarId: c.jmapCalendarId || c.calendarId || "primary",
        writable: true
      })
    } else if (c.caldavUrl && c.username && c.password) {
      writables.push({
        name: c.name || "CalDAV Calendar",
        type: "caldav",
        color: c.color || "#4A90E2",
        calendarId: c.caldavUrl,
        writable: true
      })
    } else if (c.googleCalendarId || (c.calendarId && !c.url)) {
      writables.push({
        name: c.name || "Google Calendar",
        type: "google",
        color: c.color || "#4285f4",
        calendarId: c.googleCalendarId || c.calendarId,
        writable: true
      })
    }
  }
  if (!hasLocal) {
    writables.push({
      name: "Local Calendar",
      type: "local",
      color: "#a6e3a1",
      calendarId: "local",
      writable: true
    })
  }
  return writables
}

// Parse a typed clock time into "HH:MM", or "" when it is not a valid time.
// Accepts 24-hour ("14:30", "9:05", "14.30", "1430", "9") and 12-hour
// ("2pm", "2:30 PM", "11:15 a.m.") input, since the form is a free-text field.
function parseTimeInput(text) {
  var s = String(text || "").toLowerCase().replace(/[\s.]/g, "")
  var match = /^(\d{1,2})(?::?(\d{2}))?(am|pm|a|p)?$/.exec(s)
  if (!match) return ""
  var h = parseInt(match[1], 10)
  var m = match[2] === undefined ? 0 : parseInt(match[2], 10)
  var meridiem = match[3]
  if (m > 59) return ""
  if (meridiem) {
    if (h < 1 || h > 12) return ""
    h = h % 12 + (meridiem.charAt(0) === "p" ? 12 : 0)
  } else if (h > 23) {
    return ""
  }
  return pad2(h) + ":" + pad2(m)
}

// Parse a typed "YYYY-MM-DD" (single-digit month/day allowed) into a date
// key, or "" when it is not a real calendar day.
function parseDateInput(text) {
  var match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(String(text || "").trim())
  if (!match) return ""
  var y = parseInt(match[1], 10)
  var m = parseInt(match[2], 10) - 1
  var d = parseInt(match[3], 10)
  var date = new Date(y, m, d)
  if (date.getFullYear() !== y || date.getMonth() !== m || date.getDate() !== d) return ""
  return dateKey(y, m, d)
}

// Which reminder stage a start `diffMin` minutes away falls in, or null.
// "staged" reminds at 10, 5 and 1 minute; a number reminds once that early.
function notificationStage(diffMin, noticeSetting) {
  var s = String(noticeSetting || "staged").toLowerCase()
  if (s === "staged" || s === "0") {
    if (diffMin <= 1 && diffMin >= 0) return 1
    if (diffMin <= 5 && diffMin > 1) return 5
    if (diffMin <= 10 && diffMin > 5) return 10
    return null
  }
  var mins = parseInt(s, 10) || 10
  if (diffMin >= 0 && diffMin <= mins) return mins
  return null
}

// The reminders due now: [{ key, title, body }], skipping keys already in
// `sentKeys`. `timeRange(evt)` formats the event's times for the body.
function dueNotifications(events, nowMs, noticeSetting, sentKeys, timeRange) {
  var due = []
  var seen = {}
  for (var i = 0; i < (events || []).length; i++) {
    var evt = events[i]
    if (!evt || evt.allDay || !evt.startIso) continue
    var startMs = new Date(evt.startIso).getTime()
    if (isNaN(startMs)) continue
    var diffMin = Math.round((startMs - nowMs) / 60000)
    if (diffMin < 0 || diffMin > 35) continue
    var stage = notificationStage(diffMin, noticeSetting)
    if (stage === null) continue
    var key = evt.id + "_" + evt.startIso + "_" + stage
    // A multi-day event is listed on each of its days: remind once.
    if ((sentKeys && sentKeys[key]) || seen[key]) continue
    seen[key] = true

    var body = []
    if (evt.calendar) body.push("[" + evt.calendar + "]")
    if (evt.startTime && timeRange) body.push(timeRange(evt))
    if (evt.meetingProvider) body.push("📹 " + evt.meetingProvider)
    else if (evt.location) body.push("📍 " + evt.location)
    due.push({
      key: key,
      title: (diffMin <= 1 ? "Starting now: " : "Upcoming in " + diffMin + "m: ") + evt.title,
      body: body.join("  ·  ")
    })
  }
  return due
}

// The events of `days` days from `fromKey`, in date order, as one flat list.
// Each event gets `dayKey`; the first kept event of a day also gets
// `dayHeading` (the same key), so the list can show a title per day.
// `keep(evt)` (optional) filters before the headings are placed.
function upcomingEvents(eventsByDate, fromKey, days, keep) {
  var out = []
  for (var i = 0; i < days; i++) {
    var key = stepDate(fromKey, i).dateKey
    var list = ((eventsByDate && eventsByDate[key]) || []).filter(function(e) { return !keep || keep(e) })
    for (var j = 0; j < list.length; j++) {
      var evt = Object.assign({}, list[j], { dayKey: key })
      if (j === 0) evt.dayHeading = key
      out.push(evt)
    }
  }
  return out
}

// Quick add: "Lunch tomorrow 1pm", "Standup mon 9:30-9:45", "Dentiste
// demain 14h", "Call friday at 3pm for 30m", "Review 2026-10-12".
// Returns { title, date, start, end }: date "YYYY-MM-DD" or "", times
// "HH:MM" or "". Words it does not understand stay in the title.
var QUICK_DAY_OFFSETS = {
  "today": 0, "tonight": 0, "aujourd'hui": 0, "ce soir": 0,
  "tomorrow": 1, "tmrw": 1, "demain": 1, "après-demain": 2, "apres-demain": 2
}
// Full names only: "Sam" or "sun" in a title must not move the event.
var QUICK_WEEKDAYS = {
  "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6,
  "dimanche": 0, "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6
}
var QUICK_TIME = "(\\d{1,2}(?:[:.h]\\d{2})?\\s*(?:am|pm|a\\.m\\.|p\\.m\\.|h)?)"

function quickTime(token, meridiemHint) {
  var t = String(token || "").toLowerCase().replace(/\s+/g, "").replace(/\./g, function(m, i, str) {
    return /\d/.test(str.charAt(i - 1)) && /\d/.test(str.charAt(i + 1)) ? ":" : ""
  })
  t = t.replace(/h(\d{2})$/, ":$1").replace(/h$/, "")
  if (meridiemHint && !/[ap]m?$/.test(t)) t += meridiemHint
  return parseTimeInput(t)
}

function hasTimeMarker(token) {
  return /[:.h]|am|pm|a\.m|p\.m/i.test(String(token || ""))
}

function addMinutes(hhmm, minutes) {
  var parts = hhmm.split(":")
  var total = (parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + minutes) % 1440
  return pad2(Math.floor(total / 60)) + ":" + pad2(total % 60)
}

function parseQuickAdd(text, todayKey) {
  var rest = " " + String(text || "").replace(/\s+/g, " ").trim() + " "
  var result = { title: "", date: "", start: "", end: "" }
  function take(re, fn) {
    var m = re.exec(rest)
    if (m && fn(m) !== false) rest = rest.slice(0, m.index) + " " + rest.slice(m.index + m[0].length)
  }
  var today = parseDateInput(todayKey) || dateKey(new Date().getFullYear(), new Date().getMonth(), new Date().getDate())
  var todayDow = new Date(Date.UTC(+today.slice(0, 4), +today.slice(5, 7) - 1, +today.slice(8, 10))).getUTCDay()

  // Times: a range first ("1-2pm", "9:30-10:15", "13h-14h30"), then one time.
  take(new RegExp("\\s(?:at |à |@ ?)?" + QUICK_TIME + "\\s*(?:-|–|to|à)\\s*" + QUICK_TIME + "(?=\\s)", "i"), function(m) {
    if (!hasTimeMarker(m[1]) && !hasTimeMarker(m[2])) return false
    var hint = (/(am|pm)\s*$/i.exec(m[2]) || [])[1]
    var start = quickTime(m[1], hint && !/(am|pm)/i.test(m[1]) ? hint.toLowerCase() : "")
    var end = quickTime(m[2])
    if (!start || !end) return false
    result.start = start
    result.end = end
  })
  if (!result.start) {
    take(new RegExp("\\s(?:(at |à |@ ?)" + QUICK_TIME + "|" + QUICK_TIME + ")(?=\\s)", "i"), function(m) {
      var token = m[2] || m[3]
      if (!m[1] && !hasTimeMarker(token)) return false
      var start = quickTime(token)
      if (!start) return false
      result.start = start
    })
  }
  if (result.start && !result.end) {
    var minutes = 60
    take(/\s(?:for|pendant) (\d+(?:[.,]\d+)?) ?(h|hours?|hrs?|heures?|m|mins?|minutes?)(?=\s)/i, function(m) {
      var n = parseFloat(m[1].replace(",", "."))
      minutes = Math.round(/^h/i.test(m[2]) ? n * 60 : n)
    })
    result.end = addMinutes(result.start, Math.max(1, minutes))
    // The form edits one day: an event late in the evening ends at midnight.
    if (result.end <= result.start) result.end = "23:59"
  }

  // Dates.
  take(/\s(\d{4}-\d{1,2}-\d{1,2})(?=\s)/, function(m) {
    var d = parseDateInput(m[1])
    if (!d) return false
    result.date = d
  })
  if (!result.date) {
    take(/\s(?:in|dans) (\d{1,3}) (?:days?|jours?)(?=\s)/i, function(m) {
      result.date = stepDate(today, parseInt(m[1], 10)).dateKey
    })
  }
  if (!result.date) {
    var dayWords = Object.keys(QUICK_DAY_OFFSETS).sort(function(a, b) { return b.length - a.length })
    take(new RegExp("\\s(" + dayWords.join("|") + ")(?=\\s)", "i"), function(m) {
      result.date = stepDate(today, QUICK_DAY_OFFSETS[m[1].toLowerCase()]).dateKey
    })
  }
  if (!result.date) {
    take(new RegExp("\\s(?:(next|prochain) |on |le )?(" + Object.keys(QUICK_WEEKDAYS).join("|") + ")( prochain)?(?=\\s)", "i"), function(m) {
      var ahead = (QUICK_WEEKDAYS[m[2].toLowerCase()] - todayDow + 7) % 7
      if (ahead === 0 && (m[1] || m[3])) ahead = 7
      result.date = stepDate(today, ahead).dateKey
    })
  }

  result.title = rest.replace(/\s(?:at|à|on|le)\s*$/i, " ").replace(/\s+/g, " ").trim()
  return result
}

function calculateEndTime(startTimeStr, durationMinutes) {
  var start = parseTimeInput(startTimeStr)
  if (!start) return "10:00"
  var parts = start.split(":")
  var total = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + (durationMinutes || 60)
  var endH = Math.floor(total / 60) % 24
  var endM = total % 60
  return pad2(endH) + ":" + pad2(endM)
}

if (typeof module !== "undefined") {
  module.exports = {
    dateKey: dateKey,
    keyForDate: keyForDate,
    normalizedWeekStart: normalizedWeekStart,
    weekStartSettingName: weekStartSettingName,
    toggledWeekStart: toggledWeekStart,
    weekdayOrder: weekdayOrder,
    isoWeek: isoWeek,
    dayOfYear: dayOfYear,
    daysInYear: daysInYear,
    yearProgress: yearProgress,
    yearProgressPercent: yearProgressPercent,
    parseAge: parseAge,
    parseBirthYear: parseBirthYear,
    ageFromBirthYear: ageFromBirthYear,
    parseLifeExpectancy: parseLifeExpectancy,
    lifeProgress: lifeProgress,
    lifeProgressPercent: lifeProgressPercent,
    monthGrid: monthGrid,
    stepMonth: stepMonth,
    clockFormats: clockFormats,
    clockFormatRing: clockFormatRing,
    nextClockFormat: nextClockFormat,
    normalizedHourCycle: normalizedHourCycle,
    clockFormatForHourCycle: clockFormatForHourCycle,
    hourCycleForClockFormat: hourCycleForClockFormat,
    formatEventTime: formatEventTime,
    isoWeekLiteral: isoWeekLiteral,
    parseEventsFile: parseEventsFile,
    formatSelectedDateLabel: formatSelectedDateLabel,
    CALENDAR_COLORS: CALENDAR_COLORS,
    cycleCalendarColor: cycleCalendarColor,
    parseCalendarsConfig: parseCalendarsConfig,
    formatAgendaMarkdown: formatAgendaMarkdown,
    getWritableCalendars: getWritableCalendars,
    parseTimeInput: parseTimeInput,
    notificationStage: notificationStage,
    dueNotifications: dueNotifications,
    upcomingEvents: upcomingEvents,
    parseQuickAdd: parseQuickAdd,
    parseDateInput: parseDateInput,
    calculateEndTime: calculateEndTime,
    stepDate: stepDate,
    stepToMonthBound: stepToMonthBound,
    stepToWeekBound: stepToWeekBound
  }
}
