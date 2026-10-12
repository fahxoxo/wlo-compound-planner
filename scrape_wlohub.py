#!/usr/bin/env python3
"""
Scrape items, NPC shops, compound formulas and gathering spots (collections)
from wlohub.com into ./data

Usage:
    pip install requests beautifulsoup4
    python scrape_wlohub.py                      # all three sections
    python scrape_wlohub.py --only items shops   # selected sections
    python scrape_wlohub.py --max-pages 2        # quick test

Output (data/):
    items.json      / items.csv
    shops.json      / shops.csv
    compounds.json  / compounds.csv
    collections.json / collections.csv
    meta.json       scrape time and counts
    _cache/         raw HTML per page; re-running resumes from here
                    (use --refresh to ignore the cache and download again)

Note: on wlohub "rank" is the compounding level of an item; "level" is the
character level required to equip it.
"""
import argparse
import csv
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment

SITE = "https://wlohub.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WLO-companion-scraper/1.0)",
    "Accept-Language": "en",
}
ITEM_HREF = re.compile(r"/items/(\d+)/?$")
BOOK_NAME = re.compile(r"Alchemy Book \d")


# ---------------------------------------------------------------- helpers

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def to_int(text):
    m = re.search(r"[-+]?\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def split_bases(text):
    """'Silver / Grass' -> ['Silver', 'Grass'] (first one is the primary base)."""
    text = clean(text)
    if not text or text in ("-", "—", "N/A"):
        return []
    return [b.strip() for b in text.split(" / ") if b.strip()]


def item_id_from(a):
    m = ITEM_HREF.search(a.get("href", "")) if a else None
    return int(m.group(1)) if m else None


def row_key(tr, prefix):
    m = re.match(rf"{prefix}-(\d+)$", tr.get("wire:key", ""))
    return int(m.group(1)) if m else None


def soup_of(html):
    soup = BeautifulSoup(html, "html.parser")
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    return soup


def data_rows(soup):
    table = soup.find("table")
    return [tr for tr in table.find_all("tr") if tr.find("td")] if table else []


def total_results(html):
    m = re.search(r"of\s*(?:<[^>]+>\s*)*([\d,]+)\s*(?:<[^>]+>\s*)*results", html)
    return int(m.group(1).replace(",", "")) if m else None


# ---------------------------------------------------------------- parsers

def parse_items(soup):
    out = []
    for tr in data_rows(soup):
        td = tr.find_all("td")
        if len(td) < 8:
            continue
        a = td[1].find("a")
        img = td[0].find("img")
        sub = td[4].find(class_="subtype-text")
        subtype = clean(sub.get_text()).strip("()") if sub else ""
        if sub:
            sub.extract()
        out.append({
            "id": item_id_from(a) or row_key(tr, "item"),
            "name": clean(a.get_text() if a else td[1].get_text()),
            "level": to_int(td[2].get_text()),
            "rank": to_int(td[3].get_text()),
            "type": clean(td[4].get_text()),
            "subtype": subtype,
            "bases": split_bases(td[5].get_text()),
            "attribute": clean(td[6].get_text()),
            "description": clean(td[7].get_text()),
            "icon_url": img.get("src", "") if img else "",
        })
    return out


def parse_shops(soup):
    out = []
    for tr in data_rows(soup):
        td = tr.find_all("td")
        if len(td) < 6:
            continue
        a = td[1].find("a")
        main = td[3].find(class_="type-main")
        sub = td[3].find(class_="type-sub")
        out.append({
            "id": row_key(tr, "shop"),
            "location": clean(td[0].get_text()),
            "item_id": item_id_from(a),
            "item_name": clean(td[1].get_text()),
            "rank": to_int(td[2].get_text()),
            "type": clean(main.get_text()) if main else clean(td[3].get_text()),
            "subtype": clean(sub.get_text()).strip("[]") if sub else "",
            "bases": split_bases(td[4].get_text()),
            "price": to_int(td[5].get_text()),
        })
    return out


def parse_compounds(soup):
    out = []
    for tr in data_rows(soup):
        td = tr.find_all("td")
        if len(td) < 5:
            continue
        for tip in tr.select(".wlo-tooltip"):
            # keep the verifier name before tooltips are dropped
            if tip.find_parent("td") is td[4]:
                continue
            tip.decompose()

        badge = td[0].find(class_="rpg-badge")
        if badge and badge.find(class_="rpg-icon"):
            badge.find(class_="rpg-icon").extract()

        res = td[2].find("a")
        ingredients = []
        for a in td[3].find_all("a", href=True):
            iid = item_id_from(a)
            if iid is not None:
                ingredients.append({"id": iid, "name": clean(a.get_text())})

        vcell = td[4]
        tip = vcell.find(class_="wlo-tooltip")
        verified = vcell.find("svg") is not None and tip is not None
        by = vcell.find("div", recursive=False)
        submitted_by = ""
        if by and "By:" in by.get_text():
            submitted_by = clean(by.get_text()).replace("By:", "").strip()

        books = [to_int(i["name"]) for i in ingredients if BOOK_NAME.fullmatch(i["name"])]
        materials = [i for i in ingredients if not BOOK_NAME.fullmatch(i["name"])]

        out.append({
            "id": row_key(tr, "comp"),
            "alchemy_level": clean((badge or td[0]).get_text()),
            "rank_offset": to_int(td[1].get_text()),
            "result_id": item_id_from(res),
            "result_name": clean(res.get_text()) if res else clean(td[2].get_text()),
            "materials": materials,        # slot order, Alchemy Book removed
            "book": max(books) if books else None,
            "ingredients": ingredients,    # raw list as shown on the site
            "verified": verified,
            "verified_by": clean(tip.find("strong").get_text()) if verified and tip.find("strong") else "",
            "submitted_by": submitted_by,
        })
    return out


def parse_collections(soup):
    out = []
    for tr in data_rows(soup):
        td = tr.find_all("td")
        if len(td) < 3:
            continue
        loc = td[1].find(class_="loc-name")
        via = td[1].find(class_="source-tag")
        coords = {}
        for tag in td[1].find_all(class_="coordinate-tag"):
            m = re.match(r"([XY])\s*:\s*(-?\d+)", clean(tag.get_text()))
            if m:
                coords[m.group(1).lower()] = int(m.group(2))
        items = []
        for a in td[2].find_all("a", href=True):
            iid = item_id_from(a)
            if iid is not None:
                items.append({"id": iid, "name": clean(a.get_text())})
        out.append({
            "id": row_key(tr, "col"),
            "location": clean(loc.get_text()) if loc else "",
            "x": coords.get("x"),
            "y": coords.get("y"),
            "via": clean(via.get_text()).replace("Via:", "").strip() if via else "",
            "items": items,
            "images": [img["src"] for img in td[0].find_all("img", src=True)],
        })
    return out


SECTIONS = {
    "items": parse_items,
    "shops": parse_shops,
    "compounds": parse_compounds,
    "collections": parse_collections,
}


# ---------------------------------------------------------------- fetching

class Fetcher:
    def __init__(self, cache_dir, delay, refresh):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.cache_dir = cache_dir
        self.delay = delay
        self.refresh = refresh

    def page(self, section, n, retries=5):
        path = self.cache_dir / section / f"page_{n:04d}.html"
        if path.exists() and not self.refresh:
            return path.read_text(encoding="utf-8")
        url = f"{SITE}/{section}?page={n}"
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, timeout=30)
                if r.status_code == 200 and "<table" in r.text:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(r.text, encoding="utf-8")
                    time.sleep(self.delay)
                    return r.text
                print(f"  ! HTTP {r.status_code} on {url} (attempt {attempt})")
            except requests.RequestException as e:
                print(f"  ! {e} (attempt {attempt})")
            time.sleep(3 * attempt)
        return None


def scrape_section(fetcher, section, max_pages):
    parse = SECTIONS[section]
    first = fetcher.page(section, 1)
    if first is None:
        raise RuntimeError(f"cannot load {SITE}/{section}")
    total = total_results(first)
    rows = parse(soup_of(first))
    per_page = len(rows) or 1
    pages = math.ceil(total / per_page) if total else 10**6
    if max_pages:
        pages = min(pages, max_pages)
    print(f"[{section}] {total} results, {per_page}/page, {pages if pages < 10**6 else '?'} pages")

    by_id = {r["id"]: r for r in rows}
    n = 1
    while n < pages:
        n += 1
        html = fetcher.page(section, n)
        if html is None:
            print(f"[{section}] page {n} failed; stopping (re-run to resume)")
            break
        got = parse(soup_of(html))
        if not got:
            break
        for r in got:
            by_id[r["id"]] = r
        if n % 25 == 0 or n == pages:
            print(f"[{section}] page {n}/{pages}  collected {len(by_id)}")

    result = sorted(by_id.values(), key=lambda r: (r["id"] is None, r["id"] or 0))
    if total and not max_pages and len(result) != total:
        print(f"[{section}] WARNING: got {len(result)} of {total}. "
              f"Listing order may have shifted; run again with --refresh {section}.")
    return result, total


# ---------------------------------------------------------------- output

def write_csv(path, section, rows):
    if section == "compounds":
        slots = max((len(r["materials"]) for r in rows), default=0)
        flat = []
        for r in rows:
            f = {k: v for k, v in r.items() if k not in ("ingredients", "materials")}
            for i in range(slots):
                ing = r["materials"][i] if i < len(r["materials"]) else {}
                f[f"slot{i + 1}_id"] = ing.get("id", "")
                f[f"slot{i + 1}_name"] = ing.get("name", "")
            flat.append(f)
    elif section == "collections":
        flat = [{**r,
                 "items": " | ".join(i["name"] for i in r["items"]),
                 "item_ids": " | ".join(str(i["id"]) for i in r["items"]),
                 "images": " | ".join(r["images"])} for r in rows]
    else:
        flat = [{**r, "bases": " / ".join(r["bases"])} for r in rows]
    if not flat:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", choices=list(SECTIONS), default=list(SECTIONS))
    ap.add_argument("--out", default="data", help="output folder (default: data)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--max-pages", type=int, default=0, help="limit pages per section (testing)")
    ap.add_argument("--refresh", action="store_true", help="ignore cached pages")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(out / "_cache", args.delay, args.refresh)

    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    for section in args.only:
        rows, total = scrape_section(fetcher, section, args.max_pages)
        (out / f"{section}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        write_csv(out / f"{section}.csv", section, rows)
        meta[section] = {
            "count": len(rows),
            "site_total": total,
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": f"{SITE}/{section}",
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"[{section}] saved {len(rows)} rows -> {out / section}.json / .csv\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Re-run the same command to resume from the cache.")
