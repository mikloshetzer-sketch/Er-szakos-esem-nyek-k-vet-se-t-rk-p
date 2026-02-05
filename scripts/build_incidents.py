#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Kulcsszavak: rövid, de értelmes. (A túl hosszú OR lista hibát okozhat GDELT-nél.)
# Fontos: OR csoportot zárójelbe tesszük.
QUERY = '(terror OR terrorism OR "bomb attack" OR bombing OR "suicide attack" OR "mass shooting" OR "gun attack" OR assassination OR "IED")'

MAXRECORDS = 250   # GDELT DOC API limitel, 250 biztonságos
PAGES_PER_MONTH = 2  # 2*250/hó -> kb. 6000/év elméletben, de mi később limitálunk
HARD_CAP_PER_YEAR = 1000  # a térképen kezelhető maradjon

def http_get(url: str, timeout=30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "osint-map/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def gdelt_doc_url(start_dt: str, end_dt: str, offset: int) -> str:
    params = {
        "query": QUERY,
        "mode": "ArtList",
        "format": "json",
        "startdatetime": start_dt,
        "enddatetime": end_dt,
        "maxrecords": str(MAXRECORDS),
        "startrecord": str(offset),
        "sort": "datedesc",
    }
    return GDELT_DOC_ENDPOINT + "?" + urllib.parse.urlencode(params)

def pick_best_location(article: dict):
    """
    GDELT DOC API-nél a geokódolt hely gyakran a 'location' vagy 'locations' mezőkben jelenik meg.
    Itt több variációt is kezelünk.
    """
    # 1) location (egyes)
    loc = article.get("location")
    if isinstance(loc, dict):
        lat = loc.get("lat")
        lon = loc.get("lon")
        name = loc.get("name") or loc.get("fullName") or loc.get("adm1") or ""
        if lat is not None and lon is not None:
            return float(lat), float(lon), str(name).strip()

    # 2) locations (lista) – válasszuk az első értelmeset
    locs = article.get("locations")
    if isinstance(locs, list):
        for l in locs:
            if not isinstance(l, dict):
                continue
            lat = l.get("lat")
            lon = l.get("lon")
            name = l.get("name") or l.get("fullName") or l.get("adm1") or ""
            if lat is not None and lon is not None:
                return float(lat), float(lon), str(name).strip()

    return None

def parse_articles(payload: dict):
    arts = payload.get("articles")
    if not isinstance(arts, list):
        return []
    out = []
    for a in arts:
        if not isinstance(a, dict):
            continue

        title = a.get("title") or ""
        url = a.get("url") or ""
        seendate = a.get("seendate") or a.get("date") or ""  # seendate jellemző
        domain = a.get("domain") or a.get("sourceCommonName") or a.get("source") or ""
        loc = pick_best_location(a)
        if not loc:
            continue
        lat, lon, locname = loc

        out.append({
            "title": title.strip(),
            "date": str(seendate).strip(),
            "year": int(str(seendate)[:4]) if seendate else None,
            "lat": lat,
            "lon": lon,
            "location": locname or "",
            "source": domain.strip() or "",
            "url": url.strip() or ""
        })
    return out

def month_range(year: int, month: int):
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    # GDELT formátum: YYYYMMDDhhmmss
    start_dt = start.strftime("%Y%m%d%H%M%S")
    end_dt = (end.strftime("%Y%m%d%H%M%S"))
    return start_dt, end_dt

def build_for_year(year: int):
    records = []
    for m in range(1, 13):
        start_dt, end_dt = month_range(year, m)

        # lapozás (startrecord)
        offset = 1
        for _page in range(PAGES_PER_MONTH):
            url = gdelt_doc_url(start_dt, end_dt, offset)
            txt = http_get(url)
            try:
                payload = json.loads(txt)
            except Exception:
                # Debug: mentsük az első 300 karaktert
                raise RuntimeError(f"JSON parse error for {year}-{m:02d}. Head: {txt[:300]}")

            new = parse_articles(payload)
            records.extend(new)

            if len(records) >= HARD_CAP_PER_YEAR:
                return records[:HARD_CAP_PER_YEAR]

            # ha nincs több, lépjünk tovább
            if len(new) < MAXRECORDS:
                break

            offset += MAXRECORDS
            time.sleep(0.25)
        time.sleep(0.25)

    return records[:HARD_CAP_PER_YEAR]

def dedupe(records):
    seen = set()
    out = []
    for r in records:
        key = (r.get("url",""), r.get("title",""), r.get("date",""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

def main():
    now = datetime.utcnow()
    years = sorted({now.year, now.year - 1})  # pl. 2026 és 2025
    all_records = []
    per_year_counts = {}

    for y in years:
        try:
            recs = build_for_year(y)
        except Exception as e:
            # ha a "current year" üres/hibás, attól a másik még működjön
            print(f"[WARN] Year {y} failed: {e}", file=sys.stderr)
            recs = []
        recs = dedupe(recs)
        per_year_counts[y] = len(recs)
        all_records.extend(recs)

    all_records = dedupe(all_records)

    # írjuk ki incidents.json-t
    with open("incidents.json", "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    # debug fájl
    with open("gdelt_debug.txt", "w", encoding="utf-8") as f:
        f.write("GDELT DOC API build debug\n")
        f.write(f"UTC now: {now.isoformat()}Z\n")
        f.write(f"Query: {QUERY}\n")
        f.write(f"Years attempted: {years}\n")
        f.write("Counts:\n")
        for y in years:
            f.write(f"  {y}: {per_year_counts.get(y,0)} records\n")
        f.write("\nNotes:\n")
        f.write("- incidents.json only includes articles that have usable lat/lon in DOC API response.\n")
        f.write("- If 2026 is 0, it can mean: no geocoded matches yet, or GDELT returns results without coordinates.\n")

    print("[OK] incidents.json written.")
    for y in years:
        print(f"[OK] {y}: {per_year_counts.get(y,0)}")

if __name__ == "__main__":
    main()
