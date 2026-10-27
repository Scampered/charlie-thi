"""POST /scrape  {"url": "https://en.wikipedia.org/wiki/Munich https://en.wikipedia.org/wiki/Audi"}
(nginx forwards the browser's /api/scrape to this /scrape route, like /api/train -> /train)

Runs the Scrapy spider on one or more Wikipedia pages (separated by spaces,
commas or new lines), saves the result in data.json, adds the cleaned
sentences to sentences.json and reloads Charlie.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import Blueprint, jsonify, request

from . import charlie, prepare_data  # gunicorn loads this folder as the package "app"

BASE = Path(__file__).resolve().parent
SPIDER_PATH = BASE / "caifirstbot.py"
DATA_PATH = BASE / "data.json"
SENTENCES_PATH = BASE / "sentences.json"
BACKUP_PATH = BASE / "sentences.backup.json"   # last state before "Clear Memory"

MAX_LINKS = 5        # pages per scrape
SCRAPE_TIMEOUT = 45  # seconds; keep below nginx's default 60 s proxy timeout
USER_AGENT = "CharlieBot/1.0 (TH Ingolstadt student project)"

_scrape_lock = threading.Lock()
scrape_bp = Blueprint("scrape", __name__)


class ScrapeBusy(RuntimeError):
    pass


def validate_wikipedia_url(raw):
    """Return (clean_url, page_title). Only https Wikipedia article links are allowed,
    so nobody can make the server scrape arbitrary sites."""
    raw = (raw or "").strip()
    if not raw or len(raw) > 500:
        raise ValueError("Please enter a Wikipedia link.")
    if "://" not in raw:
        raw = "https://" + raw

    bad = f"Not a valid Wikipedia link: {raw[:80]} (example: https://en.wikipedia.org/wiki/Munich)"
    try:
        parts = urlparse(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ValueError(bad)

    if parts.scheme not in ("http", "https") or parts.username or parts.password or port:
        raise ValueError(bad)
    if not (host == "wikipedia.org" or host.endswith(".wikipedia.org")):
        raise ValueError(f"Only Wikipedia links are allowed: {raw[:80]}")
    if not parts.path.startswith("/wiki/") or len(parts.path) <= len("/wiki/"):
        raise ValueError(bad)
    if any(c.isspace() for c in parts.path):
        raise ValueError(bad)

    title = unquote(parts.path[len("/wiki/"):]).replace("_", " ")
    return f"https://{host}{parts.path}", title


def validate_wikipedia_urls(raw):
    """Split the box text into links and check each one. Returns [(url, title), ...]."""
    pieces = [p for p in re.split(r"[\s,]+", raw or "") if p]
    if not pieces:
        raise ValueError("Please enter a Wikipedia link.")
    if len(pieces) > MAX_LINKS:
        raise ValueError(f"Please enter at most {MAX_LINKS} links at once.")
    pages, seen = [], set()
    for piece in pieces:
        url, title = validate_wikipedia_url(piece)
        if url not in seen:
            seen.add(url)
            pages.append((url, title))
    return pages


def _short_reason(stderr):
    """The last meaningful line of a crash, e.g. "No module named scrapy"."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    return lines[-1][:200] if lines else "unknown error"


def _run_spider(urls):
    """Run `scrapy runspider caifirstbot.py` on all links and move the result into data.json."""
    tmp_out = BASE / "data.scrape.json"
    if tmp_out.exists():
        tmp_out.unlink()

    cmd = [
        sys.executable, "-m", "scrapy", "runspider", str(SPIDER_PATH),
        "-a", "url=" + " ".join(urls),
        "-O", str(tmp_out),  # -O overwrites; -o would append to the old file
        "-s", "LOG_LEVEL=ERROR",
        "-s", "ROBOTSTXT_OBEY=True",
        "-s", f"USER_AGENT={USER_AGENT}",
        "-s", "DOWNLOAD_TIMEOUT=20",
        "-s", "RETRY_TIMES=1",
    ]
    try:
        result = subprocess.run(cmd, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=SCRAPE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Scraping took too long and was stopped. Try fewer links or try again later.")

    if result.returncode != 0:
        print("scrapy failed:", (result.stderr or "")[-1500:])
        raise RuntimeError("The scraper could not run: " + _short_reason(result.stderr))
    if result.stderr.strip():
        print("scrapy messages:", result.stderr[-1500:])

    items = []
    if tmp_out.exists():
        text = tmp_out.read_text(encoding="utf-8").strip()
        items = json.loads(text) if text else []
    if not any(i.get("tag") == "p" for i in items if isinstance(i, dict)):
        if tmp_out.exists():
            tmp_out.unlink()
        reason = _short_reason(result.stderr) if result.stderr.strip() else "page not found or empty"
        raise ValueError("Nothing could be scraped from that link (" + reason + ").")

    os.replace(str(tmp_out), str(DATA_PATH))  # the result ends up in data.json
    return {i.get("source") for i in items if isinstance(i, dict)}


def scrape_and_update(raw):
    pages = validate_wikipedia_urls(raw)

    if not _scrape_lock.acquire(blocking=False):
        raise ScrapeBusy("Another scrape is still running. Please wait a moment.")
    try:
        found = _run_spider([url for url, _ in pages])
        try:
            added, total = prepare_data.update_sentences(DATA_PATH, SENTENCES_PATH, append=True)
        except (OSError, ValueError) as e:
            print("prepare_data failed:", e)
            raise RuntimeError("The scraped data could not be processed: " + str(e)[:200])
        # "Retrain": make Charlie use the new sentences right away
        charlie.reload_index(force=True)
    finally:
        _scrape_lock.release()

    titles = ", ".join(f"'{t}'" for _, t in pages)
    missing = [t for _, t in pages if t not in found]
    if added:
        message = f"Added {added} new sentences from {titles} ({total} in total). Ask away!"
    else:
        message = f"No new sentences from {titles} - Charlie already knew everything on it."
    if missing and len(pages) > 1:
        message += " (Nothing came back from: " + ", ".join(missing) + ")"
    return {"message": message, "added": added, "total": total}


def clear_memory():
    """Empty sentences.json so Charlie forgets every scraped page.
    A copy of the old file is kept as sentences.backup.json, just in case."""
    if not _scrape_lock.acquire(blocking=False):
        raise ScrapeBusy("A scrape is still running. Please wait a moment.")
    try:
        old_count = len(prepare_data.load_existing(SENTENCES_PATH))
        if SENTENCES_PATH.exists():
            shutil.copyfile(str(SENTENCES_PATH), str(BACKUP_PATH))
        prepare_data.write_sentences([], SENTENCES_PATH)   # sentences.json becomes an empty list
        charlie.reload_index(force=True)                   # Charlie forgets right away
    finally:
        _scrape_lock.release()
    return {"message": f"Memory cleared: {old_count} sentences removed.", "removed": old_count}


@scrape_bp.route("/clear-memory", methods=["POST"])
def clear_memory_route():
    """POST /clear-memory  (the browser calls it as /api/clear-memory)"""
    try:
        return jsonify(clear_memory())
    except ScrapeBusy as e:
        return jsonify(error=str(e)), 429
    except (OSError, ValueError) as e:
        print("clear memory failed:", e)
        return jsonify(error="Could not clear the memory: " + str(e)[:200]), 500


@scrape_bp.route("/scrape", methods=["POST"])
def scrape():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(scrape_and_update(payload.get("url", "")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except ScrapeBusy as e:
        return jsonify(error=str(e)), 429
    except RuntimeError as e:
        return jsonify(error=str(e)), 502
