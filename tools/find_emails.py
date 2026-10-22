#!/usr/bin/env python3
"""Standalone script - NOT part of the deployed app, run locally by a GA - that looks for
each SSB directory person's public stevens.edu email, so an admin can review and approve
candidates on the admin page before any of it reaches the live directory. Never invents an
email from a name pattern: every candidate comes from text actually found on a real page,
with a confidence level, and a page that can't be found or doesn't mention the person
produces nothing rather than a guess.

Polite by design: checks robots.txt before fetching, waits at least 2 seconds between
requests, uses a clear identifying User-Agent, and caches every fetched page under
tools/.cache/ so a re-run resumes instead of re-fetching (gitignored, like the directory
CSVs - delete it to force a fresh crawl).

Sources (deliberately NOT guessed - see README/TASKS for how these were found):
  Tier 1: the one real SSB faculty listing page already has name + title + office + phone
          + a Cloudflare-obfuscated email for every Faculty row, in one page - no profile
          page visit needed. Matched against directory rows where type == "Faculty".
  Tier 2: tools/known_profile_urls.csv (name,url), optional and gitignored - a short,
          hand-filled list for Staff/PhD-student rows that have no public listing page at
          all. Each URL is actually fetched and the person's name must appear on that page
          before an email found there counts as a candidate.
  Tier 3: nobody. If a person isn't on the Tier 1 listing and has no Tier 2 URL, this script
          produces nothing for them rather than guessing a profile URL or an address.

Usage:  python3 tools/find_emails.py
        python3 tools/find_emails.py --directory data/ssb_directory.csv --out data/email_candidates.csv
"""
import argparse
import csv
import hashlib
import re
import sys
import time
import urllib.robotparser
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin

import requests

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CACHE_DIR = HERE / ".cache"
USER_AGENT = "VirtualGA-EmailFinder/1.0 (+contact: jpatel109@stevens.edu; one-time directory lookup)"
BASE_URL = "https://www.stevens.edu"
FACULTY_LISTING_URL = f"{BASE_URL}/school-business/faculty"
CRAWL_DELAY_SECONDS = 2.0
ALLOWED_EMAIL_DOMAIN = "@stevens.edu"

_last_fetch_at = 0.0
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


# ---------- polite fetching: robots.txt, rate limit, local cache ----------

def _robots_allows(url: str) -> bool:
    origin = f"{url.split('://')[0]}://{url.split('://')[1].split('/')[0]}"
    rp = _robots_cache.get(origin)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urljoin(origin, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            rp = None  # couldn't read robots.txt; fail open rather than block forever
        _robots_cache[origin] = rp
    return rp.can_fetch(USER_AGENT, url) if rp else True


def fetch(url: str) -> str | None:
    """Returns page text, using a local cache so a re-run is resumable and doesn't
    re-fetch pages it already has."""
    global _last_fetch_at
    cache_path = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".html")
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    if not _robots_allows(url):
        print(f"  robots.txt disallows fetching {url}, skipping", file=sys.stderr)
        return None
    elapsed = time.time() - _last_fetch_at
    if elapsed < CRAWL_DELAY_SECONDS:
        time.sleep(CRAWL_DELAY_SECONDS - elapsed)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        _last_fetch_at = time.time()
    except Exception as e:
        print(f"  fetch failed for {url}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  {url} returned HTTP {r.status_code}, skipping", file=sys.stderr)
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(r.text, encoding="utf-8")
    return r.text


# ---------- Cloudflare email-protection decode ----------

CF_HEX_RE = re.compile(r'(?:cdn-cgi/l/email-protection#|data-cfemail=")([0-9a-fA-F]+)')
MAILTO_RE = re.compile(r'href="mailto:([^"?]+)')


def cf_decode(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
        key = data[0]
        return "".join(chr(b ^ key) for b in data[1:])
    except Exception:
        return None


def extract_emails(html: str) -> list[str]:
    """Every @stevens.edu address findable on a page, decoding both the href-fragment and
    data-cfemail forms of Cloudflare's obfuscation (both confirmed present on the real
    site). Never returns a guessed address - only ones actually decoded from the page."""
    found = set()
    for m in CF_HEX_RE.finditer(html):
        decoded = cf_decode(m.group(1))
        if decoded:
            found.add(decoded)
    for m in MAILTO_RE.finditer(html):
        found.add(m.group(1))
    return [e for e in found if e.lower().endswith(ALLOWED_EMAIL_DOMAIN)]


# ---------- name matching (duplicated from app/directory.py on purpose: this script must
# stay standalone, runnable with no import of the web app or its dependencies) ----------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


def _name_ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def page_mentions_name(html: str, name: str) -> bool:
    text = re.sub(r"<[^>]+>", " ", html)
    return _norm(name) in _norm(text) or _name_ratio(name, text[:200]) >= 0.5


# ---------- Tier 1: the faculty listing page ----------

CARD_SPLIT_RE = re.compile(r'<div class="faculty-card_cc--faculty-card__\w+">')
NAME_RE = re.compile(r'<h3><a href="(/profile/[^"]+)">([^<]+)</a></h3>')
TITLE_RE = re.compile(r'<h4>([^<]*)</h4>')
OFFICE_RE = re.compile(r'faculty-card_contact-office__\w+">([^<]*)</')
PHONE_RE = re.compile(r'tel:\+1(\d{10})')


def parse_faculty_listing(html: str) -> list[dict]:
    cards = []
    for chunk in CARD_SPLIT_RE.split(html)[1:]:
        name_m = NAME_RE.search(chunk)
        if not name_m:
            continue
        profile_url, name = name_m.group(1), name_m.group(2).strip()
        title_m, office_m, phone_m = TITLE_RE.search(chunk), OFFICE_RE.search(chunk), PHONE_RE.search(chunk)
        emails = extract_emails(chunk)
        cards.append({
            "name": name,
            "profile_url": urljoin(BASE_URL, profile_url),
            "title": title_m.group(1).strip() if title_m else "",
            "office": office_m.group(1).strip() if office_m else "",
            "extension": phone_m.group(1)[-4:] if phone_m else "",
            "email": emails[0] if emails else None,
        })
    return cards


# ---------- directory + known-URL CSV I/O ----------

def load_directory(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_known_urls(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {row["name"].strip(): row["url"].strip() for row in csv.DictReader(f) if row.get("name")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--directory", default=str(REPO_ROOT / "data" / "ssb_directory.csv"))
    ap.add_argument("--known", default=str(HERE / "known_profile_urls.csv"))
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "email_candidates.csv"))
    args = ap.parse_args()

    directory_path = Path(args.directory)
    if not directory_path.exists():
        print(f"Directory CSV not found: {directory_path}", file=sys.stderr)
        sys.exit(1)
    people = load_directory(directory_path)
    known_urls = load_known_urls(Path(args.known))
    print(f"Loaded {len(people)} directory rows, {len(known_urls)} known profile URLs.")

    results: list[dict] = []
    matched_names: set[str] = set()

    print(f"Fetching {FACULTY_LISTING_URL} ...")
    listing_html = fetch(FACULTY_LISTING_URL)
    cards = parse_faculty_listing(listing_html) if listing_html else []
    print(f"Found {len(cards)} faculty cards on the listing page.")

    for person in people:
        if person.get("type") != "Faculty":
            continue
        name = person["name"].strip()
        best_card, best_score = None, 0.0
        for card in cards:
            score = _name_ratio(name, card["name"])
            if score > best_score:
                best_card, best_score = card, score
        if best_card and best_score >= 0.85 and best_card["email"]:
            mismatches = []
            if best_card["office"] and person.get("room") and person["room"] not in best_card["office"]:
                mismatches.append(f"office: CSV={person['room']!r} page={best_card['office']!r}")
            if best_card["extension"] and person.get("extension") and best_card["extension"] != person["extension"]:
                mismatches.append(f"extension: CSV={person['extension']!r} page={best_card['extension']!r}")
            results.append({
                "name": name, "email": best_card["email"], "confidence": "high",
                "source_url": FACULTY_LISTING_URL, "mismatches": "; ".join(mismatches),
            })
            matched_names.add(name)
            print(f"  [tier 1, high] {name} -> {best_card['email']}")

    for person in people:
        name = person["name"].strip()
        if name in matched_names or name not in known_urls:
            continue
        url = known_urls[name]
        print(f"Fetching {url} for {name} (tier 2) ...")
        html = fetch(url)
        if not html:
            continue
        if not page_mentions_name(html, name):
            print(f"  page doesn't clearly mention {name!r}, skipping (never guessing)")
            continue
        emails = extract_emails(html)
        if not emails:
            print(f"  no @stevens.edu email found on {url}")
            continue
        results.append({"name": name, "email": emails[0], "confidence": "medium",
                        "source_url": url, "mismatches": ""})
        print(f"  [tier 2, medium] {name} -> {emails[0]}")

    unmatched = len(people) - len(results)
    print(f"\n{len(results)} candidates found, {unmatched} directory rows with no source "
          f"(no listing-page match and no known_profile_urls.csv entry).")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "email", "confidence", "source_url", "mismatches"])
        w.writeheader()
        w.writerows(results)
    print(f"Wrote {out_path}. Review and approve candidates on the admin page before they "
          f"reach the live directory.")


if __name__ == "__main__":
    main()
