#!/usr/bin/env python3
"""
ดึงข้อมูล item ทั้งหมดจาก https://wlohub.com/items แล้วบันทึกเป็น CSV และ Excel

วิธีใช้:
    pip install requests beautifulsoup4 pandas openpyxl
    python scrape_wlohub_items.py

ตัวเลือก:
    --per-page 100     จำนวนรายการต่อหน้า (ค่าเริ่มต้น 100)
    --delay 1.0        หน่วงเวลาระหว่างแต่ละหน้า (วินาที)
    --out wlo_items    ชื่อไฟล์ผลลัพธ์ (ไม่ต้องใส่นามสกุล)
    --max-pages N      จำกัดจำนวนหน้า (ไว้ทดสอบ)
"""
import argparse
import re
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://wlohub.com/items"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WLO-items-exporter/1.0)",
    "Accept-Language": "en",
}


def fetch(session, url, retries=4):
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  ! HTTP {r.status_code} ที่ {url} (ลองครั้งที่ {attempt})")
        except requests.RequestException as e:
            print(f"  ! error: {e} (ลองครั้งที่ {attempt})")
        time.sleep(2 * attempt)
    return None


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_rows(html):
    """อ่านแถวของตาราง items จาก HTML หนึ่งหน้า"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        headers = [clean(th.get_text()).replace("↑", "").replace("↓", "").strip().lower()
                   for th in table.find_all("th")]
        if "name" not in headers or "type" not in headers:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < len(headers):
                continue
            cell = dict(zip(headers, tds))

            img = cell["icon"].find("img") if "icon" in cell else None
            a = cell["name"].find("a")
            href = urljoin(BASE_URL, a["href"]) if a and a.get("href") else ""
            m = re.search(r"/items/(\d+)", href)

            type_text = clean(cell["type"].get_text())
            tm = re.match(r"^(.*?)\s*(?:\((.*?)\))?$", type_text)
            item_type = tm.group(1) if tm else type_text
            subtype = tm.group(2) if tm and tm.group(2) else ""

            def g(key):
                return clean(cell[key].get_text()) if key in cell else ""

            rows.append({
                "id": int(m.group(1)) if m else None,
                "name": clean(a.get_text()) if a else g("name"),
                "level": g("level"),
                "rank": g("rank"),
                "type": item_type,
                "subtype": subtype,
                "base": g("base"),
                "attribute": g("attribute"),
                "description": g("description"),
                "icon_url": urljoin(BASE_URL, img["src"]) if img and img.get("src") else "",
                "item_url": href,
            })
        if rows:
            break
    return rows


def find_per_page_param(html, wanted):
    """หาชื่อพารามิเตอร์ของ select 'Items per page' (ตัวที่มี option 10/30/50/100)"""
    soup = BeautifulSoup(html, "html.parser")
    for sel in soup.find_all("select"):
        values = {o.get("value", clean(o.get_text())) for o in sel.find_all("option")}
        if {"10", "50", "100"} <= {v.strip() for v in values} and sel.get("name"):
            return sel["name"]
    return None


def find_next_url(html, current_url):
    """หาลิงก์หน้าถัดไปจากปุ่ม Next »"""
    soup = BeautifulSoup(html, "html.parser")
    a = soup.find("a", rel=lambda v: v and "next" in v)
    if not a:
        for cand in soup.find_all("a"):
            if "next" in clean(cand.get_text()).lower():
                a = cand
                break
    if a and a.get("href") and not a["href"].startswith("#") \
            and not a["href"].lower().startswith("javascript"):
        nxt = urljoin(current_url, a["href"])
        if nxt != current_url:
            return nxt
    return None


def with_param(url, **params):
    p = urlparse(url)
    q = parse_qs(p.query)
    for k, v in params.items():
        q[k] = [str(v)]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def total_results(html):
    m = re.search(r"of\s+([\d,]+)\s+results", html)
    return int(m.group(1).replace(",", "")) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--out", default="wlo_items")
    ap.add_argument("--max-pages", type=int, default=0)
    args = ap.parse_args()

    session = requests.Session()
    html = fetch(session, BASE_URL)
    if not html:
        sys.exit("เปิดหน้าแรกไม่ได้")

    total = total_results(html)
    print(f"จำนวนรายการทั้งหมดตามเว็บ: {total}")

    # พยายามตั้ง items per page
    start_url = BASE_URL
    pp_name = find_per_page_param(html, args.per_page)
    candidates = [pp_name] if pp_name else []
    candidates += [n for n in ("perPage", "per_page", "limit", "pageSize") if n != pp_name]
    for name in candidates:
        test_url = with_param(BASE_URL, **{name: args.per_page})
        test_html = fetch(session, test_url)
        if test_html and len(parse_rows(test_html)) >= min(args.per_page, 100) - 5:
            start_url, html = test_url, test_html
            print(f"ใช้พารามิเตอร์ {name}={args.per_page}")
            break
    else:
        print("ตั้งจำนวนต่อหน้าไม่สำเร็จ จะใช้ค่าเริ่มต้นของเว็บ (ช้ากว่าแต่ครบเท่ากัน)")

    all_rows, seen = [], set()
    url, page = start_url, 1
    while url:
        rows = parse_rows(html)
        new = 0
        for r in rows:
            key = r["id"] if r["id"] is not None else (r["name"], r["type"], r["level"])
            if key not in seen:
                seen.add(key)
                all_rows.append(r)
                new += 1
        print(f"หน้า {page}: {len(rows)} แถว (ใหม่ {new}) | รวม {len(all_rows)}")

        if new == 0 or (args.max_pages and page >= args.max_pages):
            break
        if total and len(all_rows) >= total:
            break

        nxt = find_next_url(html, url)
        if not nxt:  # วิธีสำรอง: ใช้ ?page=N
            nxt = with_param(url, page=page + 1)
        time.sleep(args.delay)
        html = fetch(session, nxt)
        if not html:
            print("ดึงหน้าถัดไปไม่สำเร็จ หยุดและบันทึกเท่าที่ได้")
            break
        url, page = nxt, page + 1

        if page % 10 == 0:  # บันทึกระหว่างทาง กันข้อมูลหาย
            pd.DataFrame(all_rows).to_csv(f"{args.out}_partial.csv", index=False, encoding="utf-8-sig")

    df = pd.DataFrame(all_rows).sort_values("id", na_position="last").reset_index(drop=True)
    df.to_csv(f"{args.out}.csv", index=False, encoding="utf-8-sig")
    df.to_excel(f"{args.out}.xlsx", index=False)
    print(f"\nเสร็จแล้ว: {len(df)} รายการ -> {args.out}.csv, {args.out}.xlsx")
    if total and len(df) != total:
        print(f"คำเตือน: ได้ {len(df)} แต่เว็บบอกว่ามี {total} ลองรันซ้ำหรือเพิ่ม --delay")


if __name__ == "__main__":
    main()
