import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The clock's calendar popup with Google / Apple Calendar event sync:
// a month grid with ISO week numbers and synced agenda view.
Panel {
  id: root
  moduleName: "promaa.clock"
  ipcTarget: "promaa.clock"
  manageIpc: false

  property var anchorItem: null

  // The bar tracks the widget mounted in its slot — BarWidget.qml — not this
  // nested panel. Everything the bar identifies a panel by has to be that
  // widget: the popout coordinator (and with it the open-panel dot under the
  // pill) compares against `slot.activeItem`, and switchPanelFrom looks the
  // slot up the same way.
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // ---- Today. SystemClock keeps this honest across midnight so the
  //      highlight rolls over without the panel being reopened.
  property date today: new Date()
  readonly property string todayKey: Model.keyForDate(today)

  // ---- Selected date for the Agenda view
  property string selectedDateKey: todayKey

  // ---- Synced calendar data from ~/.local/state/omarchy/calendar-events.json
  property var eventsData: Model.parseEventsFile(eventsFile.text())
  readonly property var eventsByDate: eventsData.eventsByDate || ({})
  readonly property var selectedEvents: eventsByDate[selectedDateKey] || []
  readonly property string selectedDateLabel: Model.formatSelectedDateLabel(selectedDateKey, todayKey, Qt.locale())
  readonly property int configuredCalendarCount: eventsData.configuredCount || 0
  property double lastSyncTimestamp: 0
  readonly property bool syncRunning: fetchProc.running
  readonly property bool notifyUpcomingEvents: root.setting("notifyUpcomingEvents", true)
  readonly property var notifyMinutesBefore: root.setting("notifyMinutesBefore", "staged")
  readonly property int syncIntervalMinutes: root.setting("syncIntervalMinutes", 15)
  readonly property bool enableMeetingLinks: root.setting("enableMeetingLinks", true)
  readonly property bool showNextEvent: root.setting("showNextEvent", true)
  readonly property string nextEventHorizon: String(root.setting("nextEventHorizon", "today"))
  readonly property int clockHourCycle: Model.normalizedHourCycle(root.setting(
    "clockHourCycle",
    Model.hourCycleForClockFormat(root.setting("format", "dddd HH:mm"), 24)
  ))
  property var notifiedEventKeys: ({})

  // ---- Calendar Filtering & Agenda Markdown Copy
  property string activeCalendarFilter: "all"
  readonly property var activeCalendars: eventsData.calendars || []
  readonly property var displayedEvents: {
    var list = root.selectedEvents || []
    if (root.activeCalendarFilter === "all" || !root.activeCalendarFilter) return list
    return list.filter(function(e) { return e.calendar === root.activeCalendarFilter })
  }
  property bool agendaCopied: false

  // ---- Event Creation & Management State
  property bool addingEvent: false
  property string eventTitle: ""
  property string eventCalendar: ""
  property string eventDate: selectedDateKey
  property string eventStartTime: "09:00"
  property string eventEndTime: "10:00"
  property bool eventAllDay: false
  property string eventLocation: ""
  property string eventDescription: ""
  property bool eventSubmitting: false
  property string eventErrorMessage: ""
  property string pendingEventPayloadJson: ""
  property string pendingDeletePayloadJson: ""
  readonly property var writableCalendars: Model.getWritableCalendars(root.configuredCalendars)

  function openAddEvent(dateKey) {
    root.showingSettings = false
    root.addingCalendar = false
    root.eventTitle = ""
    root.eventDate = dateKey || root.selectedDateKey || root.todayKey
    root.eventStartTime = "09:00"
    root.eventEndTime = "10:00"
    root.eventAllDay = false
    root.eventLocation = ""
    root.eventDescription = ""
    root.eventErrorMessage = ""
    root.eventSubmitting = false
    var writables = root.writableCalendars
    if (writables && writables.length > 0) {
      root.eventCalendar = writables[0].name
    } else {
      root.eventCalendar = "Local Calendar"
    }
    root.addingEvent = true
  }

  function closeAddEvent() {
    root.addingEvent = false
    root.eventSubmitting = false
    root.eventErrorMessage = ""
  }

  function setEventDuration(minutes) {
    root.eventEndTime = Model.calculateEndTime(root.eventStartTime, minutes)
  }

  function submitNewEvent() {
    if (!root.eventTitle.trim()) {
      root.eventErrorMessage = "Please enter an event title"
      return
    }
    root.eventSubmitting = true
    root.eventErrorMessage = ""

    var startIso = root.eventAllDay ? root.eventDate : (root.eventDate + "T" + root.eventStartTime + ":00")
    var endIso = root.eventAllDay ? root.eventDate : (root.eventDate + "T" + root.eventEndTime + ":00")

    var payload = {
      title: root.eventTitle.trim(),
      calendar: root.eventCalendar || "Local Calendar",
      start: startIso,
      end: endIso,
      allDay: root.eventAllDay,
      location: root.eventLocation.trim(),
      description: root.eventDescription.trim()
    }

    pendingEventPayloadJson = JSON.stringify(payload)
    createEventProc.command = [
      "python3",
      Qt.resolvedUrl("fetch-events.py").toString().replace(/^file:\/\//, ""),
      "--create-event"
    ]
    createEventProc.running = true
  }

  function deleteEvent(evt) {
    if (!evt || !evt.id) return
    var payload = {
      id: evt.id,
      calendar: evt.calendar || "Local Calendar",
      calendarId: evt.calendarId || "",
      calendarType: evt.calendarType || "local"
    }
    pendingDeletePayloadJson = JSON.stringify(payload)
    deleteEventProc.command = [
      "python3",
      Qt.resolvedUrl("fetch-events.py").toString().replace(/^file:\/\//, ""),
      "--delete-event"
    ]
    deleteEventProc.running = true
  }

  // ---- Settings Menu State
  property bool showingSettings: false
  property string settingsTab: "preferences"
  property var configuredCalendars: Model.parseCalendarsConfig(configFile.text())
  readonly property bool isGoogleAuthenticated: eventsData.authenticated === true

  property bool addingCalendar: false
  property string formName: ""
  property string formType: "url" // "url", "googleId", "jmap", "local"
  property string formAddress: ""
  property string formJmapUrl: ""
  property string formJmapToken: ""
  property string formColor: "#4285f4"
  property string pendingConfigJson: ""

  function openSettings(tab) {
    showingSettings = true
    addingCalendar = false
    addingEvent = false
    if (tab) settingsTab = tab
    if (calendarScroll) calendarScroll.contentY = 0
    configFile.reload()
  }

  function closeSettings() {
    showingSettings = false
    addingCalendar = false
    if (calendarScroll) calendarScroll.contentY = 0
  }

  function saveCalendars(list) {
    pendingConfigJson = JSON.stringify(list, null, 2)
    saveConfigProc.command = [
      "python3",
      Qt.resolvedUrl("fetch-events.py").toString().replace(/^file:\/\//, ""),
      "--save-config"
    ]
    saveConfigProc.running = true
  }

  function toggleCalendarEnabled(index) {
    var list = JSON.parse(JSON.stringify(root.configuredCalendars))
    if (index >= 0 && index < list.length) {
      list[index].enabled = list[index].enabled === false ? true : false
      saveCalendars(list)
    }
  }

  function removeCalendar(index) {
    var list = JSON.parse(JSON.stringify(root.configuredCalendars))
    if (index >= 0 && index < list.length) {
      list.splice(index, 1)
      saveCalendars(list)
    }
  }

  function cycleColorForCalendar(index) {
    var list = JSON.parse(JSON.stringify(root.configuredCalendars))
    if (index >= 0 && index < list.length) {
      list[index].color = Model.cycleCalendarColor(list[index].color)
      saveCalendars(list)
    }
  }

  function startAddingCalendar(type) {
    root.settingsTab = "calendars"
    formName = ""
    formType = type || "url"
    formAddress = ""
    formJmapUrl = "https://api.fastmail.com/jmap/session"
    formJmapToken = ""
    formColor = formType === "googleId" ? "#e01b24" : (formType === "jmap" ? "#ff7700" : (formType === "local" ? "#a6e3a1" : "#4285f4"))
    addingCalendar = true
    if (calendarScroll) calendarScroll.contentY = 0
  }

  function commitNewCalendar() {
    if (!formName.trim()) return
    var list = JSON.parse(JSON.stringify(root.configuredCalendars))
    var item = {
      name: formName.trim(),
      color: formColor,
      enabled: true
    }
    if (formType === "googleId") {
      if (!formAddress.trim()) return
      item.googleCalendarId = formAddress.trim()
    } else if (formType === "jmap") {
      if (!formJmapToken.trim()) return
      item.type = "jmap"
      item.jmapUrl = formJmapUrl.trim() || "https://api.fastmail.com/jmap/session"
      item.jmapToken = formJmapToken.trim()
      if (formAddress.trim()) {
        item.calendarId = formAddress.trim()
      }
    } else if (formType === "local") {
      item.type = "local"
    } else {
      if (!formAddress.trim()) return
      item.url = formAddress.trim()
    }
    list.push(item)
    saveCalendars(list)
    addingCalendar = false
  }

  function openGoogleAuth() {
    if (!googleAuthProc.running) googleAuthProc.running = true
  }

  // The month on screen. Stepping moves this and nothing else: the grid is
  // a read-out, not a picker, so there is no per-day cursor to keep in sync.
  property int viewYear: today.getFullYear()
  property int viewMonth: today.getMonth()

  readonly property date viewDate: new Date(viewYear, viewMonth, 1)
  readonly property bool viewingCurrentMonth: viewYear === today.getFullYear() && viewMonth === today.getMonth()


  // Pinned to today, not to the month being browsed — stepping through the
  // calendar does not change how much of the year is gone.
  readonly property real yearDone: Model.yearProgress(today.getFullYear(), today.getMonth(), today.getDate())
  readonly property int yearDonePercent: Model.yearProgressPercent(today.getFullYear(), today.getMonth(), today.getDate())

  // Memento mori, for anyone who goes looking: double-tapping the year bar
  // asks for a birth year and a life expectancy, and a second bar tracks one
  // against the other. A birth year rather than an age, so it keeps counting
  // on its own. Without one the bar stays hidden.
  readonly property int birthYear: Model.parseBirthYear(setting("birthYear", 0), today.getFullYear())
  readonly property int age: Model.ageFromBirthYear(birthYear, today.getFullYear())
  readonly property int lifeExpectancy: Model.parseLifeExpectancy(setting("lifeExpectancy", 0))
  readonly property real lifeDone: Model.lifeProgress(age, lifeExpectancy)
  readonly property int lifeDonePercent: Model.lifeProgressPercent(age, lifeExpectancy)
  property bool editingLife: false

  // Unset falls through to the locale's own first day, so a fresh install
  // starts out matching the rest of the desktop rather than a hardcoded
  // convention. Clicking the grid's "W" heading writes the choice back to
  // shell.json.
  readonly property int weekStart: Model.normalizedWeekStart(setting("weekStartDay", null), Qt.locale().firstDayOfWeek)
  readonly property string nextWeekStartLabel: Qt.locale().dayName(Model.toggledWeekStart(weekStart), Locale.LongFormat)
  readonly property var weekdays: Model.weekdayOrder(weekStart)
  readonly property var weeks: Model.monthGrid(viewYear, viewMonth, weekStart, todayKey)


  // Guarded so the widget renders before the bar is injected (the bar-widget
  // contract instantiates it bare).
  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int cellWidth: Style.space(52)
  readonly property int cellHeight: Style.space(36)
  readonly property int cellSpacing: Style.space(2)
  readonly property int weekColumnWidth: Style.space(32)
  readonly property int gutterWidth: Style.space(14)

  function open() {
    refresh()
    syncCalendars(false)
    eventsFile.reload()
    root.controller.show()
    // Set after showing, not before: showing hands the popout coordinator
    // over, which closes whichever panel was open, and that close clears the
    // shared flag. Deferring means the panel taking over always wins, while
    // a handoff to a panel that does not manage the flag still leaves it
    // cleared rather than stuck on.
    Qt.callLater(function() {
      if (root.opened) setCenterHoverRevealSuppressed(true)
    })
  }

  function close() {
    setCenterHoverRevealSuppressed(false)
    // Dismissing the panel mid-edit would otherwise leave the inputs up,
    // waiting behind a closed popup for the next time it opens.
    if (root.editingLife) root.cancelEditingLife()
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  // Summoning by hotkey moves no pointer, so a hover the bar was still
  // holding must not keep the center indicators revealed behind the panel.
  function setCenterHoverRevealSuppressed(value) {
    if (root.bar && "centerHoverRevealSuppressed" in root.bar)
      root.bar.centerHoverRevealSuppressed = value
  }

  function syncCalendars(force) {
    var now = Date.now()
    if (!force && (now - lastSyncTimestamp < 30000)) return
    lastSyncTimestamp = now
    if (!fetchProc.running) fetchProc.running = true
  }

  function getNotificationStage(diffMin, noticeSetting) {
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

  function checkUpcomingNotifications() {
    if (!root.notifyUpcomingEvents) return
    var todayEvents = root.eventsByDate[root.todayKey] || []
    var nowMs = Date.now()
    var settingVal = root.notifyMinutesBefore

    for (var i = 0; i < todayEvents.length; i++) {
      var evt = todayEvents[i]
      if (evt.allDay || !evt.startIso) continue
      var startMs = new Date(evt.startIso).getTime()
      if (isNaN(startMs)) continue
      var diffMin = Math.round((startMs - nowMs) / 60000)
      if (diffMin < 0 || diffMin > 35) continue

      var stage = root.getNotificationStage(diffMin, settingVal)
      if (stage !== null) {
        var key = evt.id + "_" + evt.startIso + "_" + stage
        if (!root.notifiedEventKeys[key]) {
          var updated = Object.assign({}, root.notifiedEventKeys)
          updated[key] = true
          root.notifiedEventKeys = updated

          var stagePrefix = diffMin <= 1 ? "Starting now: " : ("Upcoming in " + diffMin + "m: ")
          var titleStr = stagePrefix + evt.title
          var bodyParts = []
          if (evt.calendar) bodyParts.push("[" + evt.calendar + "]")
          if (evt.startTime) bodyParts.push(root.eventTimeRange(evt, " - "))
          if (evt.meetingProvider) bodyParts.push("📹 " + evt.meetingProvider)
          else if (evt.location) bodyParts.push("📍 " + evt.location)
          var bodyStr = bodyParts.join("  ·  ")

          root.sendDesktopNotification(titleStr, bodyStr)
        }
      }
    }
  }

  function copyAgendaMarkdown() {
    var events = root.displayedEvents
    if (!events || events.length === 0) return
    var md = Model.formatAgendaMarkdown(events, root.selectedDateLabel, root.activeCalendarFilter, root.clockHourCycle)
    if (!md) return

    copyProc.command = ["sh", "-c", "printf '%s' \"$1\" | (wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null)", "--", md]
    copyProc.running = true

    root.agendaCopied = true
    copyFeedbackTimer.restart()
  }

  function sendDesktopNotification(title, body) {
    notifyProc.command = ["notify-send", "-a", "Omarchy Calendar", "-i", "x-office-calendar", String(title || "Omarchy Calendar"), String(body || "")]
    notifyProc.running = true
  }

  function openExternalUrl(url) {
    if (!url || typeof url !== "string") return
    var targetUrl = url.trim()
    if (!targetUrl) return
    // Validate scheme and characters before passing to Qt.openUrlExternally / xdg-open
    if (!/^https?:\/\/[a-zA-Z0-9.\-]+(?::\d+)?(\/[^\s<>"'`]*)?$/i.test(targetUrl)) return
    if (typeof Qt.openUrlExternally === "function") {
      Qt.openUrlExternally(targetUrl)
    } else {
      openUrlProc.command = ["xdg-open", targetUrl]
      openUrlProc.running = true
    }
  }

  function selectDate(key, inMonth, year, month) {
    root.selectedDateKey = key
    if (!inMonth && year !== undefined && month !== undefined) {
      root.viewYear = year
      root.viewMonth = month
    }
  }

  function refresh() {
    root.today = new Date()
    root.goToToday()
  }

  function goToToday() {
    root.viewYear = today.getFullYear()
    root.viewMonth = today.getMonth()
    root.selectedDateKey = todayKey
  }

  function moveMonth(delta) {
    var next = Model.stepMonth(viewYear, viewMonth, delta)
    root.viewYear = next.year
    root.viewMonth = next.month
  }

  function moveYear(delta) {
    moveMonth(delta * 12)
  }


  // Applied locally first so the panel redraws on the click itself; the
  // shell.json write comes back through the bar as the same value. With no
  // writable entry (the widget is not in the layout) it stays a session-only
  // preference rather than doing nothing. The host widget builds its own
  // entry when the label format is cycled, so it has to be kept in step or
  // it would write this key straight back out from a stale copy.
  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) entry[key] = values[key]

    root.settings = entry
    if (root.hostWidget && "settings" in root.hostWidget) root.hostWidget.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  // Bar calendar allowlist: always an array, strictly applied. Only listed
  // calendars show; a missing or empty list shows nothing on the bar.
  function barCalendarShown(name) {
    var list = root.setting("nextEventCalendars", [])
    return Model.isList(list) && list.indexOf(name) !== -1
  }

  function toggleBarCalendar(name) {
    var stored = root.setting("nextEventCalendars", [])
    var checked = Model.isList(stored) ? stored.slice() : []
    var index = checked.indexOf(name)
    if (index === -1) checked.push(name)
    else checked.splice(index, 1)
    checked.sort()
    root.persistSettings({ nextEventCalendars: checked })
  }

  function setClockHourCycle(hourCycle) {
    var cycle = Model.normalizedHourCycle(hourCycle)
    if (cycle === root.clockHourCycle) return
    persistSettings({
      clockHourCycle: cycle,
      format: Model.clockFormatForHourCycle(root.setting("format", "dddd HH:mm"), cycle),
      verticalFormat: Model.clockFormatForHourCycle(root.setting("verticalFormat", "HH\n—\nmm"), cycle)
    })
  }

  function eventTimeRange(event, separator) {
    if (!event) return ""
    var start = Model.formatEventTime(event.startTime, root.clockHourCycle)
    var end = Model.formatEventTime(event.endTime, root.clockHourCycle)
    return start + (end ? (separator || " – ") + end : "")
  }

  function setWeekStart(day) {
    var next = Model.normalizedWeekStart(day, root.weekStart)
    if (next === root.weekStart) return
    persistSettings({ weekStartDay: Model.weekStartSettingName(next) })
  }

  function startEditingLife() {
    root.editingLife = true
    Qt.callLater(function() {
      bornField.text = root.birthYear > 0 ? String(root.birthYear) : ""
      expectancyField.text = String(root.lifeExpectancy)
      bornField.selectAll()
      bornField.forceActiveFocus()
    })
  }

  function cancelEditingLife() {
    root.editingLife = false
    Qt.callLater(function() { if (keyCatcher) keyCatcher.forceActiveFocus() })
  }

  // Shared by both fields: Tab hops to the other one, Enter commits the pair,
  // Escape drops the lot.
  function handleLifeKey(event, other) {
    if (event.key === Qt.Key_Escape) {
      root.cancelEditingLife()
      event.accepted = true
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      root.commitLife()
      event.accepted = true
    } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      other.selectAll()
      other.forceActiveFocus()
      event.accepted = true
    }
  }

  // Double-tapping the life bar puts it away again. The expectancy stays in
  // the config so setting a birth year again brings your own number back
  // rather than the default.
  function clearLife() {
    if (root.birthYear <= 0) return
    persistSettings({ birthYear: 0 })
  }

  function commitLife() {
    var born = Model.parseBirthYear(bornField.text, today.getFullYear())
    var span = Model.parseLifeExpectancy(expectancyField.text)
    if (born !== root.birthYear || span !== root.lifeExpectancy)
      persistSettings({ birthYear: born, lifeExpectancy: span })
    cancelEditingLife()
  }

  function toggleWeekStart() {
    setWeekStart(Model.toggledWeekStart(root.weekStart))
  }

  // Locale short day names, trimmed of the trailing period some locales
  // carry ("man." -> "MAN") so the header row stays a clean band of caps.
  function weekdayLabel(weekday) {
    return String(Qt.locale().dayName(weekday, Locale.ShortFormat)).replace(/\.$/, "").toUpperCase()
  }

  FileView {
    id: eventsFile
    path: Quickshell.env("HOME") + "/.local/state/omarchy/calendar-events.json"
    watchChanges: true
    printErrors: false
    onFileChanged: {
      reload()
      root.checkUpcomingNotifications()
    }
  }

  FileView {
    id: configFile
    path: Quickshell.env("HOME") + "/.config/omarchy/calendars.json"
    watchChanges: true
    printErrors: false
    onFileChanged: root.syncCalendars(true)
  }

  // Nothing else reads the event cache until the panel is opened or the
  // sync timer fires, so after a shell restart the bar's next-event label
  // stayed empty. Load the cache immediately and kick a background refresh.
  Component.onCompleted: {
    eventsFile.reload()
    root.syncCalendars(false)
  }

  Process {
    id: fetchProc
    command: ["python3", Qt.resolvedUrl("fetch-events.py").toString().replace(/^file:\/\//, "")]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        eventsFile.reload()
        root.checkUpcomingNotifications()
      }
    }
  }

  Process {
    id: notifyProc
  }

  Process {
    id: openUrlProc
  }

  Process {
    id: copyProc
  }

  Process {
    id: saveConfigProc
    stdinEnabled: true
    onStarted: {
      write(root.pendingConfigJson + "\n")
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.pendingConfigJson = ""
        configFile.reload()
        root.syncCalendars(true)
      }
    }
  }

  Process {
    id: createEventProc
    stdinEnabled: true
    onStarted: {
      write(root.pendingEventPayloadJson + "\n")
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.eventSubmitting = false
        root.pendingEventPayloadJson = ""
        root.addingEvent = false
        eventsFile.reload()
      }
    }
  }

  Process {
    id: deleteEventProc
    stdinEnabled: true
    onStarted: {
      write(root.pendingDeletePayloadJson + "\n")
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.pendingDeletePayloadJson = ""
        eventsFile.reload()
      }
    }
  }

  Process {
    id: googleAuthProc
    command: ["python3", Qt.resolvedUrl("google-auth.py").toString().replace(/^file:\/\//, "")]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.syncCalendars(true)
    }
  }

  Timer {
    id: autoSyncTimer
    interval: Math.max(60000, root.syncIntervalMinutes * 60000)
    repeat: true
    running: root.syncIntervalMinutes > 0
    onTriggered: root.syncCalendars(false)
  }

  Timer {
    id: notifCheckTimer
    interval: 30000
    repeat: true
    running: root.notifyUpcomingEvents
    onTriggered: root.checkUpcomingNotifications()
  }

  Timer {
    id: copyFeedbackTimer
    interval: 2000
    repeat: false
    onTriggered: root.agendaCopied = false
  }

  SystemClock {
    id: clock
    precision: SystemClock.Minutes
    onDateChanged: {
      root.checkUpcomingNotifications()
      if (Model.keyForDate(clock.date) === String(root.todayKey)) return
      var followToday = root.viewingCurrentMonth
      root.today = clock.date
      if (followToday) root.goToToday()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(calendarColumn.height)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editingLife || root.showingSettings || root.addingEvent
      onMoveRequested: function(dx, dy) {
        if (dx !== 0) root.moveMonth(dx)
        if (dy !== 0) root.moveYear(dy)
      }
      onActivateRequested: root.goToToday()
      onCloseRequested: {
        if (root.addingEvent) root.closeAddEvent()
        else if (root.showingSettings) root.closeSettings()
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "[") root.moveMonth(-1)
        else if (t === "]") root.moveMonth(1)
        else if (t === "{") root.moveYear(-1)
        else if (t === "}") root.moveYear(1)
        else if (t === "t" || t === "T") root.goToToday()
        else if (t === "w" || t === "W") root.toggleWeekStart()
        else if (t === "y" || t === "Y") root.copyAgendaMarkdown()
        else if (t === "n" || t === "N") root.openAddEvent(root.selectedDateKey)
      }


      Flickable {
        id: calendarScroll
        anchors.fill: parent
        contentWidth: calendarColumn.width
        contentHeight: calendarColumn.height
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height || contentWidth > width

        Column {
          id: calendarColumn
          width: Math.max(calendarScroll.width, gridColumn.width)
          spacing: Style.space(8)

          Column {
            id: mainCalendarSection
            visible: !root.showingSettings
            width: parent.width
            height: visible ? childrenRect.height : 0
            spacing: Style.space(8)

            // ---- Hero: today, centered. Once the view has stepped back
            //      it is also the way home — clicking the date you are
            //      looking for beats hunting for a reset button.
            Item {
              width: parent.width
              height: heroRow.height


            Row {
              id: heroRow
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.space(22)

              Text {
                textFormat: Text.PlainText
                // Baseline-aligned, not center-aligned: "July 26" carries a
                // descender, so centering the two boxes leaves the icon
                // sitting visibly low against the digits.
                anchors.baseline: heroDate.baseline
                text: "󰃭"
                color: heroMouse.containsMouse
                  ? Style.hoverStateColor(root.contentForeground, Color.accent)
                  : root.contentForeground
                font.family: root.contentFontFamily
                // Decorative, and deliberately outside the Style.font.*
                // scale. Sized so the glyph reads at the cap height of the
                // date beside it rather than towering over it.
                font.pixelSize: 48
              }

              Text {
                textFormat: Text.PlainText
                id: heroDate
                anchors.verticalCenter: parent.verticalCenter
                text: Qt.formatDate(root.today, "MMMM d")
                color: heroMouse.containsMouse
                  ? Style.hoverStateColor(root.contentForeground, Color.accent)
                  : root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: 52
                font.bold: true
              }
            }

            MouseArea {
              id: heroMouse
              x: heroRow.x
              y: heroRow.y
              width: heroRow.width
              height: heroRow.height
              enabled: !root.viewingCurrentMonth
              hoverEnabled: enabled
              cursorShape: Qt.PointingHandCursor
              onClicked: root.goToToday()

              PanelToolTip {
                visible: heroMouse.containsMouse
                text: "Back to today"
                fontFamily: root.contentFontFamily
              }
            }
          }

          // ---- Year progress, doubling as the rule under the hero:
          //      a plain hairline said nothing, and whole days done
          //      over days in the year says the same thing louder.
          Item {
            width: parent.width
            height: yearBlock.y + yearBlock.height

            Item {
              id: yearBlock
              y: Style.space(6)
              anchors.horizontalCenter: parent.horizontalCenter
              width: gridColumn.width
              height: Math.max(yearLabel.implicitHeight, Style.space(10))

              TapHandler {
                enabled: !root.editingLife
                onDoubleTapped: root.startEditingLife()
              }

              Row {
                visible: root.editingLife
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(10)

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: "BORN"
                  color: Qt.darker(root.contentForeground, 1.5)
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.letterSpacing: 1
                }

                TextField {
                  id: bornField
                  width: Style.space(70)
                  anchors.verticalCenter: parent.verticalCenter
                  placeholderText: "year"
                  foreground: root.contentForeground
                  font.family: root.contentFontFamily
                  inputMethodHints: Qt.ImhDigitsOnly

                  Keys.onPressed: function(event) { root.handleLifeKey(event, expectancyField) }
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.verticalCenterOffset: 0
                  leftPadding: Style.space(6)
                  text: "LIVE TO"
                  color: Qt.darker(root.contentForeground, 1.5)
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.letterSpacing: 1
                }

                TextField {
                  id: expectancyField
                  width: Style.space(60)
                  anchors.verticalCenter: parent.verticalCenter
                  placeholderText: "90"
                  foreground: root.contentForeground
                  font.family: root.contentFontFamily
                  inputMethodHints: Qt.ImhDigitsOnly

                  Keys.onPressed: function(event) { root.handleLifeKey(event, bornField) }
                }
              }

              Text {
                textFormat: Text.PlainText
                id: yearLabel
                visible: !root.editingLife
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: root.today.getFullYear()
                color: Qt.darker(root.contentForeground, 1.5)
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
                font.letterSpacing: 1
              }

              Text {
                textFormat: Text.PlainText
                id: yearPercent
                visible: !root.editingLife
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: root.yearDonePercent + "%"
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Rectangle {
                id: yearTrack
                visible: !root.editingLife
                anchors.left: yearLabel.right
                anchors.right: yearPercent.left
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                anchors.verticalCenter: parent.verticalCenter
                height: Style.space(6)
                radius: Style.cornerRadius > 0 ? height / 2 : 0
                color: Qt.rgba(root.contentForeground.r, root.contentForeground.g, root.contentForeground.b, 0.12)

                Rectangle {
                  width: Math.round(parent.width * root.yearDone)
                  height: parent.height
                  radius: parent.radius
                  color: Style.selectedStateColor(root.contentForeground, Color.accent)

                  Behavior on width { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                }
              }
            }
          }

          // ---- Memento mori. Only here once someone has gone looking and
          //      given an age; the same rail as the year above it, measured
          //      against a nominal lifetime.
          Item {
            visible: root.birthYear > 0
            width: parent.width
            height: visible ? lifeBlock.height : 0

            Item {
              id: lifeBlock
              anchors.horizontalCenter: parent.horizontalCenter
              width: gridColumn.width
              height: Math.max(lifeLabel.implicitHeight, Style.space(10))

              Text {
                textFormat: Text.PlainText
                id: lifeLabel
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: "LIFE"
                color: Qt.darker(root.contentForeground, 1.5)
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
                font.letterSpacing: 1
              }

              Text {
                textFormat: Text.PlainText
                id: lifePercent
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: root.lifeDonePercent + "%"
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Rectangle {
                anchors.left: lifeLabel.right
                anchors.right: lifePercent.left
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                anchors.verticalCenter: parent.verticalCenter
                height: Style.space(6)
                radius: Style.cornerRadius > 0 ? height / 2 : 0
                color: Qt.rgba(root.contentForeground.r, root.contentForeground.g, root.contentForeground.b, 0.12)

                Rectangle {
                  width: Math.round(parent.width * root.lifeDone)
                  height: parent.height
                  radius: parent.radius
                  color: Style.selectedStateColor(root.contentForeground, Color.accent)

                  Behavior on width { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                }
              }

              TapHandler {
                onDoubleTapped: root.clearLife()
              }

              MouseArea {
                id: lifeMouse
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.NoButton

                PanelToolTip {
                  visible: lifeMouse.containsMouse
                  text: "Memento Mori"
                  fontFamily: root.contentFontFamily
                }
              }
            }
          }

          // ---- Month grid: week numbers down a gutter on the left, then
          //      the seven day columns. Always six rows, so the popup is
          //      exactly as tall in February as it is in August.
          Item {
            width: parent.width
            height: gridColumn.y + gridColumn.height

            WheelHandler {
              onWheel: function(event) {
                // Horizontal wheels and touchpad side-scrolls report y === 0;
                // without this they would every one read as "next month".
                if (event.angleDelta.y === 0) return
                root.moveMonth(event.angleDelta.y > 0 ? -1 : 1)
              }
            }

            Column {
              id: gridColumn
              // The meter above is a solid rule; the grid needs room to
              // read as its own block rather than hanging off it.
              y: Style.space(18)
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.space(3)

              Row {
                id: headerRow
                spacing: root.cellSpacing

                // The week-number heading doubles as the week-start toggle.
                // It is the one control in the panel whose meaning is not
                // self-evident, so it carries a tooltip naming the day the
                // click will switch to.
                Rectangle {
                  width: root.weekColumnWidth
                  height: Style.space(16)
                  radius: Style.cornerRadius
                  color: weekStartMouse.containsMouse
                    ? Style.hoverFillFor(root.contentForeground, Color.accent)
                    : "transparent"

                  Text {
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: "W"
                    color: weekStartMouse.containsMouse
                      ? Style.hoverStateColor(root.contentForeground, Color.accent)
                      : Qt.darker(root.contentForeground, 1.9)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.letterSpacing: 1
                    font.bold: true
                  }

                  MouseArea {
                    id: weekStartMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.toggleWeekStart()
                  }

                  PanelToolTip {
                    visible: weekStartMouse.containsMouse
                    text: "Start weeks on " + root.nextWeekStartLabel
                    fontFamily: root.contentFontFamily
                  }
                }

                Item {
                  width: root.gutterWidth
                  height: Style.space(16)
                }

                Repeater {
                  model: root.weekdays

                  Text {
                    textFormat: Text.PlainText
                    required property var modelData
                    width: root.cellWidth
                    height: Style.space(16)
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    text: root.weekdayLabel(modelData)
                    color: Qt.darker(root.contentForeground, 1.5)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.letterSpacing: 1
                    font.bold: true
                  }
                }
              }

              Repeater {
                model: root.weeks

                Row {
                  required property var modelData
                  spacing: root.cellSpacing

                  Text {
                    textFormat: Text.PlainText
                    width: root.weekColumnWidth
                    height: root.cellHeight
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    text: modelData.week
                    color: Qt.darker(root.contentForeground, 1.9)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Item {
                    width: root.gutterWidth
                    height: root.cellHeight
                  }

                  Repeater {
                    model: modelData.days

                    Rectangle {
                      id: cellRect
                      required property var modelData
                      readonly property bool isSelected: modelData.key === root.selectedDateKey
                      readonly property var cellEvents: root.eventsByDate[modelData.key] || []

                      width: root.cellWidth
                      height: root.cellHeight
                      radius: Style.cornerRadius
                      color: isSelected
                        ? (modelData.today
                            ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18)
                            : Style.hoverFillFor(root.contentForeground, Color.accent))
                        : (cellMouse.containsMouse ? Style.hoverFillFor(root.contentForeground, Color.accent) : "transparent")
                      border.width: modelData.today ? Style.spacing.hairline : (isSelected ? Style.spacing.hairline : 0)
                      border.color: modelData.today
                        ? Style.normalBorderFor(root.contentForeground, Color.accent)
                        : (isSelected ? Style.selectedStateColor(root.contentForeground, Color.accent) : "transparent")

                      Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        anchors.verticalCenterOffset: cellRect.cellEvents.length > 0 ? -Style.space(3) : 0
                        text: modelData.day
                        color: cellRect.isSelected
                          ? root.contentForeground
                          : (modelData.inMonth
                              ? (modelData.weekend ? Qt.darker(root.contentForeground, 1.45) : root.contentForeground)
                              : Qt.darker(root.contentForeground, 2.2))
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.body
                        font.bold: modelData.today || cellRect.isSelected
                      }

                      Row {
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: Style.space(3)
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: Style.space(2)
                        visible: cellRect.cellEvents.length > 0

                        Repeater {
                          model: Math.min(cellRect.cellEvents.length, 3)
                          Rectangle {
                            required property int index
                            width: Style.space(4)
                            height: Style.space(4)
                            radius: width / 2
                            color: cellRect.cellEvents[index] && cellRect.cellEvents[index].color ? cellRect.cellEvents[index].color : Color.accent
                          }
                        }
                      }

                      MouseArea {
                        id: cellMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectDate(modelData.key, modelData.inMonth, modelData.year, modelData.month)
                      }
                    }

                  }
                }
              }
            }

            // Hairline down the week-number gutter, drawn only beside the
            // day rows so it does not cut through the header band.
            Rectangle {
              x: gridColumn.x + root.weekColumnWidth + root.cellSpacing + Math.round((root.gutterWidth - width) / 2)
              y: gridColumn.y + headerRow.height + gridColumn.spacing
              width: Style.spacing.hairline
              height: gridColumn.height - headerRow.height - gridColumn.spacing
              color: root.contentForeground
              opacity: 0.1
            }
          }

          // ---- Month stepping, spanning the grid it drives. The chevrons
          //      sit on the grid's outer bounds, the same edges the year
          //      rail above uses, so the row reads as the panel's other
          //      full-width rail instead of a cluster floating in space.
          //      The label is centered and fixed-width, so it holds still
          //      from "MAY" to "SEPTEMBER".
          Item {
            width: parent.width
            height: monthNav.height

            Item {
              id: monthNav
              anchors.horizontalCenter: parent.horizontalCenter
              width: gridColumn.width
              height: monthLabel.implicitHeight + Style.space(10)

              Text {
                textFormat: Text.PlainText
                id: monthLabel
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.verticalCenter: parent.verticalCenter
                // Fixed width so the chevrons hold still between a
                // "MAY 2026" and a "SEPTEMBER 2026".
                width: Style.space(130)
                horizontalAlignment: Text.AlignHCenter
                text: Qt.formatDate(root.viewDate, "MMMM yyyy").toUpperCase()
                color: Qt.darker(root.contentForeground, 1.4)
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.body
                font.letterSpacing: 1
              }

              PanelActionButton {
                // Pulled out by the button's own padding so the glyph, not
                // its hit box, lines up with the "2026" on the year rail.
                anchors.left: parent.left
                anchors.leftMargin: -Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰅁"
                tooltipText: "Previous month"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: root.moveMonth(-1)
              }

              PanelActionButton {
                anchors.right: parent.right
                anchors.rightMargin: -Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰅂"
                tooltipText: "Next month"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: root.moveMonth(1)
              }
            }
          }

          // ---- Divider between Calendar grid and Agenda
          Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: gridColumn.width
            height: Style.spacing.hairline
            color: root.contentForeground
            opacity: 0.12
          }

          // ---- Agenda / Selected Date Events View
          Column {
            id: agendaSection
            anchors.horizontalCenter: parent.horizontalCenter
            width: gridColumn.width
            spacing: Style.space(8)

            // Agenda Header: Selected date title + Sync button + Event count
            Item {
              width: parent.width
              height: Math.max(agendaHeaderRow.implicitHeight, Style.space(24))

              Row {
                id: agendaHeaderRow
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(8)

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.selectedDateLabel
                  color: root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                  font.letterSpacing: 1
                }

                Rectangle {
                  visible: root.selectedEvents.length > 0
                  anchors.verticalCenter: parent.verticalCenter
                  width: eventCountText.implicitWidth + Style.space(10)
                  height: Style.space(16)
                  radius: Style.cornerRadius > 0 ? height / 2 : 0
                  color: Style.hoverFillFor(root.contentForeground, Color.accent)

                  Text {
                    textFormat: Text.PlainText
                    id: eventCountText
                    anchors.centerIn: parent
                    text: (root.activeCalendarFilter !== "all" && root.activeCalendarFilter)
                      ? (root.displayedEvents.length + "/" + root.selectedEvents.length)
                      : root.displayedEvents.length
                    color: Style.selectedStateColor(root.contentForeground, Color.accent)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }
                }
              }

              Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(4)

                PanelActionButton {
                  id: addEventBtn
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "󰐕"
                  tooltipText: "Add event to " + root.selectedDateLabel + " (n)"
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: {
                    if (root.addingEvent) root.closeAddEvent()
                    else root.openAddEvent(root.selectedDateKey)
                  }
                }

                PanelActionButton {
                  id: copyAgendaBtn
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: root.agendaCopied ? "󰄬" : "󰆏"
                  tooltipText: root.agendaCopied ? "Copied agenda to clipboard!" : (root.displayedEvents.length > 0 ? "Copy agenda as Markdown (y)" : "No events to copy")
                  foreground: root.agendaCopied ? Color.accent : root.contentForeground
                  fontFamily: root.contentFontFamily
                  opacity: root.displayedEvents.length > 0 ? (root.agendaCopied ? 1.0 : 0.85) : 0.4
                  onClicked: {
                    if (root.displayedEvents.length > 0) root.copyAgendaMarkdown()
                  }
                }

                BorderSurface {
                  id: syncActionBtn
                  anchors.verticalCenter: parent.verticalCenter
                  implicitWidth: Math.max(Style.space(22), Style.font.icon + Style.spacing.sm * 2)
                  implicitHeight: implicitWidth
                  radius: Style.cornerRadius

                  readonly property bool _hot: syncMouse.containsMouse && enabled
                  color: _hot ? Style.hoverFillFor(root.contentForeground, root.contentForeground) : "transparent"
                  Behavior on color { ColorAnimation { duration: 60 } }

                  Text {
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: "󰑐"
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.icon
                    opacity: root.syncRunning ? 0.6 : 1.0

                    RotationAnimator on rotation {
                      running: root.syncRunning
                      from: 0
                      to: 360
                      loops: Animation.Infinite
                      duration: 800
                    }
                  }

                  MouseArea {
                    id: syncMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: root.syncCalendars(true)
                  }

                  PanelToolTip {
                    visible: syncMouse.containsMouse
                    text: root.syncRunning ? "Syncing calendars..." : "Sync calendars (" + (root.eventsData.lastSyncedFormatted || "never") + ")"
                    fontFamily: root.contentFontFamily
                  }
                }

                PanelActionButton {
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "󰒓"
                  tooltipText: "Calendar Settings"
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.openSettings()
                }
              }

            }

            // ---- Add Event Modal / Form Card ----
            Rectangle {
              visible: root.addingEvent
              width: parent.width
              height: visible ? (addEventFormCol.implicitHeight + Style.space(20)) : 0
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: 1
              border.color: Color.accent

              Column {
                id: addEventFormCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(10)
                spacing: Style.space(8)

                // Form Header
                Row {
                  width: parent.width
                  Item {
                    width: parent.width - Style.space(24)
                    height: Style.space(20)
                    Row {
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(6)
                      Text {
                        textFormat: Text.PlainText
                        text: "NEW EVENT"
                        color: Color.accent
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                        font.letterSpacing: 1
                      }
                      Text {
                        textFormat: Text.PlainText
                        text: "· " + root.eventDate
                        color: Qt.darker(root.contentForeground, 1.6)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }
                  }
                  PanelActionButton {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    iconText: "󰅖"
                    tooltipText: "Close form (Esc)"
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: root.closeAddEvent()
                  }
                }

                // Title Input
                TextField {
                  id: eventTitleInput
                  width: parent.width
                  placeholderText: "Event Title (e.g. Team Standup, Doctor Appointment)"
                  text: root.eventTitle
                  foreground: root.contentForeground
                  font.family: root.contentFontFamily
                  onTextChanged: root.eventTitle = text
                }

                // Calendar Selector Row
                Column {
                  width: parent.width
                  spacing: Style.space(4)
                  Text {
                    textFormat: Text.PlainText
                    text: "TARGET CALENDAR:"
                    color: Qt.darker(root.contentForeground, 1.7)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }
                  Row {
                    spacing: Style.space(6)
                    Repeater {
                      model: root.writableCalendars
                      Rectangle {
                        required property var modelData
                        readonly property bool isSelected: root.eventCalendar === modelData.name
                        width: calPillRow.implicitWidth + Style.space(14)
                        height: Style.space(22)
                        radius: Style.cornerRadius > 0 ? height / 2 : 0
                        color: isSelected ? Qt.rgba(modelData.color.r, modelData.color.g, modelData.color.b, 0.25) : "transparent"
                        border.width: 1
                        border.color: isSelected ? modelData.color : Qt.darker(root.contentForeground, 1.8)

                        Row {
                          id: calPillRow
                          anchors.centerIn: parent
                          spacing: Style.space(5)
                          Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            width: Style.space(6)
                            height: Style.space(6)
                            radius: 3
                            color: modelData.color || Color.accent
                          }
                          Text {
                            textFormat: Text.PlainText
                            text: modelData.name
                            color: isSelected ? root.contentForeground : Qt.darker(root.contentForeground, 1.4)
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.caption
                            font.bold: isSelected
                          }
                        }

                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: root.eventCalendar = modelData.name
                        }
                      }
                    }
                  }
                }

                // Time / Duration Row
                Column {
                  width: parent.width
                  spacing: Style.space(4)

                  Row {
                    width: parent.width
                    spacing: Style.space(8)

                    // All Day Toggle
                    Rectangle {
                      width: allDayPillText.implicitWidth + Style.space(14)
                      height: Style.space(24)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: root.eventAllDay ? Color.accent : "transparent"
                      border.width: 1
                      border.color: root.eventAllDay ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                      Text {
                        textFormat: Text.PlainText
                        id: allDayPillText
                        anchors.centerIn: parent
                        text: "All Day"
                        color: root.eventAllDay ? Color.background : root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: root.eventAllDay
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.eventAllDay = !root.eventAllDay
                      }
                    }

                    // Start Time Input
                    TextField {
                      visible: !root.eventAllDay
                      width: Style.space(70)
                      placeholderText: "09:00"
                      text: root.eventStartTime
                      foreground: root.contentForeground
                      font.family: root.contentFontFamily
                      onTextChanged: root.eventStartTime = text
                    }

                    Text {
                      visible: !root.eventAllDay
                      textFormat: Text.PlainText
                      anchors.verticalCenter: parent.verticalCenter
                      text: "–"
                      color: Qt.darker(root.contentForeground, 1.6)
                      font.family: root.contentFontFamily
                    }

                    // End Time Input
                    TextField {
                      visible: !root.eventAllDay
                      width: Style.space(70)
                      placeholderText: "10:00"
                      text: root.eventEndTime
                      foreground: root.contentForeground
                      font.family: root.contentFontFamily
                      onTextChanged: root.eventEndTime = text
                    }

                    // Quick duration presets
                    Row {
                      visible: !root.eventAllDay
                      spacing: Style.space(4)
                      anchors.verticalCenter: parent.verticalCenter

                      Repeater {
                        model: [
                          { label: "30m", minutes: 30 },
                          { label: "1h", minutes: 60 },
                          { label: "2h", minutes: 120 }
                        ]

                        Rectangle {
                          required property var modelData
                          width: durText.implicitWidth + Style.space(10)
                          height: Style.space(20)
                          radius: Style.cornerRadius > 0 ? height / 2 : 0
                          color: "transparent"
                          border.width: 1
                          border.color: Qt.darker(root.contentForeground, 1.8)

                          Text {
                            textFormat: Text.PlainText
                            id: durText
                            anchors.centerIn: parent
                            text: modelData.label
                            color: Qt.darker(root.contentForeground, 1.4)
                            font.family: root.contentFontFamily
                            font.pixelSize: 10
                          }

                          MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.setEventDuration(modelData.minutes)
                          }
                        }
                      }
                    }
                  }
                }

                // Location / URL Input
                TextField {
                  id: eventLocInput
                  width: parent.width
                  placeholderText: "Location or Video Meeting URL (optional)"
                  text: root.eventLocation
                  foreground: root.contentForeground
                  font.family: root.contentFontFamily
                  onTextChanged: root.eventLocation = text
                }

                // Description Input
                TextField {
                  id: eventDescInput
                  width: parent.width
                  placeholderText: "Description / Notes (optional)"
                  text: root.eventDescription
                  foreground: root.contentForeground
                  font.family: root.contentFontFamily
                  onTextChanged: root.eventDescription = text
                }

                // Error Message if any
                Text {
                  visible: root.eventErrorMessage.length > 0
                  textFormat: Text.PlainText
                  text: root.eventErrorMessage
                  color: "#f38ba8"
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }

                // Action Buttons
                Row {
                  anchors.right: parent.right
                  spacing: Style.space(8)

                  Rectangle {
                    width: cancelEventBtnText.implicitWidth + Style.space(16)
                    height: Style.space(26)
                    radius: Style.cornerRadius
                    color: cancelEvtMouse.containsMouse ? Style.hoverFillFor(root.contentForeground, Color.accent) : "transparent"

                    Text {
                      textFormat: Text.PlainText
                      id: cancelEventBtnText
                      anchors.centerIn: parent
                      text: "Cancel"
                      color: Qt.darker(root.contentForeground, 1.5)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }

                    MouseArea {
                      id: cancelEvtMouse
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.closeAddEvent()
                    }
                  }

                  Rectangle {
                    width: submitEventBtnText.implicitWidth + Style.space(16)
                    height: Style.space(26)
                    radius: Style.cornerRadius
                    color: Color.accent
                    opacity: (root.eventTitle.trim() && !root.eventSubmitting) ? 1.0 : 0.4

                    Row {
                      anchors.centerIn: parent
                      spacing: Style.space(4)

                      Text {
                        textFormat: Text.PlainText
                        id: submitEventBtnText
                        text: root.eventSubmitting ? "Adding..." : "Save Event"
                        color: Color.background
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }
                    }

                    MouseArea {
                      anchors.fill: parent
                      enabled: root.eventTitle.trim() && !root.eventSubmitting
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.submitNewEvent()
                    }
                  }
                }
              }
            }

            // Calendar Quick-Filter Chips (shown when multiple calendars are active)
            Item {
              visible: root.activeCalendars.length > 1
              width: parent.width
              height: visible ? filterScroll.height : 0

              Flickable {
                id: filterScroll
                width: parent.width
                height: Style.space(22)
                contentWidth: filterRow.implicitWidth
                contentHeight: height
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Row {
                  id: filterRow
                  spacing: Style.space(5)

                  // "All" chip
                  Rectangle {
                    id: allChip
                    readonly property bool isSelected: root.activeCalendarFilter === "all" || !root.activeCalendarFilter
                    width: allChipText.implicitWidth + Style.space(14)
                    height: Style.space(22)
                    radius: Style.cornerRadius > 0 ? height / 2 : 0
                    color: isSelected
                      ? Style.hoverFillFor(root.contentForeground, Color.accent)
                      : (allChipMouse.containsMouse ? Style.hoverFillFor(root.contentForeground, Color.accent) : "transparent")
                    border.width: 1
                    border.color: isSelected
                      ? Color.accent
                      : (allChipMouse.containsMouse ? Qt.darker(root.contentForeground, 1.4) : Qt.darker(root.contentForeground, 1.8))

                    Text {
                      textFormat: Text.PlainText
                      id: allChipText
                      anchors.centerIn: parent
                      text: "All"
                      color: allChip.isSelected ? Style.selectedStateColor(root.contentForeground, Color.accent) : root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: allChip.isSelected
                    }

                    MouseArea {
                      id: allChipMouse
                      anchors.fill: parent
                      hoverEnabled: true
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.activeCalendarFilter = "all"
                    }
                  }

                  Repeater {
                    model: root.activeCalendars

                    Rectangle {
                      id: calChip
                      required property var modelData
                      readonly property bool isSelected: root.activeCalendarFilter === modelData.name
                      readonly property color calColor: modelData.color ? modelData.color : Color.accent

                      width: calChipContentRow.implicitWidth + Style.space(14)
                      height: Style.space(22)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: isSelected
                        ? Qt.rgba(calColor.r, calColor.g, calColor.b, 0.22)
                        : (calChipMouse.containsMouse ? Style.hoverFillFor(root.contentForeground, Color.accent) : "transparent")
                      border.width: 1
                      border.color: isSelected
                        ? calColor
                        : (calChipMouse.containsMouse ? Qt.darker(root.contentForeground, 1.4) : Qt.darker(root.contentForeground, 1.8))

                      Row {
                        id: calChipContentRow
                        anchors.centerIn: parent
                        spacing: Style.space(5)

                        Rectangle {
                          anchors.verticalCenter: parent.verticalCenter
                          width: Style.space(6)
                          height: Style.space(6)
                          radius: Style.cornerRadius > 0 ? 3 : 0
                          color: calChip.calColor
                        }

                        Text {
                          textFormat: Text.PlainText
                          anchors.verticalCenter: parent.verticalCenter
                          text: calChip.modelData.name
                          color: calChip.isSelected ? root.contentForeground : Qt.darker(root.contentForeground, 1.3)
                          font.family: root.contentFontFamily
                          font.pixelSize: Style.font.caption
                          font.bold: calChip.isSelected
                        }
                      }

                      MouseArea {
                        id: calChipMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                          if (root.activeCalendarFilter === calChip.modelData.name) {
                            root.activeCalendarFilter = "all"
                          } else {
                            root.activeCalendarFilter = calChip.modelData.name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }

            // Events List
            Column {
              width: parent.width
              spacing: Style.space(6)
              visible: root.displayedEvents.length > 0

              Repeater {
                model: root.displayedEvents

                Rectangle {
                  required property var modelData
                  width: agendaSection.width
                  height: eventContentCol.implicitHeight + Style.space(12)
                  radius: Style.cornerRadius
                  color: Style.hoverFillFor(root.contentForeground, Color.accent)

                  // Calendar color accent strip
                  Rectangle {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    anchors.margins: Style.space(4)
                    width: Style.space(3)
                    radius: Style.cornerRadius > 0 ? width / 2 : 0
                    color: modelData.color || Color.accent
                  }

                  // Delete button for writable events
                  PanelActionButton {
                    visible: Boolean(modelData.writable)
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: Style.space(4)
                    iconText: "󰆴"
                    tooltipText: "Delete event from " + (modelData.calendar || "calendar")
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    opacity: 0.65
                    onClicked: root.deleteEvent(modelData)
                  }

                  Column {
                    id: eventContentCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Style.space(14)
                    anchors.rightMargin: modelData.writable ? Style.space(32) : Style.space(10)
                    spacing: Style.space(2)

                    Row {
                      width: parent.width
                      spacing: Style.space(6)

                      Text {
                        textFormat: Text.PlainText
                        text: modelData.allDay ? "ALL DAY" : root.eventTimeRange(modelData, " – ")
                        color: Qt.darker(root.contentForeground, 1.4)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }

                      Text {
                        textFormat: Text.PlainText
                        visible: modelData.calendar !== ""
                        text: "· " + modelData.calendar.toUpperCase()
                        color: Qt.darker(root.contentForeground, 1.8)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.letterSpacing: 0.5
                        elide: Text.ElideRight
                      }
                    }

                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      text: modelData.title
                      color: root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                      elide: Text.ElideRight
                    }

                    Row {
                      visible: modelData.location && modelData.location.length > 0
                      width: parent.width
                      spacing: Style.space(4)

                      Text {
                        textFormat: Text.PlainText
                        text: "󰍎"
                        color: Qt.darker(root.contentForeground, 1.6)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                      }

                      Text {
                        textFormat: Text.PlainText
                        width: parent.width - Style.space(16)
                        text: modelData.location
                        color: modelData.meetingUrl ? Color.accent : Qt.darker(root.contentForeground, 1.6)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.underline: Boolean(locMouse.containsMouse && modelData.meetingUrl)
                        elide: Text.ElideRight

                        MouseArea {
                          id: locMouse
                          anchors.fill: parent
                          hoverEnabled: Boolean(modelData.meetingUrl)
                          cursorShape: modelData.meetingUrl ? Qt.PointingHandCursor : Qt.ArrowCursor
                          onClicked: {
                            if (modelData.meetingUrl) root.openExternalUrl(modelData.meetingUrl)
                          }
                        }
                      }
                    }

                    // One-Click Join Meeting Link
                    Row {
                      visible: root.enableMeetingLinks && Boolean(modelData.meetingUrl && modelData.meetingUrl.length > 0)
                      spacing: Style.space(6)
                      topPadding: Style.space(3)

                      Rectangle {
                        id: joinBtn
                        width: joinRow.implicitWidth + Style.space(16)
                        height: Style.space(22)
                        radius: Style.cornerRadius > 0 ? 4 : 0
                        color: joinMouse.containsMouse ? Color.accent : Style.hoverFillFor(root.contentForeground, Color.accent)
                        border.width: 1
                        border.color: Color.accent

                        Row {
                          id: joinRow
                          anchors.centerIn: parent
                          spacing: Style.space(5)

                          Text {
                            textFormat: Text.PlainText
                            anchors.verticalCenter: parent.verticalCenter
                            text: "󰕧"
                            color: joinMouse.containsMouse ? Color.background : Color.accent
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.caption
                          }

                          Text {
                            textFormat: Text.PlainText
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.meetingProvider ? "Join " + modelData.meetingProvider : "Join Meeting"
                            color: joinMouse.containsMouse ? Color.background : root.contentForeground
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.caption
                            font.bold: true
                          }
                        }

                        MouseArea {
                          id: joinMouse
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: Qt.PointingHandCursor
                          onClicked: root.openExternalUrl(modelData.meetingUrl)
                        }

                        PanelToolTip {
                          text: modelData.meetingUrl
                          fontFamily: root.contentFontFamily
                        }
                      }
                    }
                  }
                }
              }
            }

            // Empty state when no events on selected date
            Rectangle {
              visible: root.displayedEvents.length === 0
              width: parent.width
              height: Style.space(38)
              radius: Style.cornerRadius
              color: "transparent"

              Row {
                anchors.centerIn: parent
                spacing: Style.space(8)

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: "󰃭"
                  color: Qt.darker(root.contentForeground, 2.0)
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.selectedEvents.length > 0
                    ? ("No " + root.activeCalendarFilter + " events for this day")
                    : (root.configuredCalendarCount === 0
                        ? "Add calendar feeds to ~/.config/omarchy/calendars.json"
                        : "No events scheduled for this day")
                  color: Qt.darker(root.contentForeground, 1.8)
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }

        // ==========================================
        // SETTINGS VIEW
        // ==========================================
        Column {
          id: settingsSection
          visible: root.showingSettings
          width: parent.width
          spacing: Style.space(12)

          // Settings Header: Back button + Title + Action
          Item {
            width: parent.width
            height: Style.space(32)

            Row {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(10)

              PanelActionButton {
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰅁"
                tooltipText: "Back to calendar"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: root.closeSettings()
              }

              Text {
                textFormat: Text.PlainText
                anchors.verticalCenter: parent.verticalCenter
                text: "CALENDAR SETTINGS"
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                font.letterSpacing: 1
              }
            }

            Row {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)

              PanelActionButton {
                visible: !root.addingCalendar
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰐕"
                tooltipText: "Add new calendar"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: root.startAddingCalendar("url")
              }
            }
          }

          // Divider
          Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: parent.width
            height: Style.spacing.hairline
            color: root.contentForeground
            opacity: 0.12
          }

          // Settings Tab Selector
          Row {
            width: parent.width
            spacing: Style.space(8)

            Rectangle {
              id: calTabBtn
              width: (parent.width - Style.space(8)) / 2
              height: Style.space(32)
              radius: Style.cornerRadius
              color: root.settingsTab === "calendars" ? Color.accent : Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: 1
              border.color: root.settingsTab === "calendars" ? Color.accent : Qt.darker(root.contentForeground, 1.8)

              Row {
                anchors.centerIn: parent
                spacing: Style.space(6)

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: "󰃭"
                  color: root.settingsTab === "calendars" ? Color.background : root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Calendars (" + root.configuredCalendars.length + ")"
                  color: root.settingsTab === "calendars" ? Color.background : root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: root.settingsTab === "calendars"
                }
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                  root.settingsTab = "calendars"
                  if (calendarScroll) calendarScroll.contentY = 0
                }
              }
            }

            Rectangle {
              id: prefTabBtn
              width: (parent.width - Style.space(8)) / 2
              height: Style.space(32)
              radius: Style.cornerRadius
              color: root.settingsTab === "preferences" ? Color.accent : Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: 1
              border.color: root.settingsTab === "preferences" ? Color.accent : Qt.darker(root.contentForeground, 1.8)

              Row {
                anchors.centerIn: parent
                spacing: Style.space(6)

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: "󰂚"
                  color: root.settingsTab === "preferences" ? Color.background : root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Preferences"
                  color: root.settingsTab === "preferences" ? Color.background : root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: root.settingsTab === "preferences"
                }
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                  root.settingsTab = "preferences"
                  if (calendarScroll) calendarScroll.contentY = 0
                }
              }
            }
          }

          // Calendars Tab Content
          Column {
            id: calendarsTabCol
            visible: root.settingsTab === "calendars"
            width: parent.width
            spacing: Style.space(10)

            // Add Calendar Form (when addingCalendar is active)
            Rectangle {
              visible: root.addingCalendar

            anchors.horizontalCenter: parent.horizontalCenter
            width: parent.width
            height: addFormCol.implicitHeight + Style.space(20)
            radius: Style.cornerRadius
            color: Style.hoverFillFor(root.contentForeground, Color.accent)
            border.width: Style.spacing.hairline
            border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

            Column {
              id: addFormCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(12)
              spacing: Style.space(8)

              Text {
                textFormat: Text.PlainText
                text: "NEW CALENDAR FEED"
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1
              }

              // Type Selector: iCal URL vs Google API ID vs JMAP
              Row {
                spacing: Style.space(8)

                Rectangle {
                  width: typeUrlText.implicitWidth + Style.space(14)
                  height: Style.space(24)
                  radius: Style.cornerRadius
                  color: root.formType === "url" ? Color.accent : "transparent"
                  border.width: root.formType === "url" ? 0 : Style.spacing.hairline
                  border.color: Qt.darker(root.contentForeground, 1.8)

                  Text {
                    textFormat: Text.PlainText
                    id: typeUrlText
                    anchors.centerIn: parent
                    text: "iCal / Webcal URL"
                    color: root.formType === "url" ? Color.background : root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: root.formType === "url"
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      root.formType = "url"
                      root.formColor = "#4285f4"
                    }
                  }
                }

                Rectangle {
                  width: typeGoogleText.implicitWidth + Style.space(14)
                  height: Style.space(24)
                  radius: Style.cornerRadius
                  color: root.formType === "googleId" ? Color.accent : "transparent"
                  border.width: root.formType === "googleId" ? 0 : Style.spacing.hairline
                  border.color: Qt.darker(root.contentForeground, 1.8)

                  Text {
                    textFormat: Text.PlainText
                    id: typeGoogleText
                    anchors.centerIn: parent
                    text: "Google Calendar ID"
                    color: root.formType === "googleId" ? Color.background : root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: root.formType === "googleId"
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      root.formType = "googleId"
                      root.formColor = "#e01b24"
                    }
                  }
                }

                Rectangle {
                  width: typeJmapText.implicitWidth + Style.space(14)
                  height: Style.space(24)
                  radius: Style.cornerRadius
                  color: root.formType === "jmap" ? Color.accent : "transparent"
                  border.width: root.formType === "jmap" ? 0 : Style.spacing.hairline
                  border.color: Qt.darker(root.contentForeground, 1.8)

                  Text {
                    textFormat: Text.PlainText
                    id: typeJmapText
                    anchors.centerIn: parent
                    text: "JMAP"
                    color: root.formType === "jmap" ? Color.background : root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: root.formType === "jmap"
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      root.formType = "jmap"
                      root.formColor = "#ff7700"
                    }
                  }
                }

                Rectangle {
                  width: typeLocalText.implicitWidth + Style.space(14)
                  height: Style.space(24)
                  radius: Style.cornerRadius
                  color: root.formType === "local" ? Color.accent : "transparent"
                  border.width: root.formType === "local" ? 0 : Style.spacing.hairline
                  border.color: Qt.darker(root.contentForeground, 1.8)

                  Text {
                    textFormat: Text.PlainText
                    id: typeLocalText
                    anchors.centerIn: parent
                    text: "Local Offline"
                    color: root.formType === "local" ? Color.background : root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: root.formType === "local"
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      root.formType = "local"
                      root.formColor = "#a6e3a1"
                    }
                  }
                }
              }

              // Name Field
              TextField {
                id: calNameInput
                width: parent.width
                placeholderText: "Calendar Name (e.g. Personal, Proton, Local Work)"
                text: root.formName
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                onTextChanged: root.formName = text
              }

              // JMAP Session URL Field (JMAP only)
              TextField {
                id: calJmapUrlInput
                visible: root.formType === "jmap"
                width: parent.width
                placeholderText: "Session URL (default: https://api.fastmail.com/jmap/session)"
                text: root.formJmapUrl
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                onTextChanged: root.formJmapUrl = text
              }

              // JMAP Token Field (JMAP only)
              TextField {
                id: calJmapTokenInput
                visible: root.formType === "jmap"
                width: parent.width
                placeholderText: "JMAP API / Bearer Token"
                text: root.formJmapToken
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                onTextChanged: root.formJmapToken = text
              }

              // Address / ID Field (not needed for Local)
              TextField {
                id: calAddressInput
                visible: root.formType !== "local"
                width: parent.width
                placeholderText: root.formType === "googleId"
                  ? "Google Calendar ID (e.g. xyz@group.calendar.google.com)"
                  : (root.formType === "jmap"
                     ? "Calendar ID (optional, leave blank for all)"
                     : "iCal URL (Google, Apple, Proton .ics link)")
                text: root.formAddress
                foreground: root.contentForeground
                font.family: root.contentFontFamily
                onTextChanged: root.formAddress = text
              }


              // Color picker row
              Row {
                width: parent.width
                spacing: Style.space(12)

                Row {
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(6)

                  Text {
                    textFormat: Text.PlainText
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Color:"
                    color: Qt.darker(root.contentForeground, 1.5)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Repeater {
                    model: Model.CALENDAR_COLORS
                    Rectangle {
                      required property var modelData
                      width: Style.space(16)
                      height: Style.space(16)
                      radius: width / 2
                      color: modelData
                      border.width: root.formColor === modelData ? 2 : 0
                      border.color: root.contentForeground

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.formColor = modelData
                      }
                    }
                  }
                }

              }

              // Form Action Buttons
              Row {
                anchors.right: parent.right
                spacing: Style.space(8)

                Rectangle {
                  width: cancelBtnText.implicitWidth + Style.space(16)
                  height: Style.space(26)
                  radius: Style.cornerRadius
                  color: cancelMouse.containsMouse ? Style.hoverFillFor(root.contentForeground, Color.accent) : "transparent"

                  Text {
                    textFormat: Text.PlainText
                    id: cancelBtnText
                    anchors.centerIn: parent
                    text: "Cancel"
                    color: Qt.darker(root.contentForeground, 1.5)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  MouseArea {
                    id: cancelMouse
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.addingCalendar = false
                  }
                }

                Rectangle {
                  readonly property bool isFormValid: root.formType === "local"
                    ? Boolean(root.formName.trim())
                    : (root.formType === "jmap"
                       ? Boolean(root.formName.trim() && root.formJmapToken.trim())
                       : Boolean(root.formName.trim() && root.formAddress.trim()))
                  width: addBtnText.implicitWidth + Style.space(16)
                  height: Style.space(26)
                  radius: Style.cornerRadius
                  color: Color.accent
                  opacity: isFormValid ? 1.0 : 0.4

                  Text {
                    textFormat: Text.PlainText
                    id: addBtnText
                    anchors.centerIn: parent
                    text: "Add Calendar"
                    color: Color.background
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }

                  MouseArea {
                    anchors.fill: parent
                    enabled: parent.isFormValid
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.commitNewCalendar()
                  }
                }
              }
            }
          }

          // List of Configured Calendars
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.configuredCalendars.length > 0

            Text {
              textFormat: Text.PlainText
              text: "ACTIVE CALENDARS (" + root.configuredCalendars.length + ")"
              color: Qt.darker(root.contentForeground, 1.8)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1
            }

            Repeater {
              model: root.configuredCalendars

              Rectangle {
                id: calItemRow
                required property var modelData
                required property int index

                width: settingsSection.width
                height: Style.space(46)
                radius: Style.cornerRadius
                color: Style.hoverFillFor(root.contentForeground, Color.accent)

                Row {
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.margins: Style.space(10)
                  spacing: Style.space(8)

                  // Enable/Disable toggle
                  Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(16)
                    height: Style.space(16)
                    radius: Style.cornerRadius > 0 ? 3 : 0
                    color: (modelData.enabled !== false) ? Color.accent : "transparent"
                    border.width: 1
                    border.color: (modelData.enabled !== false) ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                    Text {
                      textFormat: Text.PlainText
                      anchors.centerIn: parent
                      text: "✓"
                      visible: modelData.enabled !== false
                      color: Color.background
                      font.pixelSize: 10
                      font.bold: true
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.toggleCalendarEnabled(calItemRow.index)
                    }

                    PanelToolTip {
                      text: modelData.enabled !== false ? "Disable calendar" : "Enable calendar"
                      fontFamily: root.contentFontFamily
                    }
                  }

                  // Color dot (click to cycle)
                  Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(12)
                    height: Style.space(12)
                    radius: width / 2
                    color: modelData.color || Color.accent

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.cycleColorForCalendar(calItemRow.index)
                    }

                    PanelToolTip {
                      text: "Click to change color"
                      fontFamily: root.contentFontFamily
                    }
                  }

                  // Name & Details
                  Column {
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - Style.space(140)
                    spacing: Style.space(2)

                    Row {
                      spacing: Style.space(6)
                      Text {
                        textFormat: Text.PlainText
                        text: modelData.name || "Untitled"
                        color: modelData.enabled !== false ? root.contentForeground : Qt.darker(root.contentForeground, 2.0)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.bold: true
                      }

                      Text {
                        textFormat: Text.PlainText
                        text: (modelData.type === "jmap" || modelData.jmapToken) ? "JMAP" : (modelData.googleCalendarId ? "GOOGLE API" : "ICAL FEED")
                        color: Qt.darker(root.contentForeground, 1.9)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: (modelData.type === "jmap" || modelData.jmapToken) ? (modelData.jmapUrl || "JMAP Feed") : (modelData.googleCalendarId || modelData.url || "No address")
                      color: Qt.darker(root.contentForeground, 1.9)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideMiddle
                      width: parent.width
                    }
                  }

                  // Delete button
                  PanelActionButton {
                    anchors.verticalCenter: parent.verticalCenter
                    iconText: "󰆴"
                    tooltipText: "Delete calendar"
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: root.removeCalendar(calItemRow.index)
                  }
                }
              }
            }

            // Empty state if no calendars
            Rectangle {
              visible: root.configuredCalendars.length === 0 && !root.addingCalendar
              width: parent.width
              height: Style.space(60)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)

              Column {
                anchors.centerIn: parent
                spacing: Style.space(6)

                Text {
                  textFormat: Text.PlainText
                  anchors.horizontalCenter: parent.horizontalCenter
                  text: "No Calendars Configured"
                  color: root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }

                Rectangle {
                  anchors.horizontalCenter: parent.horizontalCenter
                  width: addFirstBtnText.implicitWidth + Style.space(16)
                  height: Style.space(24)
                  radius: Style.cornerRadius
                  color: Color.accent

                  Text {
                    textFormat: Text.PlainText
                    id: addFirstBtnText
                    anchors.centerIn: parent
                    text: "+ Add Your First Calendar"
                    color: Color.background
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.startAddingCalendar("url")
                  }
                }
              }
            }
          }
          }

          // Preferences & Sync Tab Content
          Column {
            id: preferencesTabCol
            visible: root.settingsTab === "preferences"
            width: parent.width
            spacing: Style.space(10)

            // 1. Clock Format Card
            Rectangle {
              width: parent.width
              height: Style.space(60)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: Style.spacing.hairline
              border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

              Item {
                anchors.fill: parent
                anchors.margins: Style.space(12)

                Column {
                  anchors.left: parent.left
                  anchors.right: clockFormatPills.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)

                  Text {
                    textFormat: Text.PlainText
                    text: "Clock Format"
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: "Choose a 12-hour or 24-hour clock"
                    color: Qt.darker(root.contentForeground, 1.8)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Row {
                  id: clockFormatPills
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(4)

                  Repeater {
                    model: [
                      { label: "12-hour", value: 12 },
                      { label: "24-hour", value: 24 }
                    ]

                    Rectangle {
                      id: clockFormatPill
                      required property var modelData
                      readonly property bool isSelected: root.clockHourCycle === modelData.value
                      width: clockFormatText.implicitWidth + Style.space(14)
                      height: Style.space(22)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: isSelected ? Color.accent : "transparent"
                      border.width: 1
                      border.color: isSelected ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                      Text {
                        textFormat: Text.PlainText
                        id: clockFormatText
                        anchors.centerIn: parent
                        text: clockFormatPill.modelData.label
                        color: clockFormatPill.isSelected ? Color.background : root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: clockFormatPill.isSelected
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.setClockHourCycle(clockFormatPill.modelData.value)
                      }
                    }
                  }
                }
              }
            }

            // 2. Auto-Sync Interval Card
            Rectangle {
              width: parent.width
              height: Style.space(76)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: Style.spacing.hairline
              border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

              Column {
                anchors.fill: parent
                anchors.margins: Style.space(12)
                spacing: Style.space(8)

                Row {
                  spacing: Style.space(6)

                  Text {
                    textFormat: Text.PlainText
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Auto-Sync Interval"
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }

                  Text {
                    textFormat: Text.PlainText
                    anchors.verticalCenter: parent.verticalCenter
                    text: "· Background updates"
                    color: Qt.darker(root.contentForeground, 1.8)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Row {
                  spacing: Style.space(6)

                  Repeater {
                    model: [
                      { label: "5m", value: 5 },
                      { label: "15m", value: 15 },
                      { label: "30m", value: 30 },
                      { label: "60m", value: 60 },
                      { label: "Manual", value: 0 }
                    ]

                    Rectangle {
                      id: syncOptPill
                      required property var modelData
                      width: syncOptText.implicitWidth + Style.space(16)
                      height: Style.space(24)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: root.syncIntervalMinutes === modelData.value ? Color.accent : "transparent"
                      border.width: 1
                      border.color: root.syncIntervalMinutes === modelData.value ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                      Text {
                        textFormat: Text.PlainText
                        id: syncOptText
                        anchors.centerIn: parent
                        text: syncOptPill.modelData.label
                        color: root.syncIntervalMinutes === syncOptPill.modelData.value ? Color.background : root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: root.syncIntervalMinutes === syncOptPill.modelData.value
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.persistSettings({ syncIntervalMinutes: syncOptPill.modelData.value })
                      }
                    }
                  }
                }
              }
            }

            // 3. Desktop Notifications Card
            Rectangle {
              width: parent.width
              height: root.notifyUpcomingEvents ? Style.space(88) : Style.space(60)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: Style.spacing.hairline
              border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

              Column {
                anchors.fill: parent
                anchors.margins: Style.space(12)
                spacing: Style.space(8)

                Item {
                  width: parent.width
                  height: Style.space(34)

                  Column {
                    anchors.left: parent.left
                    anchors.right: notifToggleSwitch.left
                    anchors.rightMargin: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.space(2)

                    Text {
                      textFormat: Text.PlainText
                      text: "Desktop Notifications"
                      color: root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: true
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: "Alert before upcoming meetings & appointments"
                      color: Qt.darker(root.contentForeground, 1.8)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  ToggleSwitch {
                    id: notifToggleSwitch
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    checked: root.notifyUpcomingEvents
                    foreground: root.contentForeground
                    accent: Color.accent
                    onToggled: root.persistSettings({ notifyUpcomingEvents: !root.notifyUpcomingEvents })
                  }
                }

                // Timing selector (if notifications enabled)
                Row {
                  visible: root.notifyUpcomingEvents
                  spacing: Style.space(6)

                  Text {
                    textFormat: Text.PlainText
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Notice:"
                    color: Qt.darker(root.contentForeground, 1.6)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Repeater {
                    model: [
                      { label: "10, 5, 1m (Staged)", value: "staged" },
                      { label: "5 min", value: 5 },
                      { label: "10 min", value: 10 },
                      { label: "15 min", value: 15 },
                      { label: "30 min", value: 30 }
                    ]

                    Rectangle {
                      id: timingPill
                      required property var modelData
                      readonly property bool isSelected: String(root.notifyMinutesBefore) === String(modelData.value)
                      width: timingText.implicitWidth + Style.space(14)
                      height: Style.space(22)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: isSelected ? Color.accent : "transparent"
                      border.width: 1
                      border.color: isSelected ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                      Text {
                        textFormat: Text.PlainText
                        id: timingText
                        anchors.centerIn: parent
                        text: timingPill.modelData.label
                        color: timingPill.isSelected ? Color.background : root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: timingPill.isSelected
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.persistSettings({ notifyMinutesBefore: timingPill.modelData.value })
                      }
                    }
                  }
                }

              }
            }

            // 4. 1-Click Meeting Join Integration Card
            Rectangle {
              width: parent.width
              height: Style.space(60)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: Style.spacing.hairline
              border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

              Item {
                anchors.fill: parent
                anchors.margins: Style.space(12)

                Column {
                  anchors.left: parent.left
                  anchors.right: meetToggleSwitch.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)

                  Text {
                    textFormat: Text.PlainText
                    text: "1-Click Meeting Join"
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: "Show direct join buttons for Zoom, Google Meet, Teams"
                    color: Qt.darker(root.contentForeground, 1.8)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                ToggleSwitch {
                  id: meetToggleSwitch
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  checked: root.enableMeetingLinks
                  foreground: root.contentForeground
                  accent: Color.accent
                  onToggled: root.persistSettings({ enableMeetingLinks: !root.enableMeetingLinks })
                }
              }
            }

            // 5. Calendar Week Start Card
            Rectangle {
              width: parent.width
              height: Style.space(60)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: Style.spacing.hairline
              border.color: Style.normalBorderFor(root.contentForeground, Color.accent)

              Item {
                anchors.fill: parent
                anchors.margins: Style.space(12)

                Column {
                  anchors.left: parent.left
                  anchors.right: weekPillsRow.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)

                  Text {
                    textFormat: Text.PlainText
                    text: "First Day of Week"
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: "Start calendar grid on Monday or Sunday"
                    color: Qt.darker(root.contentForeground, 1.8)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Row {
                  id: weekPillsRow
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(4)

                  Rectangle {
                    width: monText.implicitWidth + Style.space(14)
                    height: Style.space(22)
                    radius: Style.cornerRadius > 0 ? height / 2 : 0
                    color: root.weekStart === 1 ? Color.accent : "transparent"
                    border.width: 1
                    border.color: root.weekStart === 1 ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                    Text {
                      textFormat: Text.PlainText
                      id: monText
                      anchors.centerIn: parent
                      text: "Monday"
                      color: root.weekStart === 1 ? Color.background : root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: root.weekStart === 1
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.setWeekStart(1)
                    }
                  }

                  Rectangle {
                    width: sunText.implicitWidth + Style.space(14)
                    height: Style.space(22)
                    radius: Style.cornerRadius > 0 ? height / 2 : 0
                    color: root.weekStart === 0 ? Color.accent : "transparent"
                    border.width: 1
                    border.color: root.weekStart === 0 ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                    Text {
                      textFormat: Text.PlainText
                      id: sunText
                      anchors.centerIn: parent
                      text: "Sunday"
                      color: root.weekStart === 0 ? Color.background : root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: root.weekStart === 0
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.setWeekStart(0)
                    }
                  }
                }
              }
            }

            // 6. Next Event On Bar Card
            Rectangle {
              width: parent.width
              height: root.showNextEvent
                ? Style.space(62) + Style.space(30) + (root.activeCalendars.length > 0
                  ? Style.space(26) + Style.space(22) * root.activeCalendars.length
                  : 0)
                : Style.space(60)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.contentForeground, Color.accent)
              border.width: Style.spacing.hairline
              border.color: Style.normalBorderFor(root.contentForeground, Color.accent)
              clip: true

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(12)
                spacing: Style.space(8)

                Item {
                  width: parent.width
                  height: Style.space(36)

                  Column {
                    anchors.left: parent.left
                    anchors.right: nextEventToggleSwitch.left
                    anchors.rightMargin: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.space(2)

                    Text {
                      textFormat: Text.PlainText
                      text: "Next Event On Bar"
                      color: root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: true
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: "Show the next or ongoing event next to the clock"
                      color: Qt.darker(root.contentForeground, 1.8)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  ToggleSwitch {
                    id: nextEventToggleSwitch
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    checked: root.showNextEvent
                    foreground: root.contentForeground
                    accent: Color.accent
                    onToggled: root.persistSettings({ showNextEvent: !root.showNextEvent })
                  }
                }

                // Look-ahead horizon, same pill shape as the sync interval.
                Row {
                  visible: root.showNextEvent
                  spacing: Style.space(6)

                  Text {
                    textFormat: Text.PlainText
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Look ahead:"
                    color: Qt.darker(root.contentForeground, 1.8)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Repeater {
                    model: [
                      { label: "Today", value: "today" },
                      { label: "Today+Tomorrow", value: "tomorrow" },
                      { label: "Week", value: "week" },
                      { label: "Month", value: "month" },
                      { label: "Forever", value: "forever" }
                    ]

                    Rectangle {
                      id: horizonPill
                      required property var modelData
                      readonly property bool isSelected: root.nextEventHorizon === modelData.value
                      width: horizonPillText.implicitWidth + Style.space(16)
                      height: Style.space(24)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: isSelected ? Color.accent : "transparent"
                      border.width: 1
                      border.color: isSelected ? Color.accent : Qt.darker(root.contentForeground, 1.8)

                      Text {
                        textFormat: Text.PlainText
                        id: horizonPillText
                        anchors.centerIn: parent
                        text: horizonPill.modelData.label
                        color: isSelected ? Color.background : root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: isSelected
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.persistSettings({ nextEventHorizon: horizonPill.modelData.value })
                      }
                    }
                  }
                }

                // Per-calendar tickboxes, the bar's strict allowlist.
                Column {
                  visible: root.showNextEvent && root.activeCalendars.length > 0
                  width: parent.width
                  spacing: Style.space(4)

                  Repeater {
                    model: root.activeCalendars

                    Item {
                      id: barCalRow
                      required property var modelData
                      width: parent.width
                      height: Style.space(20)
                      readonly property bool isChecked: root.barCalendarShown(modelData.name)

                      Row {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Style.space(8)

                        Rectangle {
                          anchors.verticalCenter: parent.verticalCenter
                          width: Style.space(8)
                          height: Style.space(8)
                          radius: Style.space(4)
                          color: String(barCalRow.modelData.color || "#888888")
                        }

                        Text {
                          textFormat: Text.PlainText
                          anchors.verticalCenter: parent.verticalCenter
                          text: barCalRow.modelData.name
                          color: root.contentForeground
                          font.family: root.contentFontFamily
                          font.pixelSize: Style.font.caption
                        }
                      }

                      Rectangle {
                        id: barCalBox
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        width: Style.space(16)
                        height: Style.space(16)
                        radius: 4
                        color: barCalRow.isChecked ? Color.accent : "transparent"
                        border.width: 1
                        border.color: barCalRow.isChecked ? Color.accent : Qt.darker(root.contentForeground, 1.6)
                        Behavior on color { ColorAnimation { duration: 120 } }

                        Text {
                          textFormat: Text.PlainText
                          anchors.centerIn: parent
                          visible: barCalRow.isChecked
                          text: "\u2713"
                          color: Color.background
                          font.family: root.contentFontFamily
                          font.pixelSize: Style.font.caption
                          font.bold: true
                        }
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.toggleBarCalendar(barCalRow.modelData.name)
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
}
