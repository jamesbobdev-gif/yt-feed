# My Feed — personal YouTube channel feed

A local web app that shows only the latest videos from channels you choose.
No YouTube homepage, no recommendations, no Shorts, no API key.

## Setup

1. **Requirements**: Python 3.10+ (standard library only — no `pip install`).
2. **Create your channel list**: `cp channels.example.json channels.json`
   (Windows: `copy channels.example.json channels.json`), or just start the
   server and add channels from the UI.
3. **Run**: `python server.py`, then open <http://localhost:3007>.

### Optional: auto-start on Windows
Copy `Start My Feed.example.vbs` → `Start My Feed.vbs` and
`watchdog.example.vbs` → `watchdog.vbs`, edit the folder path inside each for
your machine, then double-click `Start My Feed.vbs` to launch — or point a Task
Scheduler job (at logon + every 5 min) / a Startup-folder shortcut at
`watchdog.vbs` to keep it alive. Errors land in `server.log`.

## Use it

- **Add a channel**: paste `@handle`, any channel/video URL, or a raw `UC...` id into the box. It's added to whichever tab (Videos / Music) is active; re-adding an existing channel moves it to the current tab.
- **Filter**: click a channel chip; click ✕ on a chip to remove the channel.
- **Play (Videos tab)**: click a card — plays in an embedded player (youtube-nocookie, `rel=0` so related videos stay on-channel). Watched videos are dimmed (stored in the browser's localStorage).
- **Music tab**: audio only — clicking a track plays it through an invisible player; a bottom bar gives play/pause/next/prev/stop and tracks auto-advance down the list. Music keeps playing while you browse the Videos tab (and pauses if you start a video). The music section is not limited to the 1-month window.

## How it works

YouTube shut down its public RSS feeds in 2026, so `server.py` fetches each
channel's `/videos` page and parses the embedded `ytInitialData` JSON
(`lockupViewModel` items). Shorts and livestreams are excluded. Results are
cached for 10 minutes per channel.
Only videos from the last ~1 month are shown (`MAX_AGE_DAYS` in `server.py`).
Your channel list lives in `channels.json` next to the server.

If YouTube changes its page markup again, the place to fix is
`parse_lockup()` / `find_video_renderers()` in `server.py`.
