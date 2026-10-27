# Chronica - calendar & clock for Omarchy

**Every calendar you own, in your Omarchy bar.** Google, iCloud, Proton, Outlook, Fastmail (JMAP), Nextcloud, Stalwart, or any `.ics` / `webcal` feed. Two-way on Google, JMAP and CalDAV (iCloud, Nextcloud): create, edit and delete events without leaving the desktop. Reminders, one-click meeting join, and an offline calendar when you have no account at all.

```bash
omarchy plugin add https://github.com/promaaa/sync-calendar-omarchy.git --enable --yes
```

[![Omarchy Plugins](https://img.shields.io/badge/Omarchy%20Plugins-listed-2ea44f?style=flat-square)](https://plugins.omarchy.org/plugin.html?id=promaa.clock)
![GitHub stars](https://img.shields.io/github/stars/promaaa/sync-calendar-omarchy?style=flat-square)
![License](https://img.shields.io/github/license/promaaa/sync-calendar-omarchy?style=flat-square)

Follows your Omarchy theme (the shots below are three different ones). Works with the stock Omarchy Quattro bar and with Shibumi Shell. Python 3 standard library only, no pip packages, no system service, no sudo.

![The calendar panel open on an Omarchy desktop](full-preview.webp)

## Gallery

| Preview | View |
| --- | --- |
| ![Creating an event and picking which calendar it lands in](add-event.webp) | Add an event, straight into iCloud, Google, JMAP or the local calendar |
| ![Plugin settings](settings.webp) | Preferences: clock format, sync interval, reminders, meeting join, first day of week |

## Features

- **Two-Way Event Sync & Creation**: Add (`󰐕` or `n` hotkey), edit (`󰏫`) and delete (`󰆴`) events directly from your desktop into writable calendars (**Google Calendar API**, **JMAP / Fastmail / Stalwart**, **CalDAV / Apple iCloud / Nextcloud**, and **Local Offline Calendars**). Times accept `14:30`, `1430` or `2:30pm`. Editing covers single-day events; on Google it also covers one occurrence of a recurring event, while CalDAV and JMAP series and multi-day events stay delete-only for now. CalDAV edits keep alarms and attendees, and refuse to overwrite a change made meanwhile on another device.
- **Universal iCalendar & JMAP Support**: Compatible with any calendar service providing an `.ics` / `webcal://` link (Google, Apple iCloud, Proton, Outlook / Office 365, Nextcloud, generic iCal) or modern **JMAP** API (Fastmail, Stalwart, Cyrus IMAP, Apache James).
- **Offline Local Calendar**: Create and manage local events stored in `~/.local/state/omarchy/local-events.json` without needing any external cloud account.
- **One-Click "Join Meeting"**: Automatically detects Google Meet, Zoom, Microsoft Teams, Webex, and Jitsi links in event details and displays an instant join button.
- **Staged & Configurable Notifications**: Native alerts prior to upcoming meetings with smart staged reminders (10m, 5m, 1m before) or single intervals (5m, 10m, 15m, 30m). A reminder says only when an event starts, never its title, place or calendar: the shell's notification service exposes notification text to other local accounts, so the details stay in the panel.
- **Copy Agenda as Markdown**: 1-click clipboard export (`󰆏` button or `y` hotkey) to format your daily schedule into clean Markdown tasks for standups, Slack, or Obsidian.
- **Quick-Toggle Calendar Filter Chips**: Fast single-click filter pills in the agenda header to isolate or show specific calendars on the fly.
- **Configurable Auto-Sync & Instant Refresh**: Customizable background sync intervals (5m, 15m, 30m, 60m, or manual) plus an instant sync button with real-time status.
- **Seamless Theming**: Dynamically inherits the active Omarchy or Shibumi bar colors, fonts, and styling.
- **Multi-Calendar Sync**: Connect multiple calendar accounts and feeds with customizable per-calendar colors and easy enable/disable toggles.
- **Interactive Month Grid**: Click any date to view scheduled events for that day.
- **Next 7 Days**: The `7 days` pill (or `u`) lists the coming week, with a title per day. Click an event to read its description.
- **Quick Add**: Type a sentence in the title field, such as `Lunch tomorrow 1pm-2pm`, `Standup friday 9:30-9:45` or `Dentiste demain 14h`, then press Enter. The plugin fills the date and times and keeps `Lunch` as the title. You still review and save the event.
- **12/24-Hour Time Preference**: Switch the clock, agenda and copied agenda times between 12-hour and 24-hour display.
- **Visual Event Indicators**: Days with events show subtle colored dots corresponding to the calendar source.
- **Fast & Non-Blocking**: One background helper process (`fetch-events.py --serve`, about 35 MB) runs every sync and edit in turn, so edits never race. The shell starts it and stops it; it restarts by itself after a crash. Each sync downloads only what changed.
- **Recurring & Multi-Day Events**: Full support for daily, weekly, monthly, and yearly recurring events (`RRULE` / `EXDATE`) and multi-day spans.

### Keyboard Shortcuts & Vim Navigation

| Key | Action |
| --- | --- |
| `h` / `l` or `←` / `→` | **Day Navigation**: Move selection 1 day backward / forward |
| `k` / `j` or `↑` / `↓` | **Week Navigation**: Move selection 1 week backward / forward |
| `0` / `^` | **Week Start**: Jump to first day of current week |
| `$` | **Week End**: Jump to last day of current week |
| `H` / `L` | **Month Bounds**: Jump to 1st day / last day of current month |
| `[` / `]` or `PgUp` / `PgDn` | **Month**: Previous / Next month |
| `{` / `}` | **Year**: Previous / Next year |
| `t` / `T` | **Today**: Jump to current date |
| `n` / `N` or `Enter` | **New Event**: Open event creation modal for the selected date |
| `y` / `Y` | **Copy Agenda**: Export day's events to clipboard as Markdown tasks |
| `u` / `U` | **Agenda View**: Switch between the selected day and the next 7 days |
| `r` / `R` | **Refresh & Sync**: Instant background calendar synchronization |
| `w` / `W` | **Week Start**: Toggle Monday / Sunday week start |
| `?` | **Shortcuts Help**: Toggle keyboard shortcuts cheatsheet modal |
| `Esc` | **Close**: Dismiss Add Event modal, Settings drawer, or Calendar popup |

## Installation

### Stock Omarchy bar

Install with the Omarchy CLI:

```bash
omarchy plugin add https://github.com/promaaa/sync-calendar-omarchy.git --enable --yes
```

This plugin replaces the built-in `omarchy.clock`. After installing, disable
the built-in clock, re-center the bar on the new widget, and pin it as `centerAnchor` in `~/.config/omarchy/shell.json` so the clock does not shift when hovered or resized:

```bash
omarchy plugin disable omarchy.clock
omarchy bar move promaa.clock --section center
sed -i 's/"centerAnchor": "[^"]*"/"centerAnchor": "promaa.clock"/' ~/.config/omarchy/shell.json
```

#### Via GUI

1. Open the Omarchy menu (**Super + Alt + Space**).
2. Go to **Install > Plugins**.
3. Paste the repository URL: `https://github.com/promaaa/sync-calendar-omarchy.git`
4. Hit Enter.

### Shibumi Shell

This plugin implements Shibumi's standard Quattro widget and `KeyboardPanel`
contracts. Its manifest also declares the `clock` semantic capability, so
Shibumi treats it as a clock provider: activating it places it in the center
region and replaces the built-in **Shibumi Center** group instead of showing a
duplicate clock.

1. Open the Shibumi Control Center from the Shibumi wordmark.
2. Open **Plugins** and select **Add plugin**.
3. Paste `https://github.com/promaaa/sync-calendar-omarchy.git`.
4. Acknowledge the third-party plugin warning, install it, and activate
   **Chronica** in the plugin catalog.

The calendar configuration and cached event paths remain under Omarchy's XDG
directories because Shibumi runs inside the existing Omarchy Shell process.

### Hyprland Key Bindings

The panel answers IPC calls, so a key binding can open the event form or read the next event:

```bash
omarchy-shell promaa.clock addEvent                       # open the event form for today
omarchy-shell promaa.clock quickAdd "Gym tomorrow 7am"    # open it filled from a sentence
omarchy-shell promaa.clock nextEvent                      # print the next event, e.g. "14:30–15:00 Standup"
```

For example, in `~/.config/hypr/bindings.conf`:

```
bind = SUPER ALT, C, exec, omarchy-shell promaa.clock addEvent
```

## Configuration

The easiest way to add calendars is the guided setup (see [Connecting a Calendar](#connecting-a-calendar)). Everything ends up in `~/.config/omarchy/calendars.json`, which you can also edit by hand:

```json
[
  {
    "name": "Fastmail JMAP",
    "type": "jmap",
    "jmapUrl": "https://api.fastmail.com/jmap/session",
    "jmapToken": "fmu1-xxxxxxxxxxxxxxxx",
    "color": "#ff7700",
    "enabled": true
  },
  {
    "name": "Google Calendar",
    "url": "https://calendar.google.com/calendar/ical/your_email%40gmail.com/private-xxxxxxxxxxxxxxxx/basic.ics",
    "color": "#4285f4",
    "enabled": true
  },
  {
    "name": "Proton Calendar",
    "url": "https://calendar.proton.me/api/calendar/v1/url/xxxxxxxx/calendar.ics",
    "color": "#6d4aff",
    "enabled": true
  },
  {
    "name": "Apple iCloud",
    "caldavUrl": "https://pXX-caldav.icloud.com/1234567890/calendars/home/",
    "username": "you@icloud.com",
    "password": "xxxx-xxxx-xxxx-xxxx",
    "color": "#30d158",
    "enabled": true
  },
  {
    "name": "Outlook / Office 365",
    "url": "https://outlook.live.com/owa/calendar/xxxxxxxx/calendar.ics",
    "color": "#0078d4",
    "enabled": true
  },
  {
    "name": "Nextcloud / CalDAV",
    "url": "https://nextcloud.example.com/remote.php/dav/public-calendars/xxxxxxxx?export",
    "color": "#0082c9",
    "enabled": true
  },
  {
    "name": "Radicale / Private CalDAV",
    "url": "https://example.com/username/calendar-id",
    "username": "your_username",
    "password": "your_password",
    "color": "#ff5555",
    "enabled": true
  }
]
```

Edits to `calendars.json` hot-reload automatically without restarting the shell. A CalDAV entry (`caldavUrl` + `username` + `password`) needs no public `url`: events are read and written with the same login.

### Passwords and tokens

When a desktop keyring runs (GNOME Keyring or KWallet, through `secret-tool`
from libsecret), the plugin moves every `password` and `jmapToken` into it,
and also the Google `refresh_token` and `client_secret`. The JSON files then
hold `"@keyring"` in their place. Without a keyring, the secrets stay in the
files, which the plugin writes with mode 0600. `--purge-data` also clears
the keyring items.

A password is only sent over `https://`, and never to the host of a redirect.

### Translation option

An entry with `"translateKorean": true` sends the title and location of its
Korean events to Google Translate (`translate.googleapis.com`) and shows the
English result. It is off by default. Leave it off for a private calendar.

### Opening events in their own app

A read-only feed often carries little more than a title and a time. An entry
with `openUrl` puts an open button (`󰏌`) on each of its events, which opens
the event where it lives. `{id}` in the link becomes the event's UID and
`{date}` the agenda day the event is listed under (`YYYY-MM-DD`; for an event
spanning several days, each of them), both URL-encoded. The button only
appears when the result is a plain `http(s)://host/...` link; an event whose
feed had no UID gets one made up by the plugin, which its app will not know.
Outlook / Office 365 opens an event by its UID, a recurring one as its whole
series:

```json
{
  "name": "Work",
  "url": "https://outlook.office365.com/owa/calendar/.../calendar.ics",
  "openUrl": "https://outlook.office.com/calendar/event/{id}"
}
```

The link goes to the default browser. To open it somewhere else, add
`openWith`: the command as a list of arguments, run directly and never
through a shell, with `{url}` standing for the link. In an Omarchy web app
window:

```json
"openWith": ["omarchy-launch-webapp", "{url}"]
```

Or in a web app installed from Chromium, in the browser profile that is
signed in to that account (useful with several Microsoft 365 tenants, which
the default browser cannot tell apart):

```json
"openWith": ["chromium", "--profile-directory=Profile 1", "--app-id=<app id>",
             "--app-launch-url-for-shortcuts-menu-item={url}"]
```

Chromium names that web app's launcher
`~/.local/share/applications/chrome-<app id>-<profile>.desktop`.

`openWith` runs that program as you when you click the button, so only put
commands you trust there. The link, with the event's UID in it, is one of
its arguments: if the program keeps running (a browser that was not already
open), other local users can read it in `/proc`, as with any link a browser
is started with.

## Connecting a Calendar

Open the panel's **Settings (`󰒓`)** and click **`󰐕`**. A terminal opens with a short guided setup:
pick your provider, it opens the matching page of the
**[setup guide](https://promaaa.github.io/sync-calendar-omarchy/)**, you paste the one thing it
asks for (a Google sign-in, an iCloud app password, a Fastmail token, a link), it checks that it
works, lets you choose calendars and saves them. You can also run it directly:

```bash
python3 ~/.config/omarchy/plugins/promaa.clock/setup.py          # menu
python3 ~/.config/omarchy/plugins/promaa.clock/setup.py google   # or icloud, jmap, caldav, ics, local
```

| Provider | Access | What you need |
| --- | --- | --- |
| [Google](https://promaaa.github.io/sync-calendar-omarchy/#google) | read & write | your own Google Cloud app, about 5 minutes once |
| [Apple iCloud](https://promaaa.github.io/sync-calendar-omarchy/#icloud) | read & write | an app-specific password |
| [Fastmail / JMAP](https://promaaa.github.io/sync-calendar-omarchy/#jmap) | read & write | an API token |
| [Nextcloud / CalDAV](https://promaaa.github.io/sync-calendar-omarchy/#caldav) | read & write | an app password |
| [Any .ics link](https://promaaa.github.io/sync-calendar-omarchy/#ics) | read only | the link (Proton, Outlook, Google secret address) |

Passwords and tokens it saves go to the keyring when one runs (see above), else to files with mode 0600.
If Google ever shows `auth_expired`, or adding an event fails with `insufficientPermissions`
(logins from before v1.4.4 were read-only), click **`󰌆`** next to the calendar to sign in again.

## Uninstallation & Clean Removal

### 1. Remove the Plugin
To remove the plugin from Omarchy:
```bash
omarchy plugin remove promaa.clock --yes
```
To re-enable the default clock widget and restore bar placement:
```bash
omarchy plugin enable omarchy.clock
omarchy bar move omarchy.clock --section center
sed -i 's/"centerAnchor": "promaa.clock"/"centerAnchor": "omarchy.clock"/' ~/.config/omarchy/shell.json
```

### 2. Purge Token-Bearing Configuration and OAuth State
Standard plugin removal unlinks the plugin files, but intentionally retains your configuration and cached state. To completely remove all calendar configurations (including JMAP bearer tokens), OAuth credentials, and cached event data:

**Option A (Automated Purge):**
```bash
python3 ~/.config/omarchy/plugins/promaa.clock/fetch-events.py --purge-data
```

**Option B (Manual Removal):**
```bash
# Token-bearing calendar configuration (contains JMAP bearer tokens and private URLs)
rm -f ~/.config/omarchy/calendars.json

# Google OAuth2 credentials and refresh tokens
rm -f ~/.local/state/omarchy/google-auth.json

# Cached calendar event state, sync cache and translation cache
rm -f ~/.local/state/omarchy/calendar-events.json
rm -rf ~/.local/state/omarchy/sync-cache
rm -f ~/.local/state/omarchy/translation-cache.json

# Events of the offline local calendar
rm -f ~/.local/state/omarchy/local-events.json
rmdir ~/.local/state/omarchy 2>/dev/null || true
```


## Contributing

Contributions, bug reports, and suggestions are welcome. Feel free to open an issue or submit a pull request!

Run the tests before a pull request (Python 3.9 or newer, Node 22 or newer):

```bash
python3 -m unittest discover -s tests
node --test tests/test_model.js
```

With `python-dateutil` installed, `tests/test_rrule.py` also compares the
recurrence engine with it. The plugin itself needs only the standard library.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
