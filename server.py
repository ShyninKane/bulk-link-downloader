#!/usr/bin/env python3
"""Local backend for Bulk Link Downloader.

Serves the UI (static/) and downloads links sequentially, with resume,
retry and verification. Runs entirely with the Python standard library.

Usage:
    python server.py
"""
import http.server
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
STATE_FILE = os.path.join(BASE_DIR, 'state.json')
DEFAULT_DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')
USER_AGENT = 'Mozilla/5.0 (compatible; BulkLinkDownloader/1.0)'
CHUNK_SIZE = 256 * 1024
PORT = 8765

lock = threading.RLock()

state = {
    'links': [],
    'downloadsDir': DEFAULT_DOWNLOADS_DIR,
    'maxRetries': 5,
    'running': False,
    'stopRequested': False,
}

subscribers = []
subscribers_lock = threading.Lock()

worker_thread = None

# Session cookie for authenticated downloads (e.g. archive.org items that
# require a logged-in account). Kept in memory only — never written to
# state.json — since it's a login credential.
session_cookie = ''


def get_cookie():
    with lock:
        return session_cookie


def set_cookie(value):
    global session_cookie
    with lock:
        session_cookie = value or ''


def request_headers(extra=None):
    headers = {'User-Agent': USER_AGENT}
    cookie = get_cookie()
    if cookie:
        headers['Cookie'] = cookie
    if extra:
        headers.update(extra)
    return headers


class RangeNotSatisfiable(Exception):
    pass


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def save_state():
    with lock:
        data = json.dumps(state)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(data)
    os.replace(tmp, STATE_FILE)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            state.update(loaded)
        except Exception:
            pass
    state['running'] = False
    state['stopRequested'] = False
    # Any item still marked 'downloading' was interrupted by a previous
    # process exit (crash, closed terminal, etc.) — no worker is running
    # yet, so it must go back to 'pending' or it'll show as stuck forever.
    fixed_any = False
    for it in state.get('links', []):
        if it.get('status') == 'downloading':
            it['status'] = 'pending'
            fixed_any = True
    if fixed_any:
        save_state()


def persister_loop():
    while True:
        time.sleep(2)
        with lock:
            running = state['running']
        if running:
            save_state()


# ---------------------------------------------------------------------------
# URL parsing / filename handling
# ---------------------------------------------------------------------------

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name):
    name = INVALID_CHARS.sub('_', name.strip())
    name = name.rstrip('. ')
    return name[:200] if name else 'file'


def parse_url(url):
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split('/') if p]
    if len(parts) >= 3 and parts[0] == 'download':
        identifier = urllib.parse.unquote(parts[1])
        filename = urllib.parse.unquote('/'.join(parts[2:]))
    elif parts:
        identifier = parsed.netloc or 'misc'
        filename = urllib.parse.unquote(parts[-1])
    else:
        identifier = parsed.netloc or 'misc'
        filename = 'file'
    return sanitize(identifier), sanitize(filename)


def new_item(idx, url):
    identifier, filename = parse_url(url)
    return {
        'id': idx,
        'url': url,
        'identifier': identifier,
        'filename': filename,
        'status': 'pending',
        'attempts': 0,
        'bytesDone': 0,
        'bytesTotal': None,
        'error': None,
    }


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------

def head_request(url):
    req = urllib.request.Request(url, method='HEAD', headers=request_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        length = resp.headers.get('Content-Length')
        return int(length) if length is not None else None


def stream_download(item, dest, resume_from, expected_total):
    extra = {}
    if resume_from > 0:
        extra['Range'] = 'bytes=%d-' % resume_from
    req = urllib.request.Request(item['url'], headers=request_headers(extra))
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            raise RangeNotSatisfiable()
        raise
    with resp:
        status = resp.status
        total = expected_total
        if resume_from > 0 and status == 206:
            mode = 'ab'
            downloaded = resume_from
            content_range = resp.headers.get('Content-Range')
            if content_range:
                m = re.match(r'bytes \d+-\d+/(\d+)', content_range)
                if m:
                    total = int(m.group(1))
        else:
            mode = 'wb'
            downloaded = 0
            cl = resp.headers.get('Content-Length')
            if cl is not None:
                total = int(cl)
        item['bytesTotal'] = total
        last_report = 0.0
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_report > 0.2:
                    item['bytesDone'] = downloaded
                    last_report = now
        item['bytesDone'] = downloaded
    return downloaded, total


def try_once(item, dest):
    try:
        try:
            expected_total = head_request(item['url'])
        except Exception:
            expected_total = None

        resume_from = os.path.getsize(dest) if os.path.exists(dest) else 0
        if expected_total is not None and resume_from > expected_total:
            resume_from = 0

        if expected_total is not None and resume_from == expected_total and resume_from > 0:
            downloaded, total = resume_from, expected_total
        else:
            try:
                downloaded, total = stream_download(item, dest, resume_from, expected_total)
            except RangeNotSatisfiable:
                if os.path.exists(dest):
                    os.remove(dest)
                downloaded, total = stream_download(item, dest, 0, expected_total)

        if total is not None and downloaded != total:
            return False, 'Incomplete download: %d of %d bytes' % (downloaded, total)

        item['bytesDone'] = downloaded
        item['bytesTotal'] = total
        return True, None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, 'HTTP 401 Unauthorized — this item requires a logged-in archive.org session. Paste your browser cookie in Settings.'
        return False, 'HTTP %d %s' % (e.code, e.reason)
    except urllib.error.URLError as e:
        return False, 'Network error: %s' % e.reason
    except OSError as e:
        return False, 'File error: %s' % e.strerror
    except Exception as e:
        return False, str(e)


def process_item(item, max_retries, downloads_dir):
    folder = os.path.join(downloads_dir, item['identifier'])
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, item['filename'])

    while True:
        with lock:
            stop = state['stopRequested']
        if stop:
            item['status'] = 'pending'
            save_state()
            return
        if item['attempts'] >= max_retries:
            item['status'] = 'failed'
            save_state()
            broadcast_event({
                'type': 'error', 'id': item['id'], 'filename': item['filename'],
                'url': item['url'], 'error': item['error'], 'attempts': item['attempts'],
            })
            return

        item['attempts'] += 1
        item['status'] = 'downloading'
        item['error'] = None

        ok, err = try_once(item, dest)
        if ok:
            item['status'] = 'downloaded'
            item['error'] = None
            save_state()
            return

        item['error'] = err
        item['status'] = 'pending'
        if item['attempts'] < max_retries:
            backoff = min(30, 2 ** item['attempts'])
            waited = 0.0
            while waited < backoff:
                with lock:
                    if state['stopRequested']:
                        break
                time.sleep(0.2)
                waited += 0.2


def worker_loop():
    with lock:
        state['running'] = True
        state['stopRequested'] = False
    save_state()
    broadcast_event({'type': 'started'})
    try:
        while True:
            with lock:
                if state['stopRequested']:
                    break
                item = next((it for it in state['links'] if it['status'] in ('pending', 'downloading')), None)
                max_retries = state['maxRetries']
                downloads_dir = state['downloadsDir']
            if item is None:
                break
            process_item(item, max_retries, downloads_dir)
    finally:
        with lock:
            state['running'] = False
        save_state()
        broadcast_event({'type': 'finished', 'summary': compute_summary()})


def start_worker():
    global worker_thread
    with lock:
        if state['running']:
            return
        state['stopRequested'] = False
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------

def broadcast_event(evt):
    with subscribers_lock:
        subs = list(subscribers)
    data = ('data: %s\n\n' % json.dumps(evt)).encode('utf-8')
    for q in subs:
        q.put(data)


def compute_summary():
    with lock:
        links = state['links']
        counts = {'pending': 0, 'downloading': 0, 'downloaded': 0, 'failed': 0}
        for it in links:
            counts[it['status']] = counts.get(it['status'], 0) + 1
        return {
            'total': len(links),
            'pending': counts['pending'],
            'downloading': counts['downloading'],
            'downloaded': counts['downloaded'],
            'failed': counts['failed'],
            'running': state['running'],
            'stopRequested': state['stopRequested'],
            'maxRetries': state['maxRetries'],
            'downloadsDir': state['downloadsDir'],
            'cookieSet': bool(get_cookie()),
        }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'BulkLinkDownloader/1.0'

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b''
        return json.loads(raw.decode('utf-8')) if raw else {}

    def _serve_static(self, rel_path):
        path = os.path.join(STATIC_DIR, rel_path)
        ext = os.path.splitext(rel_path)[1]
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', CONTENT_TYPES.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse(self):
        q = queue.Queue()
        with subscribers_lock:
            subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(data)
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            with subscribers_lock:
                if q in subscribers:
                    subscribers.remove(q)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/':
            self._serve_static('index.html')
        elif path in ('/app.js', '/style.css'):
            self._serve_static(path.lstrip('/'))
        elif path == '/api/summary':
            self._send_json(compute_summary())
        elif path == '/api/state':
            offset = int((qs.get('offset') or ['0'])[0])
            limit = min(500, int((qs.get('limit') or ['100'])[0]))
            status_filter = (qs.get('status') or ['all'])[0]
            search = (qs.get('search') or [''])[0].lower()
            with lock:
                items = state['links']
                if status_filter != 'all':
                    items = [it for it in items if it['status'] == status_filter]
                if search:
                    items = [it for it in items if search in it['filename'].lower() or search in it['url'].lower()]
                total_matched = len(items)
                page = [dict(it) for it in items[offset:offset + limit]]
            self._send_json({'items': page, 'totalMatched': total_matched})
        elif path == '/api/export-failed':
            with lock:
                failed = [it for it in state['links'] if it['status'] == 'failed']
                lines = ['%s\t%s' % (it['url'], it['error'] or '') for it in failed]
            body = ('\n'.join(lines) + '\n').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="failed_links.txt"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/events':
            self._handle_sse()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/load':
            body = self._read_json()
            urls = body.get('links', [])
            with lock:
                existing = {it['url']: it for it in state['links']}
                new_links = []
                seen = set()
                for u in urls:
                    u = u.strip()
                    if not u or u in seen:
                        continue
                    seen.add(u)
                    new_links.append(existing[u] if u in existing else new_item(len(new_links), u))
                for i, it in enumerate(new_links):
                    it['id'] = i
                state['links'] = new_links
            save_state()
            self._send_json({'ok': True, 'total': len(new_links)})
        elif path == '/api/start':
            start_worker()
            self._send_json({'ok': True})
        elif path == '/api/stop':
            with lock:
                state['stopRequested'] = True
            save_state()
            self._send_json({'ok': True})
        elif path == '/api/retry-failed':
            with lock:
                count = 0
                for it in state['links']:
                    if it['status'] == 'failed':
                        it['status'] = 'pending'
                        it['attempts'] = 0
                        it['error'] = None
                        count += 1
            save_state()
            self._send_json({'ok': True, 'count': count})
        elif path == '/api/config':
            body = self._read_json()
            with lock:
                if 'maxRetries' in body:
                    state['maxRetries'] = max(1, int(body['maxRetries']))
                if body.get('downloadsDir'):
                    state['downloadsDir'] = body['downloadsDir']
            if 'cookie' in body:
                set_cookie(body['cookie'])
            save_state()
            self._send_json({'ok': True})
        else:
            self.send_error(404)


def main():
    load_state()
    threading.Thread(target=persister_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    url = 'http://127.0.0.1:%d/' % PORT
    print('Bulk Link Downloader running at %s' % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        save_state()


if __name__ == '__main__':
    main()
