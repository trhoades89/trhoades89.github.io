#!/usr/bin/env python3
"""Build the data files behind /schools/ from official open data.

Sources (all Open Government Licence v3.0):
  * Get Information about Schools (GIAS): every establishment, location and characteristics.
  * Ofsted "State-funded school inspections and outcomes: management information" (latest inspections).
  * DfE school performance tables ("Compare school and college performance"): KS2, KS4 and 16-18.
  * DfE "Primary and secondary school applications and offers": school-level preferences and offers.

Outputs:
  schools/data/schools.json      compact table of every open school, used to draw the map
  schools/data/la/<LA code>.json per-school detail, loaded when a school is opened

Run:  python3 scripts/build_school_data.py [--cache DIR]
Each source is optional apart from GIAS: if one fails to download, the build continues without it
and says so, so a single upstream change can't take the whole map down.
"""

import argparse
import collections
import csv
import datetime as dt
import http.cookiejar
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "schools", "data")

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/json,text/csv,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}

GIAS_URL = "https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/edubasealldata{d}.csv"
OFSTED_PAGE = "government/statistical-data-sets/monthly-management-information-ofsteds-school-inspections-outcomes"
CSP_URL = ("https://www.compare-school-performance.service.gov.uk/download-data?download=true&regions=0"
           "&filters={f}&fileformat=csv&year={y}&meta=false")
ADMISSIONS_SLUG = "primary-and-secondary-school-applications-and-offers"

MISSING = {"", "NULL", "NA", "NE", "NP", "SUPP", "LOWCOV", "NEW", "x", "z", "c", "u", "..", ":", "n/a", "DNS"}


# ---------------------------------------------------------------- helpers

def log(*a):
    print(*a, flush=True)


class Fetcher:
    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def get(self, url, tries=3, timeout=300):
        key = None
        if self.cache_dir:
            key = os.path.join(self.cache_dir, re.sub(r"[^A-Za-z0-9._-]+", "_", url)[-180:])
            if os.path.exists(key):
                with open(key, "rb") as f:
                    return f.read()
        last = None
        for attempt in range(tries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with self.opener.open(req, timeout=timeout) as r:
                    data = r.read()
                if key:
                    with open(key, "wb") as f:
                        f.write(data)
                return data
            except urllib.error.HTTPError as e:
                if e.code in (400, 403, 404, 410):
                    raise
                last = e
            except Exception as e:  # network blips
                last = e
            time.sleep(2 ** attempt * 2)
        raise last


def decode(raw):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_csv(raw, header_contains=None):
    """Return list of dict rows; skips title rows above the real header if header_contains given."""
    rows = list(csv.reader(io.StringIO(decode(raw))))
    start = 0
    if header_contains:
        while start < len(rows) and header_contains not in rows[start]:
            start += 1
        if start == len(rows):
            raise ValueError(f"header containing {header_contains!r} not found")
    hdr = [h.strip() for h in rows[start]]
    return [dict(zip(hdr, r)) for r in rows[start + 1:] if any(c.strip() for c in r)], hdr


def num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "")
    if v in MISSING:
        return None
    pct = v.endswith("%")
    v = v.rstrip("%")
    try:
        x = float(v)
    except ValueError:
        return None
    if math.isnan(x):
        return None
    return x


def rnd(x, dp=1):
    if x is None:
        return None
    r = round(x, dp)
    return int(r) if dp == 0 or r == int(r) else r


def clean(v):
    v = (v or "").strip()
    return None if v in MISSING or v in ("Not applicable", "Does not apply", "Unknown", "Not recorded") else v


def iso(d):
    """dd/mm/yyyy or dd-mm-yyyy -> yyyy-mm-dd."""
    d = (d or "").strip()
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", d)
    if not m:
        return None
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"


def col(hdr, *patterns, required=False):
    """Find the first header matching any regex (case-insensitive)."""
    for p in patterns:
        rx = re.compile(p, re.I)
        for h in hdr:
            if rx.search(h):
                return h
    if required:
        raise KeyError(f"no column matching {patterns}")
    return None


def academic_years(n=4):
    today = dt.date.today()
    start = today.year if today.month >= 9 else today.year - 1
    return [f"{y}-{y + 1}" for y in range(start, start - n, -1)]


# ---------------------------------------------------------------- GIAS

def to_wgs84():
    from pyproj import Transformer
    return Transformer.from_crs(27700, 4326, always_xy=True)


PHASE_MAP = {
    "Nursery": "N", "Primary": "P", "Middle deemed primary": "P", "Secondary": "S",
    "Middle deemed secondary": "S", "All-through": "A", "16 plus": "C",
}
AP_TYPES = re.compile(r"pupil referral|alternative provision", re.I)
SPECIAL_TYPES = re.compile(r"special", re.I)
KEEP_GROUPS = {
    "Local authority maintained schools", "Academies", "Free Schools", "Independent schools",
    "Special schools", "Colleges",
}


def infer_phase(row, typ, group):
    if AP_TYPES.search(typ):
        return "R"
    if SPECIAL_TYPES.search(typ) or group == "Special schools":
        return "X"
    ph = PHASE_MAP.get(row.get("PhaseOfEducation (name)", ""))
    if ph:
        return ph
    if group == "Colleges":
        return "C"
    lo, hi = num(row.get("StatutoryLowAge")), num(row.get("StatutoryHighAge"))
    if lo is None or hi is None:
        return "P"
    if lo >= 15:
        return "C"
    if lo <= 7 and hi >= 15:
        return "A"
    if hi <= 13:
        return "N" if hi <= 5 else "P"
    return "S"


def sector_of(typ, group):
    if group in ("Academies", "Free Schools"):
        return "a"
    if group == "Independent schools" or typ == "Non-maintained special school":
        return "i"
    if group == "Colleges":
        return "c"
    return "m"


def load_gias(fx):
    raw, used = None, None
    for i in range(0, 14):
        d = (dt.date.today() - dt.timedelta(days=i)).strftime("%Y%m%d")
        try:
            raw = fx.get(GIAS_URL.format(d=d))
            used = d
            break
        except Exception as e:
            log("  GIAS", d, "not available:", e)
    if raw is None:
        raise SystemExit("Could not download GIAS extract for the last 14 days")
    rows, _ = read_csv(raw)
    log(f"GIAS {used}: {len(rows)} establishments")
    tf = to_wgs84()
    schools = {}
    skipped = collections.Counter()
    for r in rows:
        status = r.get("EstablishmentStatus (name)", "")
        if not status.startswith("Open"):
            continue
        group = r.get("EstablishmentTypeGroup (name)", "")
        typ = r.get("TypeOfEstablishment (name)", "")
        if group not in KEEP_GROUPS:
            skipped[group] += 1
            continue
        if group == "Colleges" and not re.search(r"sixth form|further education", typ, re.I):
            skipped[typ] += 1
            continue
        e, n = num(r.get("Easting")), num(r.get("Northing"))
        if not e or not n:
            skipped["no location"] += 1
            continue
        lng, lat = tf.transform(e, n)
        urn = int(r["URN"])
        schools[urn] = {"row": r, "lat": round(lat, 5), "lng": round(lng, 5), "typ": typ, "group": group,
                        "ph": infer_phase(r, typ, group), "sec": sector_of(typ, group)}
    log(f"  kept {len(schools)} open schools; skipped {dict(skipped.most_common(8))}")
    return schools, {"name": "Get Information about Schools (DfE)", "edition": f"extract {used[:4]}-{used[4:6]}-{used[6:]}",
                     "url": "https://get-information-schools.service.gov.uk/Downloads"}


# ---------------------------------------------------------------- Ofsted

OEIF_GRADE = {"1": "Outstanding", "2": "Good", "3": "Requires improvement", "4": "Inadequate"}
CONCERN = {"SM": "Special measures", "SWK": "Serious weaknesses", "RSI": "Requires significant improvement"}
REPORT_CARD_AREAS = [
    ("Inclusion", r"^Inclusion$"),
    ("Curriculum and teaching", r"^Curriculum and teaching$"),
    ("Achievement", r"^Achievement$"),
    ("Attendance and behaviour", r"^Attendance and behaviour$"),
    ("Personal development and wellbeing", r"^Personal development and well-?being$"),
    ("Early years", r"^Early years( \(where applicable\))?$"),
    ("Post-16 provision", r"^(Post-16 provision|Sixth form)( \(where applicable\))?$"),
    ("Leadership and governance", r"^Leadership and governance$"),
]
OEIF_SUBS = [
    ("Quality of education", r"^Latest OEIF quality of education"),
    ("Behaviour and attitudes", r"^Latest OEIF behaviour and attitudes"),
    ("Personal development", r"^Latest OEIF personal development"),
    ("Leadership and management", r"^Latest OEIF effectiveness of leadership"),
    ("Early years", r"^Latest OEIF early years"),
    ("Sixth form", r"^Latest OEIF sixth form"),
]


def ofsted_attachment(fx, on_or_before=None):
    """Newest 'latest inspections' CSV, optionally no later than a given date."""
    j = json.loads(fx.get("https://www.gov.uk/api/content/" + OFSTED_PAGE))
    best = None
    for a in j["details"]["attachments"]:
        t = a.get("title", "")
        if "latest inspections" not in t.lower() or not a.get("url", "").lower().endswith(".csv"):
            continue
        m = re.search(r"(\d{1,2}) (\w+) (\d{4})", t)
        if not m:
            continue
        try:
            d = dt.datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y").date()
        except ValueError:
            continue
        if on_or_before and d > on_or_before:
            continue
        if best is None or d > best[0]:
            best = (d, a)
    if not best:
        raise RuntimeError("no 'latest inspections' CSV attachment found")
    return best


def load_historic_grades(fx):
    """Latest overall effectiveness per URN from before single-word grades were dropped (Sept 2024).

    The current MI file only carries graded judgements made under the 2019 framework, so schools last
    graded earlier (and since only visited for ungraded inspections) would otherwise show no grade.
    """
    d, att = ofsted_attachment(fx, on_or_before=dt.date(2024, 8, 31))
    rows, hdr = read_csv(fx.get(att["url"]), header_contains="URN")
    c_oe = col(hdr, r"^Overall effectiveness$", required=True)
    c_date = col(hdr, r"^Inspection start date$", required=True)
    out = {}
    for r in rows:
        try:
            urn = int(r["URN"])
        except (KeyError, ValueError):
            continue
        oe = (r.get(c_oe) or "").strip()
        if oe in OEIF_GRADE:
            out[urn] = (oe, iso(r.get(c_date)))
    log(f"  historic grades from MI as at {d}: {len(out)} schools")
    return out


def load_ofsted(fx):
    d, att = ofsted_attachment(fx)
    rows, hdr = read_csv(fx.get(att["url"]), header_contains="URN")
    log(f"Ofsted MI as at {d}: {len(rows)} schools, {len(hdr)} columns")

    c_start = col(hdr, r"^Inspection start date$")
    c_pub = col(hdr, r"^Publication date$")
    c_sg = col(hdr, r"^Safeguarding standards$")
    c_coc = col(hdr, r"^Category of concern$")
    rc_cols = [(label, col(hdr, rx)) for label, rx in REPORT_CARD_AREAS]
    c_oe = col(hdr, r"^Latest OEIF overall effectiveness", r"^Overall effectiveness$")
    c_oe_date = col(hdr, r"^Inspection start date of latest OEIF", r"^Inspection start date$")
    c_oe_rel = col(hdr, r"^Does the latest OEIF graded inspection relate")
    c_oe_sg = col(hdr, r"^Latest OEIF\s+safeguarding")
    c_oe_coc = col(hdr, r"^Latest OEIF category of concern")
    sub_cols = [(label, col(hdr, rx)) for label, rx in OEIF_SUBS]
    c_ung_date = col(hdr, r"^Date of latest ungraded inspection")
    c_ung_out = col(hdr, r"^Ungraded inspection overall outcome")
    missing = [n for n, c in [("report card start", c_start), ("OEIF overall", c_oe), ("ungraded outcome", c_ung_out)] if not c]
    if missing:
        log("  WARNING Ofsted columns not found:", missing)

    try:
        historic = load_historic_grades(fx)
    except Exception as e:
        log(f"  WARNING historic Ofsted grades unavailable: {type(e).__name__}: {e}")
        historic = {}

    out = {}
    counts = collections.Counter()
    for r in rows:
        try:
            urn = int(r["URN"])
        except (KeyError, ValueError):
            continue
        rec = {}
        headline = 0
        # Report card (renewed framework, from November 2025)
        rc_date = iso(r.get(c_start)) if c_start else None
        grades = [(label, clean(r.get(c))) for label, c in rc_cols if c]
        grades = [(k, v) for k, v in grades if v]
        if rc_date and grades:
            if c_sg and clean(r.get(c_sg)):
                grades.append(("Safeguarding standards", clean(r.get(c_sg))))
            rc = {"date": rc_date, "grades": grades}
            if c_pub and iso(r.get(c_pub)):
                rc["pub"] = iso(r.get(c_pub))
            coc = CONCERN.get((r.get(c_coc) or "").strip()) if c_coc else None
            if coc:
                rc["note"] = f"Category of concern: {coc}."
            rec["rc"] = rc
            headline = 5
        # Last graded inspection under the previous framework (OEIF)
        oe_raw = (r.get(c_oe) or "").strip() if c_oe else ""
        oe_date = iso(r.get(c_oe_date)) if c_oe_date else None
        if oe_raw in OEIF_GRADE or oe_raw.lower() == "not judged":
            rec["date"] = oe_date
            rec["oe"] = OEIF_GRADE.get(oe_raw)
            if rec["oe"]:
                rec["oeDate"] = oe_date
            subs = []
            for label, c in sub_cols:
                v = (r.get(c) or "").strip() if c else ""
                if v in OEIF_GRADE:
                    subs.append([label, OEIF_GRADE[v]])
            if c_oe_sg and clean(r.get(c_oe_sg)):
                subs.append(["Safeguarding effective", clean(r.get(c_oe_sg))])
            rec["sub"] = subs
            notes = []
            if c_oe_rel and (r.get(c_oe_rel) or "").strip() == "No":
                notes.append("This graded inspection was of the school's predecessor (before it became an academy or changed form).")
            coc = CONCERN.get((r.get(c_oe_coc) or "").strip()) if c_oe_coc else None
            if coc and "rc" not in rec:
                notes.append(f"Category of concern: {coc}.")
            if notes:
                rec["note"] = " ".join(notes)
            if headline == 0:
                headline = int(oe_raw) if oe_raw in OEIF_GRADE else 5
        # Graded before the 2019 framework: fall back to the last grade in the pre-September-2024 file.
        if "date" not in rec and urn in historic and historic[urn][1]:
            oe_raw, oe_date = historic[urn]
            rec.update({"date": oe_date, "oe": OEIF_GRADE[oe_raw], "oeDate": oe_date, "sub": []})
            if headline == 0:
                headline = int(oe_raw)
        # Latest ungraded inspection
        if c_ung_date and iso(r.get(c_ung_date)):
            rec["ung"] = {"date": iso(r.get(c_ung_date)), "outcome": clean(r.get(c_ung_out)) if c_ung_out else None}
        if rec:
            rec["h"] = headline
            out[urn] = rec
            counts[headline] += 1
    log(f"  headline counts: {dict(sorted(counts.items()))}  (5 = newer inspection without single grade)")
    return out, {"name": "Ofsted state-funded schools inspections and outcomes (management information)",
                 "edition": f"as at {d:%-d %B %Y}", "url": "https://www.gov.uk/" + OFSTED_PAGE}


# ---------------------------------------------------------------- Performance tables

KS2_MEASURES = [
    # label, column, unit, bar max
    ("Reading, writing & maths: expected standard", "PTRWM_EXP", "%", 100),
    ("Reading, writing & maths: higher standard", "PTRWM_HIGH", "%", 100),
    ("Reading: average scaled score", "READ_AVERAGE", "", None),
    ("Maths: average scaled score", "MAT_AVERAGE", "", None),
    ("Grammar, punctuation & spelling: average score", "GPS_AVERAGE", "", None),
    ("Reading progress score", "READPROG", "", None),
    ("Writing progress score", "WRITPROG", "", None),
    ("Maths progress score", "MATPROG", "", None),
]
KS4_MEASURES = [
    ("Attainment 8 score", "ATT8SCR", "", 90),
    ("Progress 8 score", "P8MEA", "", None),
    ("Grade 5+ in English & maths", "PTL2BASICS_95", "%", 100),
    ("Grade 4+ in English & maths", "PTL2BASICS_94", "%", 100),
    ("Entering the EBacc", "PTEBACC_E_PTQ_EE", "%", 100),
    ("EBacc average point score", "EBACCAPS", "", None),
    ("Attainment 8 (previous year)", "ATT8SCR_PREV", "", None),
]
KS5_MEASURES = [
    ("A level: average points per entry", "TALLPPE_ALEV_1618", "", 60),
    ("A level: average grade", "TALLPPEGRD_ALEV_1618", "grade", None),
    ("AAB+ incl. 2 facilitating subjects", "PTAAB_2FAC", "%", 100),
    ("Applied general: average grade", "TALLPPEGRD_AGEN", "grade", None),
    ("A level students", "TALLPUP_ALEV_1618", "count", None),
]
KS_CONFIG = {
    "ks2": ("KS2", KS2_MEASURES, ("TELIG",), "PTRWM_EXP"),
    "ks4": ("KS4", KS4_MEASURES, ("TPUP",), "ATT8SCR"),
    "ks5": ("KS5", KS5_MEASURES, ("TPUP1618",), "TALLPPE_ALEV_1618"),
}


def load_performance(fx, key):
    filt, measures, cohort_cols, headline = KS_CONFIG[key]
    for y in academic_years():
        try:
            raw = fx.get(CSP_URL.format(f=filt, y=y))
        except Exception as e:
            log(f"  {filt} {y}: {e}")
            continue
        if b"URN" not in raw[:2000]:
            log(f"  {filt} {y}: not a data file")
            continue
        rows, hdr = read_csv(raw)
        if len(rows) < 100:
            continue
        log(f"{filt} {y}: {len(rows)} rows")
        present = [m for m in measures if m[1] in hdr]
        # National reference rows have no URN; prefer the state-funded England row if present.
        nat_rows = [r for r in rows if not (r.get("URN") or "").strip().isdigit()]
        nat = {}
        for r in nat_rows:
            if (r.get("RECTYPE") or "").strip() != "4":  # 4 = local authority rows; too many to log
                lab = " ".join((r.get(c) or "") for c in ("RECTYPE", "LEA", "SCHNAME", "ALPHAIND"))
                log(f"    reference row: {lab.strip()[:90]!r} {headline}={r.get(headline)}")
        pick = None
        for r in nat_rows:
            if (r.get("RECTYPE") or "").strip() == "7":
                pick = r
        if pick is None:
            for r in nat_rows:
                if (r.get("RECTYPE") or "").strip() == "5":
                    pick = r
        if pick is not None:
            for label, c, unit, mx in present:
                if unit not in ("grade", "count"):
                    nat[c] = rnd(num(pick.get(c)), 1)
        out = {}
        for r in rows:
            u = (r.get("URN") or "").strip()
            if not u.isdigit():
                continue
            ms = []
            for label, c, unit, mx in present:
                raw_v = (r.get(c) or "").strip()
                if unit == "grade":
                    v = raw_v if raw_v and raw_v not in MISSING else None
                else:
                    v = rnd(num(raw_v), 1)
                if v is None:
                    continue
                ms.append([label, v, nat.get(c), mx, "%" if unit == "%" else ""])
            if not ms:
                continue
            cohort = None
            for c in cohort_cols:
                cohort = rnd(num(r.get(c)), 0)
                if cohort:
                    break
            hv = rnd(num(r.get(headline)), 1)
            out[int(u)] = {"year": y.replace("-", "/")[:5] + y[-2:], "cohort": cohort, "m": ms, "h": hv}
        log(f"  {len(out)} schools with {filt} results; national: { {k: v for k, v in nat.items() if v is not None} }")
        return out, {"name": f"DfE school performance tables: {filt}", "edition": y.replace("-", "/"),
                     "url": "https://www.compare-school-performance.service.gov.uk/download-data"}
    raise RuntimeError(f"no {filt} performance data found")


# ---------------------------------------------------------------- Admissions

def load_admissions(fx):
    html = decode(fx.get(f"https://explore-education-statistics.service.gov.uk/find-statistics/{ADMISSIONS_SLUG}"))
    nd = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S).group(1))
    rv = nd["props"]["pageProps"]["releaseVersionSummary"]
    z = zipfile.ZipFile(io.BytesIO(fx.get(
        f"https://content.explore-education-statistics.service.gov.uk/api/releases/{rv['id']}/files")))
    names = [n for n in z.namelist() if n.lower().endswith(".csv") and re.search(r"school.?level", n, re.I)]
    if not names:
        raise RuntimeError(f"no school-level CSV in admissions zip: {z.namelist()}")
    rows, hdr = read_csv(z.read(names[0]))
    log(f"Admissions {rv.get('title')}: {names[0]} {len(rows)} rows")
    rows = [r for r in rows if (r.get("geographic_level") or "School") == "School" and (r.get("school_urn") or "").strip().isdigit()]
    periods = sorted({r["time_period"] for r in rows})
    latest = periods[-1]
    by = collections.defaultdict(list)
    for r in rows:
        by[int(r["school_urn"])].append(r)

    def year_label(tp):
        return f"{tp[:4]}/{tp[4:]}" if len(tp) == 6 else tp

    out = {}
    for urn, rs in by.items():
        cur = [r for r in rs if r["time_period"] == latest]
        if not cur:
            continue
        # All-through schools can have Reception and Year 7 rows; show the larger intake.
        r = max(cur, key=lambda x: num(x.get("total_number_places_offered")) or 0)
        offers = rnd(num(r.get("total_number_places_offered")), 0)
        first = rnd(num(r.get("times_put_as_1st_preference")), 0)
        first_off = rnd(num(r.get("number_1st_preference_offers")), 0)
        ratio = num(r.get("proportion_1stprefs_v_totaloffers"))
        if ratio is None and offers and first is not None:
            ratio = first / offers
        entry = {"R": "Reception", "7": "Year 7", "9": "Year 9", "3": "Year 3", "10": "Year 10"}.get(
            (r.get("entry_year") or "").strip(), (r.get("school_phase") or "").strip() or "entry")
        rec = {
            "year": year_label(latest), "entry": entry, "offers": offers, "first": first,
            "any": rnd(num(r.get("times_put_as_any_preferred_school")), 0),
            "firstOffers": first_off,
            "ratio": rnd(ratio, 2),
            "pctFirstOffered": rnd(100 * first_off / first, 0) if first and first_off is not None else None,
            "fromOtherLA": rnd(num(r.get("all_applications_from_another_LA")), 0),
        }
        trend = []
        for tp in periods[-4:]:
            same = [x for x in rs if x["time_period"] == tp and x.get("entry_year") == r.get("entry_year")]
            if same:
                v = num(same[0].get("proportion_1stprefs_v_totaloffers"))
                if v is not None:
                    trend.append([year_label(tp), rnd(v, 2)])
        if len(trend) > 1:
            rec["trend"] = trend
        out[urn] = {k: v for k, v in rec.items() if v is not None}
    log(f"  {len(out)} schools with admissions data for {latest}")
    return out, {"name": "DfE primary and secondary school applications and offers", "edition": rv.get("title"),
                 "url": f"https://explore-education-statistics.service.gov.uk/find-statistics/{ADMISSIONS_SLUG}"}


# ---------------------------------------------------------------- Assemble

def address(r):
    parts = [r.get(k, "").strip() for k in ("Street", "Locality", "Address3", "Town", "Postcode")]
    return ", ".join(p for p in parts if p)


def head_name(r):
    parts = [r.get("HeadTitle (name)", ""), r.get("HeadFirstName", ""), r.get("HeadLastName", "")]
    parts = [p.strip() for p in parts if p and p.strip() and p.strip() not in ("Not applicable",)]
    return " ".join(parts) or None


def build(args):
    fx = Fetcher(args.cache)
    sources = []
    gias, src = load_gias(fx)
    sources.append(src)

    def optional(name, fn, *a):
        try:
            data, s = fn(fx, *a)
            sources.append(s)
            return data
        except Exception as e:
            log(f"WARNING: {name} unavailable: {type(e).__name__}: {e}")
            return {}

    ofsted = optional("Ofsted", load_ofsted)
    perf = {k: optional(k, load_performance, k) for k in ("ks2", "ks4", "ks5")}
    adm = optional("admissions", load_admissions)

    types, faiths, las = [], [""], {}
    type_ix, faith_ix = {}, {"": 0}
    cols = ["urn", "name", "lat", "lng", "ph", "sec", "t", "o", "g", "faith", "sel", "la", "ks2", "ks4", "ks5", "dem"]
    table = []
    details = collections.defaultdict(dict)
    for urn in sorted(gias):
        s = gias[urn]
        r = s["row"]
        typ = s["typ"]
        if typ not in type_ix:
            type_ix[typ] = len(types)
            types.append(typ)
        faith = clean(r.get("ReligiousCharacter (name)"))
        if faith in ("None",):
            faith = None
        fk = faith or ""
        if fk not in faith_ix:
            faith_ix[fk] = len(faiths)
            faiths.append(fk)
        la = (r.get("LA (code)") or "").strip() or "000"
        las[la] = (r.get("LA (name)") or "").strip()
        gender = {"Boys": "B", "Girls": "G"}.get(r.get("Gender (name)", ""), "M")
        selective = 1 if r.get("AdmissionsPolicy (name)", "") == "Selective" else 0
        o = ofsted.get(urn)
        a = adm.get(urn)
        table.append([
            urn, r["EstablishmentName"].strip(), s["lat"], s["lng"], s["ph"], s["sec"], type_ix[typ],
            o["h"] if o else 0, gender, faith_ix[fk], selective, la,
            perf["ks2"].get(urn, {}).get("h"),
            # Independent schools' KS4 figures mostly exclude IGCSEs, so they aren't comparable on the map.
            perf["ks4"].get(urn, {}).get("h") if s["sec"] != "i" else None,
            perf["ks5"].get(urn, {}).get("h"),
            a["ratio"] if a else None,
        ])
        lo, hi = r.get("StatutoryLowAge", "").strip(), r.get("StatutoryHighAge", "").strip()
        d = {
            "addr": address(r),
            "web": (r.get("SchoolWebsite") or "").strip() or None,
            "tel": (r.get("TelephoneNum") or "").strip() or None,
            "head": head_name(r),
            "ages": f"{lo}–{hi}" if lo and hi else None,
            "pupils": rnd(num(r.get("NumberOfPupils")), 0),
            "cap": rnd(num(r.get("SchoolCapacity")), 0),
            "fsm": rnd(num(r.get("PercentageFSM")), 1),
            "admPolicy": clean(r.get("AdmissionsPolicy (name)")),
            "faith": faith,
            "nursery": clean(r.get("NurseryProvision (name)")),
            "sixth": clean(r.get("OfficialSixthForm (name)")),
            "boarders": clean(r.get("Boarders (name)")),
            "trust": clean(r.get("Trusts (name)")),
            "inspectorate": clean(r.get("InspectorateName (name)")),
            "ofsted": {k: v for k, v in o.items() if k != "h"} if o else None,
            "adm": a,
        }
        for k in ("ks2", "ks4", "ks5"):
            p = perf[k].get(urn)
            if p:
                d[k] = {"year": p["year"], "cohort": p["cohort"], "m": p["m"]}
                if k == "ks4" and s["sec"] == "i":
                    d[k]["note"] = ("Performance tables largely exclude IGCSEs and other qualifications many independent "
                                    "schools use, so these figures can understate results and aren't comparable.")
        details[la][str(urn)] = {k: v for k, v in d.items() if v not in (None, "", [])}

    if os.path.isdir(os.path.join(OUT, "la")):
        shutil.rmtree(os.path.join(OUT, "la"))
    os.makedirs(os.path.join(OUT, "la"), exist_ok=True)
    payload = {
        "built": dt.date.today().isoformat(),
        "sources": sources,
        "cols": cols,
        "lk": {"type": types, "faith": faiths, "la": las},
        "rows": table,
    }
    with open(os.path.join(OUT, "schools.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    for la, recs in details.items():
        with open(os.path.join(OUT, "la", f"{la}.json"), "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(os.path.join(OUT, "schools.json"))
    total = sum(os.path.getsize(os.path.join(OUT, "la", x)) for x in os.listdir(os.path.join(OUT, "la")))
    phases = collections.Counter(row[4] for row in table)
    log(f"Wrote {len(table)} schools ({size / 1e6:.1f} MB) and {len(details)} LA detail files ({total / 1e6:.1f} MB)")
    log(f"  by phase {dict(phases)}; with Ofsted {sum(1 for x in table if x[7])}, KS2 {sum(1 for x in table if x[12] is not None)}, "
        f"KS4 {sum(1 for x in table if x[13] is not None)}, KS5 {sum(1 for x in table if x[14] is not None)}, "
        f"admissions {sum(1 for x in table if x[15] is not None)}")
    if len(sources) < 6:
        log("NOTE: some optional sources were unavailable; see warnings above. Present:", [s["name"] for s in sources])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", help="directory to cache downloads in (useful when iterating locally)")
    build(ap.parse_args())
