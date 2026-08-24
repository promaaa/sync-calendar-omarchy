# Calendar Sync for Omarchy

A fast, lightweight calendar and clock status bar plugin for Omarchy that syncs Google Calendar, Apple iCloud, Proton Calendar, Microsoft Outlook, Fastmail (JMAP / iCal), Nextcloud, Stalwart, and generic iCalendar (.ics / webcal) feeds directly into your desktop.

![GitHub stars](https://img.shields.io/github/stars/promaaa/sync-calendar-omarchy?style=flat-square)
![License](https://img.shields.io/github/license/promaaa/sync-calendar-omarchy?style=flat-square)

![Global desktop view](global-view.jpg)

## Gallery

| Preview | View |
| --- | --- |
| ![Calendar panel with several events in one day](close-view1.png) | Zoomed calendar view |
| ![Preferences menu](close-view2.png) | Preferences menu |

## Features

- **Universal iCalendar & JMAP Support**: Compatible with any calendar service providing an `.ics` / `webcal://` link (Google, Apple iCloud, Proton, Outlook / Office 365, Nextcloud, generic iCal) or modern **JMAP** API (Fastmail, Stalwart, Cyrus IMAP, Apache James).
- **One-Click "Join Meeting"**: Automatically detects Google Meet, Zoom, Microsoft Teams, Webex, and Jitsi links in event details and displays an instant join button.
- **Staged & Configurable Notifications**: Native alerts prior to upcoming meetings with smart staged reminders (10m, 5m, 1m before) or single intervals (5m, 10m, 15m, 30m).
- **Copy Agenda as Markdown**: 1-click clipboard export (`󰆏` button or `y` hotkey) to format your daily schedule into clean Markdown tasks for standups, Slack, or Obsidian.
- **Quick-Toggle Calendar Filter Chips**: Fast single-click filter pills in the agenda header to isolate or show specific calendars on the fly.
- **Configurable Auto-Sync & Instant Refresh**: Customizable background sync intervals (5m, 15m, 30m, 60m, or manual) plus an instant sync button with real-time status.
- **Seamless Theming**: Dynamically inherits your active Omarchy theme colors, fonts, and styling.
- **Multi-Calendar Sync**: Connect multiple calendar accounts and feeds with customizable per-calendar colors and easy enable/disable toggles.
- **Interactive Month Grid**: Click any date to view scheduled events for that day.
- **Visual Event Indicators**: Days with events show subtle colored dots corresponding to the calendar source.
- **Fast & Non-Blocking**: Background multi-threaded event fetcher with zero UI freezes.
- **Recurring & Multi-Day Events**: Full support for daily, weekly, monthly, and yearly recurring events (`RRULE` / `EXDATE`) and multi-day spans.

## Installation

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

### Via GUI

1. Open the Omarchy menu (**Super + Alt + Space**).
2. Go to **Install > Plugins**.
3. Paste the repository URL: `https://github.com/promaaa/sync-calendar-omarchy.git`
4. Hit Enter.

## Configuration

Configure your calendar feeds and preferences using the in-app **Settings Menu (`󰒓`)** or directly in `~/.config/omarchy/calendars.json`:

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
    "url": "webcal://pXX-caldav.icloud.com/published/2/xxxxxxxx",
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
      "color": "#ff5555",
      "username": "your_username",
      "password": "your_password",
      "enabled": true
   }

]
```

Edits to `calendars.json` hot-reload automatically without restarting the shell.

## Getting Calendar Links

### Google Calendar (Private iCal)
1. Open [Google Calendar](https://calendar.google.com/) on the web.
2. In the left sidebar, hover over your calendar $\rightarrow$ click the three dots $\rightarrow$ **Settings and sharing**.
3. Scroll down to **Integrate calendar** $\rightarrow$ Copy the **Secret address in iCal format**.

### Apple iCloud Calendar
1. Open [iCloud Calendar](https://www.icloud.com/calendar) or Apple Calendar on macOS / iOS.
2. Click the **Share** icon next to the calendar $\rightarrow$ Turn on **Public Calendar** (or share link).
3. Copy the `webcal://...` link.

### Proton Calendar
1. Open [Proton Calendar](https://calendar.proton.me/) on the web.
2. Go to **Settings** $\rightarrow$ **Calendars** $\rightarrow$ Click **Share** next to the calendar.
3. Under **Share outside Proton**, click **Create link** (choose Full details) and copy the `.ics` link.

### Microsoft Outlook / Office 365
1. Open [Outlook on the web](https://outlook.live.com/calendar/).
2. Go to **Settings (`󰒓`)** $\rightarrow$ **Calendar** $\rightarrow$ **Shared calendars**.
3. Under **Publish a calendar**, select your calendar and permissions $\rightarrow$ Click **Publish** $\rightarrow$ Copy the **ICS** link.

### JMAP Calendar (Fastmail, Stalwart, Cyrus, Apache James)

JMAP is a modern, fast, JSON-based calendar standard ([RFC 9670](https://www.rfc-editor.org/rfc/rfc9670) / [RFC 8984](https://www.rfc-editor.org/rfc/rfc8984)). It syncs directly via API tokens with zero OAuth setup required.

#### 1. Fastmail
1. Go to Fastmail **Settings (`󰒓`)** $\rightarrow$ **Privacy & Security** $\rightarrow$ **Integrations** $\rightarrow$ **Manage API tokens**.
2. Click **New API Token** $\rightarrow$ select **Calendars** (or Full access) $\rightarrow$ Generate and copy the token.
3. Add in the plugin settings (**Settings (`󰒓`) $\rightarrow$ + Add Calendar $\rightarrow$ JMAP**):
   - **Session URL**: `https://api.fastmail.com/jmap/session` *(default)*
   - **API Token**: `fmu1-xxxxxxxxxxxxxxxx`

#### 2. Stalwart / Cyrus / Generic JMAP Servers
- **Session URL**: `https://mail.example.com/.well-known/jmap` (or `https://mail.example.com/jmap/session`)
- **API Token**: Your user / application Bearer token
- **Calendar ID**: *(Optional)* Specific calendar ID or leave blank to query all calendars in your account.

### Nextcloud / ownCloud / Generic iCal
1. In your calendar web interface, open calendar settings / sharing options.
2. Look for **Public link**, **Subscription link**, or **Export / iCal link** (`.ics` or `webcal://`).
3. Paste the URL into the plugin settings.

### Google Calendar API (Restricted Shared Calendars)

> [!TIP]
> **For your own calendars**, use the **Secret address in iCal format** above — it requires zero API setup.
> The Google Calendar API is only needed for shared or organization calendars where iCal export is restricted.

#### 1. Setup Google Cloud Project & Credentials
1. Go to **[Google Cloud Console](https://console.cloud.google.com/)** and create a project (or select an existing one).
2. Enable the **Google Calendar API** under **APIs & Services $\rightarrow$ Library**.
3. Configure the **OAuth consent screen** (<https://console.cloud.google.com/apis/credentials/consent>):
   - Set User Type to **External** (or **Internal** if using a company Google Workspace account).
   - Enter an app name (e.g., `Omarchy Calendar`) and save.
   - Under **Test users**, click **+ Add users** and **add your Google email address**.
4. Create Credentials (<https://console.cloud.google.com/apis/credentials>):
   - Click **+ Create Credentials $\rightarrow$ OAuth client ID**.
   - Set Application type strictly to **Desktop app** *(do not select "Web application")*.
   - Copy your **Client ID** and **Client Secret** (or download the client JSON to `~/Downloads`).

#### 2. Run the Auth Helper
```bash
python3 ~/.config/omarchy/plugins/promaa.clock/google-auth.py "<CLIENT_ID>" "<CLIENT_SECRET>"
```
- A browser window will open asking you to sign in.
- If Google shows *"Google hasn't verified this app"*, click **Advanced $\rightarrow$ Go to Omarchy Calendar (unsafe) $\rightarrow$ Continue / Allow**.

#### 3. Find Your Calendar ID
- For your primary calendar: use `"primary"` or your full email address.
- For secondary or shared calendars: open [Google Calendar](https://calendar.google.com/) $\rightarrow$ Calendar settings $\rightarrow$ Scroll to **Integrate calendar** $\rightarrow$ Copy the exact **Calendar ID** (e.g., `xyz@group.calendar.google.com`).

#### Troubleshooting
* **`Error 400: redirect_uri_mismatch`**: Make sure the credential type is set to **Desktop app**, not Web application.
* **`Access blocked: App has not completed verification`**: Add your Google account email to **Test users** in the OAuth consent screen.
* **`HTTP Error 404: Not Found`**: Check that the `googleCalendarId` is the exact Calendar ID (not the display name). Test the sync with `python3 ~/.config/omarchy/plugins/promaa.clock/fetch-events.py`.


## Contributing

Contributions, bug reports, and suggestions are welcome. Feel free to open an issue or submit a pull request!

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
