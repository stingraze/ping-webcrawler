# (C)Tsubasa Kato - Inspire Search Corporation
# Last Updated: 2026/02/03 JST
# Visit our company at: https://www.inspiresearch.io/en
#
# ping-webcrawler: measures response time first, then crawls & extracts metadata/content.
# UI/UX refresh: 2026-grade responsive UI + light/dark mode + SSE progress + results preview.

from __future__ import annotations

import csv
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue, Empty
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import chardet
import heapq
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, Response, jsonify, render_template, request, send_file

app = Flask(__name__)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DEFAULTS = {
    "REQUEST_TIMEOUT": 5,
    "MAX_DEPTH": 3,
    "MAX_URLS": 100,
    "MAX_CHARS": 800,
    "WORKERS": 10,
    "SAME_HOST_ONLY": True,
    "FLUSH_INTERVAL_SEC": 5,
}

USER_AGENT = (
    "ping-webcrawler/2026 (+https://www.inspiresearch.io/en) "
    "requests; contact=info@inspiresearch.io"
)


@dataclass
class CrawlerState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    logs: Queue = field(default_factory=Queue)  # strings
    results: List[Dict] = field(default_factory=list)

    running: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)

    config: Dict = field(default_factory=lambda: dict(DEFAULTS))
    csv_path: Optional[Path] = None
    run_id: Optional[str] = None

    def reset(self, config: Dict):
        with self.lock:
            self.config = {**DEFAULTS, **config}
            self.results.clear()
            self.done.clear()
            self.stop.clear()
            self.running.set()

            # drain logs
            while True:
                try:
                    self.logs.get_nowait()
                except Empty:
                    break

            self.run_id = str(int(time.time()))
            self.csv_path = DATA_DIR / f"extracted_data_{self.run_id}.csv"

    def log(self, msg: str):
        # Keep logs reasonably bounded by dropping old items if queue grows too large
        try:
            if self.logs.qsize() > 2000:
                for _ in range(200):
                    self.logs.get_nowait()
        except Exception:
            pass
        self.logs.put(msg)

    def snapshot_state(self) -> Dict:
        with self.lock:
            return {
                "running": self.running.is_set(),
                "done": self.done.is_set(),
                "stopped": self.stop.is_set(),
                "count": len(self.results),
                "max_urls": int(self.config["MAX_URLS"]),
                "run_id": self.run_id,
                "csv_ready": bool(self.csv_path and self.csv_path.exists()),
                "csv_name": self.csv_path.name if self.csv_path else None,
            }


STATE = CrawlerState()


def signal_handler(sig, frame):
    STATE.log("Interrupt received. Stopping crawler…")
    STATE.stop.set()
    STATE.running.clear()
    STATE.done.set()
    _flush_csv_atomic()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


def _normalize_url(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None

    # Default scheme if missing
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")

    if parsed.scheme not in ("http", "https"):
        return None

    # Drop fragments
    parsed = parsed._replace(fragment="")

    # Basic sanity
    if not parsed.netloc:
        return None

    # Normalize: remove default ports, etc.
    netloc = parsed.netloc
    if netloc.endswith(":80") and parsed.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and parsed.scheme == "https":
        netloc = netloc[:-4]

    normalized = urlunparse(
        (parsed.scheme, netloc, parsed.path or "/", parsed.params, parsed.query, "")
    )
    return normalized


def _is_same_host(a: str, b: str) -> bool:
    try:
        return urlparse(a).netloc == urlparse(b).netloc
    except Exception:
        return False


def measure_response_time(session: requests.Session, url: str, timeout: int) -> Tuple[float, str]:
    if STATE.stop.is_set():
        return (float("inf"), url)

    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        elapsed = r.elapsed.total_seconds()
        STATE.log(f"RTT measured: {url} -> {elapsed:.3f}s (HTTP {r.status_code})")
        return (elapsed, url)
    except (requests.RequestException, TimeoutError) as e:
        STATE.log(f"RTT timeout/error: {url} ({type(e).__name__})")
        return (float("inf"), url)


def extract_metadata_and_content(response: requests.Response, max_chars: int) -> Tuple[str, str, str, str, List[str]]:
    detected_encoding = chardet.detect(response.content).get("encoding") or "utf-8"
    response.encoding = detected_encoding

    soup = BeautifulSoup(response.text, "html.parser")

    title = (soup.title.string.strip() if soup.title and soup.title.string else "N/A")

    keywords_tag = soup.find("meta", {"name": "keywords"})
    keywords = keywords_tag.get("content", "N/A").strip() if keywords_tag else "N/A"

    desc_tag = soup.find("meta", {"name": "description"})
    description = desc_tag.get("content", "N/A").strip() if desc_tag else "N/A"

    body_text = "N/A"
    if soup.body:
        body_text = soup.body.get_text(" ", strip=True)

    # Links
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(response.url, href)
        norm = _normalize_url(absolute)
        if norm:
            links.append(norm)

    # Trim large fields
    return (
        title[:max_chars],
        keywords[:max_chars],
        description[:max_chars],
        body_text[:max_chars],
        links,
    )


def _flush_csv_atomic():
    # Writes STATE.results to STATE.csv_path atomically (safe download mid-run)
    snap = None
    csv_path = None
    with STATE.lock:
        csv_path = STATE.csv_path
        snap = list(STATE.results)

    if not csv_path:
        return

    tmp_path = csv_path.with_suffix(".tmp")
    fieldnames = ["URL", "response_time_sec", "title", "keywords", "description", "body_content"]

    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in snap:
                w.writerow({
                    "URL": row.get("URL", ""),
                    "response_time_sec": row.get("response_time_sec", ""),
                    "title": row.get("title", ""),
                    "keywords": row.get("keywords", ""),
                    "description": row.get("description", ""),
                    "body_content": row.get("body_content", ""),
                })
        tmp_path.replace(csv_path)
    except Exception as e:
        STATE.log(f"CSV flush failed: {type(e).__name__}: {e}")


def periodic_flush():
    interval = int(STATE.config.get("FLUSH_INTERVAL_SEC", DEFAULTS["FLUSH_INTERVAL_SEC"]))
    while STATE.running.is_set() and not STATE.done.is_set() and not STATE.stop.is_set():
        time.sleep(max(1, interval))
        _flush_csv_atomic()
    _flush_csv_atomic()


def crawl_bfs(session: requests.Session, start_url: str, base_host_url: str, visited: Set[str]):
    cfg = STATE.config
    timeout = int(cfg["REQUEST_TIMEOUT"])
    max_depth = int(cfg["MAX_DEPTH"])
    max_urls = int(cfg["MAX_URLS"])
    max_chars = int(cfg["MAX_CHARS"])
    same_host_only = bool(cfg["SAME_HOST_ONLY"])

    queue: List[Tuple[str, int]] = [(start_url, 0)]

    while queue and not STATE.stop.is_set():
        url, depth = queue.pop(0)

        if depth > max_depth:
            continue

        with STATE.lock:
            if len(STATE.results) >= max_urls:
                return

        if url in visited:
            continue
        visited.add(url)

        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            rt = r.elapsed.total_seconds()
            STATE.log(f"Crawl: {url} (depth={depth}) -> HTTP {r.status_code} in {rt:.3f}s")

            if r.status_code != 200:
                continue

            title, keywords, description, body_content, links = extract_metadata_and_content(r, max_chars)

            with STATE.lock:
                if len(STATE.results) < max_urls:
                    STATE.results.append({
                        "URL": url[:max_chars],
                        "response_time_sec": round(rt, 4),
                        "title": title,
                        "keywords": keywords,
                        "description": description,
                        "body_content": body_content,
                    })

            # enqueue next links
            if depth < max_depth:
                for link in links:
                    if same_host_only and not _is_same_host(link, base_host_url):
                        continue
                    if link not in visited:
                        queue.append((link, depth + 1))

        except (requests.RequestException, TimeoutError) as e:
            STATE.log(f"Crawl timeout/error: {url} ({type(e).__name__})")


def optimized_crawl(urls: List[str]):
    cfg = STATE.config
    timeout = int(cfg["REQUEST_TIMEOUT"])
    workers = int(cfg["WORKERS"])
    max_urls = int(cfg["MAX_URLS"])

    t_flush = threading.Thread(target=periodic_flush, daemon=True)
    t_flush.start()

    visited: Set[str] = set()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        # Measure RTT first
        with ThreadPoolExecutor(max_workers=workers) as ex:
            response_times = list(ex.map(lambda u: measure_response_time(session, u, timeout), urls))

        valid = [(t, u) for (t, u) in response_times if t != float("inf")]
        heapq.heapify(valid)

        if not valid:
            STATE.log("No reachable URLs after RTT measurement.")
            return

        # Crawl in RTT order
        while valid and not STATE.stop.is_set():
            with STATE.lock:
                if len(STATE.results) >= max_urls:
                    break

            _, url = heapq.heappop(valid)
            base = url  # used for same-host checks
            crawl_bfs(session, url, base, visited)

            # Small pacing (polite)
            time.sleep(0.2)

    finally:
        try:
            session.close()
        except Exception:
            pass

        STATE.done.set()
        STATE.running.clear()
        _flush_csv_atomic()
        STATE.log("Crawling finished.")


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", defaults=DEFAULTS)


@app.route("/api/start", methods=["POST"])
def api_start():
    payload = request.get_json(force=True, silent=True) or {}
    urls_raw = payload.get("urls", "")

    urls = []
    for line in (urls_raw or "").splitlines():
        norm = _normalize_url(line)
        if norm:
            urls.append(norm)

    urls = list(dict.fromkeys(urls))  # dedupe, preserve order
    if not urls:
        return jsonify({"ok": False, "error": "No valid URLs found."}), 400

    config = {
        "REQUEST_TIMEOUT": int(payload.get("REQUEST_TIMEOUT", DEFAULTS["REQUEST_TIMEOUT"])),
        "MAX_DEPTH": int(payload.get("MAX_DEPTH", DEFAULTS["MAX_DEPTH"])),
        "MAX_URLS": int(payload.get("MAX_URLS", DEFAULTS["MAX_URLS"])),
        "MAX_CHARS": int(payload.get("MAX_CHARS", DEFAULTS["MAX_CHARS"])),
        "WORKERS": int(payload.get("WORKERS", DEFAULTS["WORKERS"])),
        "SAME_HOST_ONLY": bool(payload.get("SAME_HOST_ONLY", DEFAULTS["SAME_HOST_ONLY"])),
        "FLUSH_INTERVAL_SEC": int(payload.get("FLUSH_INTERVAL_SEC", DEFAULTS["FLUSH_INTERVAL_SEC"])),
    }

    STATE.reset(config)
    STATE.log(f"Starting crawl. Seeds={len(urls)} depth≤{config['MAX_DEPTH']} max_urls={config['MAX_URLS']}")

    threading.Thread(target=optimized_crawl, args=(urls,), daemon=True).start()

    return jsonify({"ok": True, "state": STATE.snapshot_state()})


@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify({"ok": True, "state": STATE.snapshot_state()})


@app.route("/api/results", methods=["GET"])
def api_results():
    limit = int(request.args.get("limit", "50"))
    limit = max(1, min(limit, 500))

    with STATE.lock:
        data = list(reversed(STATE.results))[:limit]  # newest first
        count = len(STATE.results)

    return jsonify({"ok": True, "count": count, "items": data})


@app.route("/progress", methods=["GET"])
def progress_sse():
    def gen():
        # Initial state
        yield f"event: state\ndata: {json.dumps(STATE.snapshot_state())}\n\n"

        last_heartbeat = 0.0
        while True:
            # Exit condition: done + queue empty
            if STATE.done.is_set() and STATE.logs.empty():
                yield f"event: state\ndata: {json.dumps(STATE.snapshot_state())}\n\n"
                break

            try:
                msg = STATE.logs.get(timeout=0.6)
                yield f"event: log\ndata: {json.dumps({'message': msg})}\n\n"
            except Empty:
                pass

            # heartbeat / state update
            now = time.time()
            if now - last_heartbeat > 2.0:
                last_heartbeat = now
                yield f"event: state\ndata: {json.dumps(STATE.snapshot_state())}\n\n"

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/stop", methods=["POST"])
def stop():
    STATE.stop.set()
    STATE.log("Stop requested by user.")
    return jsonify({"ok": True, "state": STATE.snapshot_state()})


@app.route("/download", methods=["GET"])
def download():
    with STATE.lock:
        p = STATE.csv_path

    if not p or not p.exists():
        return jsonify({"ok": False, "error": "No CSV available yet."}), 404

    return send_file(p, as_attachment=True, download_name=p.name, mimetype="text/csv")


if __name__ == "__main__":
    # Note: SSE works best with a threaded server.
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
