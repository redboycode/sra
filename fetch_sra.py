#!/usr/bin/env python3
"""
Fetch a list of RSS/Atom feeds and write a single aggregated feed.json
Run on a schedule (see .github/workflows/update-feed.yml) — not in the browser.
"""

import json
import sys
from datetime import datetime, timezone
from time import mktime

import feedparser
import requests

# ---- Edit this list to add/remove blogs (RSS/Atom feeds) ----
# Soccer tactical-analysis sources only. Verified 2026-09-06: all have a
# real, populated feed except the two noted below, which return HTTP 200
# (or a Cloudflare challenge) with zero usable entries — kept in the list so
# they pick up automatically if the site comes back to life, not because
# they're currently contributing anything.
FEEDS = [
    {"source": "Football Bunseki", "url": "https://footballbunsekicom.com/feed/"},
    {"source": "The Mastermind Site", "url": "https://themastermindsite.com/feed/"},
    {"source": "Coaches' Voice", "url": "https://learning.coachesvoice.com/feed/"},
    {"source": "Between The Posts", "url": "https://betweentheposts.net/feed/"},
    {"source": "Tactics Journal", "url": "https://tacticsjournal.com/feed.xml"},
    # Not to be confused with totalfootballanalysis.com below — different site.
    {"source": "Tactical Football Analysis", "url": "https://tacticalfootballanalysis.com/feed/"},
    {"source": "Holding Midfield", "url": "https://holdingmidfield.com/feed/"},
    {"source": "Spielverlagerung", "url": "https://spielverlagerung.com/feed/"},
    {"source": "Outswinger FC", "url": "https://outswingerfc.substack.com/feed"},
    # Cloudflare bot-challenge page as of 2026-09-06 ("Just a moment..."), not
    # a real 403 — feedparser will just get 0 entries from the challenge HTML.
    # Left in in case the challenge lifts; not worth a scraper.
    {"source": "Total Football Analysis", "url": "https://totalfootballanalysis.com/feed/"},
    # Editorial blog looks inactive since the Hudl acquisition — 0 entries.
    {"source": "StatsBomb", "url": "https://statsbomb.com/feed/"},
]

# ---- Sources needing tag-based filtering at fetch time ----
# The Analyst (Opta) is multi-sport — NFL, tennis, MLB, college football all
# come through the same feed. Every soccer entry carries a "Football"
# category tag; every non-soccer entry (FCS Football, NFL, MLB, Tennis) does
# not — verified against a live pull of all 70 entries, no ambiguous cases.
TAG_FILTERED_FEEDS = [
    {"source": "The Analyst", "url": "https://theanalyst.com/feed/", "require_tag": "Football"},
]

TIMEOUT_SECONDS = 10
OUTPUT_PATH = "feed.json"
# A custom UA can get silently capped or blocked by some sites' CDNs (see
# qra's Tower Research note) — a standard browser UA fetches reliably.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
MAX_ITEMS_PER_SOURCE = 200
MAX_BACKFILL_PAGES = 50


def parse_entry_date(entry):
    """Return an ISO timestamp, falling back gracefully if missing."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            dt = datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
            return dt.isoformat()
    return datetime.now(tz=timezone.utc).isoformat()


def fetch_one(url, source_name, deep=False, require_tag=None):
    """deep=True paginates via WordPress's ?paged=N convention until a page
    comes back with nothing new — used for one-time backfills. Regular runs
    only fetch page 1; accumulation in main() is what keeps older items
    around after that, not re-fetching deep every hour."""
    items = []
    seen_links = set()
    for page in range(1, MAX_BACKFILL_PAGES + 1) if deep else [1]:
        sep = "&" if "?" in url else "?"
        page_url = url if page == 1 else f"{url}{sep}paged={page}"
        try:
            resp = requests.get(
                page_url,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            if deep and resp.status_code == 404:
                break  # WordPress convention for "past the last page" on some sites
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                break
            new_on_page = 0
            for entry in parsed.entries:
                if require_tag and not any(
                    t.get("term") == require_tag for t in entry.get("tags", [])
                ):
                    continue
                link = entry.get("link", url)
                if link in seen_links:
                    continue
                seen_links.add(link)
                new_on_page += 1
                items.append(
                    {
                        "title": entry.get("title", "Untitled"),
                        "link": link,
                        "source": source_name,
                        "published": parse_entry_date(entry),
                    }
                )
            if deep and new_on_page == 0:
                break  # site doesn't actually support ?paged= — stop instead of looping
        except Exception as exc:
            print(f"[WARN] failed to fetch {page_url}: {exc}", file=sys.stderr)
            break
    return items


def load_existing_items():
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except FileNotFoundError:
        return []


def merge_and_cap(existing_items, new_items, max_per_source=MAX_ITEMS_PER_SOURCE):
    """Accumulate rather than overwrite: feed.json is the union of everything
    ever fetched, deduped by link, capped per source (oldest dropped first)
    so the file and git history don't grow forever. Without this, an item
    that scrolls off a source's own front page/feed window would vanish from
    feed.json the next run even though nothing about it changed."""
    by_link = {}
    for item in existing_items:
        by_link[item["link"]] = item
    for item in new_items:
        by_link[item["link"]] = item

    by_source = {}
    for item in by_link.values():
        by_source.setdefault(item["source"], []).append(item)

    capped = []
    for items in by_source.values():
        items.sort(key=lambda x: x["published"], reverse=True)
        capped.extend(items[:max_per_source])

    capped.sort(key=lambda x: x["published"], reverse=True)
    return capped


def main():
    deep = "--backfill" in sys.argv

    all_items = []
    for feed in FEEDS:
        all_items.extend(fetch_one(feed["url"], feed["source"], deep=deep))
    for feed in TAG_FILTERED_FEEDS:
        all_items.extend(
            fetch_one(feed["url"], feed["source"], deep=deep, require_tag=feed["require_tag"])
        )

    merged_items = merge_and_cap(load_existing_items(), all_items)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "items": merged_items,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Fetched {len(all_items)} items this run, {len(merged_items)} total in {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
