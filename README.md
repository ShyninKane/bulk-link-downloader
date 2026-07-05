# Bulk Link Downloader

Downloads a large list of direct file links (e.g. archive.org `/download/...` URLs)
one at a time, with resume, retry, and verification, tracked in a browser UI.
Built for lists on the order of thousands of links (e.g. a full archive.org
collection), with a virtualized table so the UI stays responsive at that scale.

## Features

- Sequential downloads with per-link status: pending / downloading / downloaded / failed
- Automatic retry with backoff, and resume via HTTP range requests for partial files
- Start / Stop, where Stop lets the in-flight file finish before halting
- Progress persisted to disk (`state.json`) — survives browser refresh and script restart
- Cookie support for sites that require a logged-in session (see below)
- Virtualized, filterable, searchable table for large link lists

## Requirements

Python 3 standard library only — no `pip install` needed.

## Why Python, not pure HTML/JS

Archive.org's redirect response (`archive.org/download/...`) sends
`Access-Control-Allow-Origin: *`, but the actual file server it redirects to
(`dnXXXX.ca.archive.org`) does **not** send CORS headers. That blocks a
browser's `fetch()` from reading the response body, so a pure client-side page
cannot verify sizes, show byte-level progress, or resume partial downloads for
these links. The small Python backend does the actual HTTP requests
server-side (no CORS involved there) and serves the UI on `localhost`, so the
page talks to it same-origin.

## Run

```
python server.py
```

This opens `http://127.0.0.1:8765/` in your browser automatically.

## Use

1. Paste links (one per line) into the text box, or choose a `.txt` file, then click **Load Links**.
2. Optionally set max retries or change the downloads folder — these save automatically as you edit them.
3. Click **Start**. Files download one at a time into `downloads/<archive-identifier>/<filename>`.
4. **Stop** finishes the file currently downloading, then halts before the next one.
5. Failed links can be retried with **Retry Failed**, or exported to a text file with **Export Failed List**.
6. Progress is saved to `state.json` next to `server.py`, so closing the browser or restarting the script keeps prior progress — re-running `Start` resumes where it left off (including partial files, via HTTP range requests where the server supports them).

## "HTTP 401 Unauthorized" errors

Some archive.org items are restricted to logged-in accounts — anonymous
requests to their `/download/...` links get a 401 even though the item page
loads fine in your browser. To fix:

1. Log into the site in your normal browser.
2. Open DevTools → Network tab, reload the page, click any request to that site.
3. Copy the full value of the `Cookie` request header.
4. Paste it into the "Site cookie" field in the tool — it saves automatically as you type.

The cookie is sent with every download/HEAD request from then on. It's kept
in memory and in your browser's `localStorage` only — it is never written to
`state.json`, so it won't end up on disk in plain text next to the tool.

## Notes

- Filenames and identifiers are taken from the URL path (`/download/<identifier>/<filename>`); for other URL shapes the last path segment is used as the filename.
- A download counts as successful when the received byte count matches the server's reported size.
- Desktop notifications require granting permission in the browser once the "Desktop notifications" checkbox is ticked.

## Project structure

```
server.py          backend: HTTP API, download/retry/resume logic, state persistence
static/index.html   page structure
static/app.js       UI logic: virtualized table, polling, SSE, settings
static/style.css    styling
state.json          generated at runtime — link list + progress (gitignored)
downloads/          generated at runtime — downloaded files (gitignored)
```
