#!/usr/bin/env python3
"""Collect councils' published "last distance offered" figures and attach them to schools.

Most oversubscribed schools in England fill their last places by home-to-school distance, and
councils publish the distance of the last child offered a place ("cut-off" or "furthest distance
offered"). That distance is the school's effective catchment for that year, so the map draws it
as a radius.

There is no national dataset, so this script starts from the pages listed in
scripts/catchment_sources/<region>.json, follows admissions links on each council's site, downloads
candidate documents (PDF, Excel, Word or HTML tables), finds rows that name one of the borough's
schools alongside a distance, and keeps the most recent figure for each school.

Outputs:
  schools/data/catchment.json         {urn: [{entry, year, mi, basis, row, src, borough}, ...]}
  schools/data/catchment_report.json  what was crawled and matched, per borough (for checking)

Run after build_school_data.py (it reads schools/data/schools.json for school names):
  python3 scripts/build_catchments.py --region london [--cache DIR]
"""

import argparse
import collections
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_school_data import OUT, ROOT, Fetcher, decode, log  # noqa: E402

MAX_PAGES = 30
MAX_DOCS = 30
THIS_YEAR = dt.date.today().year

LINK_RX = re.compile(
    r"how (school )?places (were|are) (offered|allocated)|allocation|allocated|offers? (were )?made|cut.?off|"
    r"furthest|last (child|distance|place|allocated)|distance offered|on.?time offers?|school statistics|"
    r"applications and offers|offer day|grid for reception|previous years?|previous allocation|admissions data|"
    r"places were offered|offer outcomes|catchment.?radius|offers map|distance",
    re.I)
SKIP_RX = re.compile(
    r"arrangements|consultation|polic(y|ies)|privacy|cookie|supplementary|in.?year|nursery|appeal(s)? (form|hearing)|"
    r"transport|travel|uniform|term dates|free school meals|\.(jpg|png|gif|svg)$|mailto:|tel:|javascript:",
    re.I)
DOC_URL_RX = re.compile(r"\.(pdf|xlsx?|docx?|csv)(\?|$)|/download|/media/document|/__data/assets|/downloads/file", re.I)
SECONDARY_RX = re.compile(r"secondary|year 7|transfer to sec|\btss\b|high school|y7", re.I)
PRIMARY_RX = re.compile(r"primary|reception|infant|junior|starting school", re.I)
GUIDE_RX = re.compile(r"guide|prospectus|brochure|booklet", re.I)

UNIT_RX = re.compile(r"(\d{1,5}(?:[.,]\d+)?)\s*(miles?|mi\b|mls\b|kms?\b|kilomet\w*|metres|meters|mtrs|m\b)", re.I)
NUM_RX = re.compile(r"^\s*(\d{1,5}(?:\.\d+)?)\s*$")


# ------------------------------------------------------------------ HTML helpers

class PageParser(HTMLParser):
    """Collect links (href, text) and tables (rows of cell text) from an HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.tables = [], []
        self._a = None
        self._tables = []  # stack of tables being built
        self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._a = [a["href"], ""]
        elif tag == "table":
            self._tables.append([])
        elif tag == "tr" and self._tables:
            self._tables[-1].append([])
        elif tag in ("td", "th") and self._tables:
            self._cell = ""
        elif tag == "br" and self._cell is not None:
            self._cell += " "

    def handle_endtag(self, tag):
        if tag == "a" and self._a:
            self.links.append((self._a[0], " ".join(self._a[1].split())))
            self._a = None
        elif tag in ("td", "th") and self._tables and self._cell is not None:
            if not self._tables[-1]:
                self._tables[-1].append([])
            self._tables[-1][-1].append(" ".join(self._cell.split()))
            self._cell = None
        elif tag == "table" and self._tables:
            t = self._tables.pop()
            if t:
                self.tables.append([r for r in t if r])

    def handle_data(self, data):
        if self._a is not None:
            self._a[1] += data
        if self._cell is not None:
            self._cell += data


def page_title(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    return html.unescape(" ".join(m.group(1).split())) if m else ""


# ------------------------------------------------------------------ document -> rows

def rows_from_pdf(raw):
    import pdfplumber
    rows = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages[:80]:
            tables = page.extract_tables() or []
            got = False
            for t in tables:
                for r in t:
                    cells = [" ".join((c or "").split()) for c in r]
                    if any(cells):
                        rows.append(cells)
                        got = True
                rows.append(["<table end>"])
            if not got:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    if line.strip():
                        rows.append([c.strip() for c in re.split(r"\s{2,}|\t", line) if c.strip()] or [line])
    return rows


def rows_from_xlsx(raw):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        for r in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in r]
            if any(cells):
                rows.append(cells)
        rows.append(["<table end>"])
    return rows


def rows_from_xls(raw):
    import xlrd
    wb = xlrd.open_workbook(file_contents=raw)
    rows = []
    for sh in wb.sheets():
        for i in range(sh.nrows):
            cells = [str(c).strip() for c in sh.row_values(i)]
            if any(cells):
                rows.append(cells)
        rows.append(["<table end>"])
    return rows


def rows_from_docx(raw):
    import docx
    d = docx.Document(io.BytesIO(raw))
    rows = []
    for t in d.tables:
        for r in t.rows:
            rows.append([" ".join(c.text.split()) for c in r.cells])
        rows.append(["<table end>"])
    for p in d.paragraphs:
        if p.text.strip():
            rows.append([p.text.strip()])
    return rows


def rows_from_html(text):
    p = PageParser()
    try:
        p.feed(text)
    except Exception:
        pass
    rows = []
    for t in p.tables:
        rows.extend(t)
        rows.append(["<table end>"])
    return rows, p.links


def sniff(raw, url):
    if raw[:4] == b"%PDF":
        return "pdf"
    if raw[:2] == b"PK":
        return "docx" if b"word/" in raw[:3000] else "xlsx"
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    return "html"


# ------------------------------------------------------------------ school matching

STOP = {
    "school", "schools", "primary", "academy", "the", "and", "of", "community", "voluntary", "aided", "controlled",
    "foundation", "free", "federation", "trust", "college", "church", "england", "ce", "rc", "cofe", "vc", "va",
    "catholic", "roman", "c", "e", "cte", "specialist", "sports", "language", "arts", "technology", "science",
    "mixed", "day", "with", "for", "a", "an", "at", "in", "london", "borough", "nursery", "centre", "unit",
}
DENOMS = {"ce", "rc", "jewish", "muslim", "islamic", "sikh", "hindu", "methodist", "christian"}


def norm_tokens(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\bc\s*of\s*e\b|\bcofe\b|church of england", " ce ", s)
    s = re.sub(r"roman catholic|\bcatholic\b|\br\.?\s?c\.?\b", " rc ", s)
    s = re.sub(r"\bsaint\b|\bst\.", " st ", s)
    s = s.replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", s)


def core(tokens):
    return [t for t in tokens if t not in STOP]


class Matcher:
    def __init__(self, schools):
        self.schools = schools
        full = {}
        for s in schools:
            c = core(norm_tokens(s["name"]))
            if c:
                full[s["urn"]] = c
        # Aliases: the full core name, plus short prefixes when they are unique in the borough
        # (documents often drop suffixes like "and Language College").
        self.aliases = collections.defaultdict(set)
        for urn, c in full.items():
            self.aliases[" ".join(c)].add(urn)
        prefixes = collections.defaultdict(set)
        for urn, c in full.items():
            for k in (2, 3):
                if len(c) > k:
                    prefixes[" ".join(c[:k])].add(urn)
        for p, urns in prefixes.items():
            if len(urns) == 1 and len(p) >= 8 and p not in self.aliases:
                self.aliases[p] |= urns
        self.denoms = {s["urn"]: set(norm_tokens(s["name"])) & DENOMS for s in schools}
        self.by_urn = {s["urn"]: s for s in schools}

    def match(self, text):
        toks = norm_tokens(text)
        r = " " + " ".join(core(toks)) + " "
        present = set(toks) & DENOMS
        best, best_len, tie = None, 0, False
        for alias, urns in self.aliases.items():
            if len(alias) < 3 or f" {alias} " not in r:
                continue
            cands = list(urns)
            if len(cands) > 1:
                # Same core name (e.g. two "St Mary's"): use denomination words to pick one.
                scored = sorted(cands, key=lambda u: -len(self.denoms[u] & present))
                if len(self.denoms[scored[0]] & present) == len(self.denoms[scored[1]] & present):
                    if len(alias) > best_len:
                        best, best_len, tie = None, len(alias), True
                    continue
                cands = scored[:1]
            if len(alias) > best_len:
                best, best_len, tie = cands[0], len(alias), False
            elif len(alias) == best_len and cands[0] != best:
                tie = True
        return None if tie else best


# ------------------------------------------------------------------ distance extraction

def to_miles(v, unit):
    unit = unit.lower()
    if unit.startswith("mi") or unit.startswith("ml"):
        return v
    if unit.startswith("k"):
        return v / 1.609344
    return v / 1609.344  # metres


def doc_unit_hint(rows):
    text = " ".join(" ".join(r) for r in rows[:400]).lower()
    counts = {"miles": len(re.findall(r"\bmiles?\b", text)),
              "metres": len(re.findall(r"\bmet(re|er)s\b|\(m\)", text)),
              "km": len(re.findall(r"\bkm\b|kilomet", text))}
    unit, n = max(counts.items(), key=lambda kv: kv[1])
    return unit if n else None


def header_info(row):
    """For a header row, return (distance column index, unit) if it names a distance column."""
    best = None
    for i, c in enumerate(row):
        cl = c.lower()
        if not re.search(r"distance|miles|metres|meters|\bkm\b", cl):
            continue
        if re.search(r"sibling", cl) and not re.search(r"non.?sibling|other", cl):
            continue
        score = 1 + (2 if re.search(r"last|furthest|further|cut.?off|final|maximum|max\b|offered", cl) else 0)
        unit = "miles" if "mile" in cl else "metres" if re.search(r"metre|meter|\(m\)", cl) else "km" if re.search(r"\bkm\b|kilomet", cl) else None
        if best is None or score > best[0]:
            best = (score, i, unit)
    return (best[1], best[2]) if best else (None, None)


def distance_from_row(cells, col, col_unit, hint):
    """Return (miles, how) or (None, None)."""
    text = " | ".join(cells)
    if re.search(r"\ball\b.{0,30}(offered|applicants)|no distance|not oversubscribed|undersubscribed", text, re.I) and not UNIT_RX.search(text):
        return None, "all offered"
    # 1) The column the header says holds the distance
    if col is not None and col < len(cells):
        c = cells[col]
        m = UNIT_RX.search(c)
        if m:
            return to_miles(float(m.group(1).replace(",", ".")), m.group(2)), "column+unit"
        m = NUM_RX.match(c.replace(",", ""))
        if m and (col_unit or hint):
            return to_miles(float(m.group(1)), col_unit or hint), "column"
    # 2) Any value written with a unit in the row (use the last one: tables put the cut-off last)
    ms = [m for m in UNIT_RX.finditer(text) if not re.search(r"sibling", text[max(0, m.start() - 25):m.start()], re.I)]
    if ms:
        m = ms[-1]
        return to_miles(float(m.group(1).replace(",", ".")), m.group(2)), "unit in row"
    # 3) Miles documents: a bare decimal like 0.532 in a cell
    if hint == "miles":
        vals = [float(c) for c in cells if re.fullmatch(r"\d{1,2}\.\d{2,4}", c.strip())]
        if vals:
            return vals[-1], "decimal (miles doc)"
    if hint == "km":
        vals = [float(c) for c in cells if re.fullmatch(r"\d{1,2}\.\d{2,4}", c.strip())]
        if vals:
            return vals[-1] / 1.609344, "decimal (km doc)"
    return None, None


# ------------------------------------------------------------------ per-document processing

def doc_year(title, url, rows):
    head = " ".join(" ".join(r) for r in rows[:30])
    cand = [int(y) for y in re.findall(r"20[12]\d", f"{title} {urllib.parse.unquote(url)}") if int(y) <= THIS_YEAR]
    if not cand:
        cand = [int(y) for y in re.findall(r"20[12]\d", head) if int(y) <= THIS_YEAR]
    if not cand:
        return None
    y = max(cand)
    # Guides for next year's entry quote the previous year's allocations.
    if GUIDE_RX.search(title + " " + url) and y >= THIS_YEAR - 1:
        y -= 1
    return y


def doc_phase(title, url, rows):
    s = f"{title} {urllib.parse.unquote(url)}"
    if SECONDARY_RX.search(s) and not PRIMARY_RX.search(s):
        return "S"
    if PRIMARY_RX.search(s) and not SECONDARY_RX.search(s):
        return "P"
    return None


def entry_for(school, phase_hint):
    ph = school["ph"]
    if ph == "S" or (ph == "A" and phase_hint == "S"):
        return "Year 7"
    if ph in ("P", "A"):
        n = school["name"].lower()
        return "Year 3" if "junior" in n and "infant" not in n else "Reception"
    return None


def process_doc(rows, matcher, title, url, borough):
    hint = doc_unit_hint(rows)
    year = doc_year(title, url, rows)
    phase = doc_phase(title, url, rows)
    col, col_unit = None, None
    found, unmatched = [], []
    for cells in rows:
        if cells == ["<table end>"]:
            col, col_unit = None, None
            continue
        text = " | ".join(cells)
        h_col, h_unit = header_info(cells)
        if h_col is not None and not matcher.match(text):
            col, col_unit = h_col, h_unit
            continue
        urn = matcher.match(text)
        mi, how = distance_from_row(cells, col, col_unit, hint)
        if urn is None:
            if mi is not None and len(unmatched) < 25:
                unmatched.append(text[:200])
            continue
        school = matcher.by_urn[urn]
        entry = entry_for(school, phase)
        if entry is None or (phase == "S" and school["ph"] == "P") or (phase == "P" and school["ph"] == "S"):
            continue
        if mi is not None and not (0.02 <= mi <= 15):
            how, mi = f"rejected {mi:.2f} mi", None
        found.append({"urn": urn, "entry": entry, "year": year, "mi": round(mi, 3) if mi is not None else None,
                      "how": how, "row": text[:300], "src": url, "title": title[:150], "borough": borough["name"],
                      "basis": borough.get("basis")})
    return found, unmatched, {"unit": hint, "year": year, "phase": phase}


# ------------------------------------------------------------------ crawl

def same_site(a, b):
    ha, hb = urllib.parse.urlparse(a).netloc.lower(), urllib.parse.urlparse(b).netloc.lower()
    base = lambda h: ".".join(h.split(".")[-3:]) if h.endswith(".gov.uk") or h.endswith(".org.uk") else ".".join(h.split(".")[-2:])
    return base(ha) == base(hb)


def crawl_borough(fx, b, matcher):
    report = {"name": b["name"], "pages": [], "docs": [], "errors": []}
    queue = [(u, 0, "seed") for u in b.get("seeds", [])] + [(u, 0, "known doc") for u in b.get("docs", [])]
    seen, results = set(), []
    pages = docs = 0
    while queue:
        url, depth, why = queue.pop(0)
        url = url.split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        is_doc_url = bool(DOC_URL_RX.search(url))
        if (is_doc_url and docs >= MAX_DOCS) or (not is_doc_url and pages >= MAX_PAGES):
            continue
        try:
            raw = fx.get(url, tries=2)
        except Exception as e:
            report["errors"].append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
            continue
        kind = sniff(raw, url)
        title = why
        links = []
        try:
            if kind == "html":
                pages += 1
                text = decode(raw)
                title = page_title(text) or why
                rows, links = rows_from_html(text)
            else:
                docs += 1
                rows = {"pdf": rows_from_pdf, "xlsx": rows_from_xlsx, "xls": rows_from_xls, "docx": rows_from_docx}[kind](raw)
                title = why if why not in ("seed", "known doc", "link") else urllib.parse.unquote(url.rsplit("/", 1)[-1])
        except Exception as e:
            report["errors"].append(f"{url}: parse {kind}: {type(e).__name__}: {str(e)[:120]}")
            continue
        found, unmatched, meta = process_doc(rows, matcher, title, url, b)
        with_dist = [f for f in found if f["mi"] is not None]
        entry = {"url": url, "kind": kind, "title": title[:150], "rows": len(rows), "matched": len(found),
                 "with_distance": len(with_dist), **meta}
        if unmatched:
            entry["unmatched_with_distance"] = unmatched[:12]
        if found:
            entry["sample"] = [f"{matcher.by_urn[f['urn']]['name']} -> {f['mi']} ({f['how']}) :: {f['row'][:120]}" for f in found[:8]]
        (report["pages"] if kind == "html" else report["docs"]).append(entry)
        results.extend(found)
        # Follow links
        for href, text in links:
            href = html.unescape(href.strip())
            absu = urllib.parse.urljoin(url, href)
            if not absu.startswith("http") or absu in seen:
                continue
            label = f"{text} {urllib.parse.unquote(absu)}"
            if SKIP_RX.search(label) or not LINK_RX.search(label):
                continue
            if DOC_URL_RX.search(absu):
                queue.append((absu, depth + 1, text or "link"))
            elif depth < 2 and same_site(absu, url):
                queue.append((absu, depth + 1, text or "link"))
        time.sleep(0.3)
    report["fetched_pages"], report["fetched_docs"] = pages, docs
    return results, report


def pick_best(records):
    """One record per (urn, entry): newest year, then from the document with most matches, with a distance."""
    doc_matches = collections.Counter(r["src"] for r in records if r["mi"] is not None)
    best = {}
    for r in records:
        if r["mi"] is None:
            continue
        key = (r["urn"], r["entry"])
        rank = (r["year"] or 0, doc_matches[r["src"]])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    return [v[1] for v in best.values()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="london")
    ap.add_argument("--cache")
    ap.add_argument("--only", help="comma-separated borough names to process (for testing)")
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(ROOT, "scripts", "catchment_sources", f"{args.region}.json")))
    data = json.load(open(os.path.join(OUT, "schools.json")))
    c = {k: i for i, k in enumerate(data["cols"])}
    by_la = collections.defaultdict(list)
    for r in data["rows"]:
        if r[c["sec"]] in ("m", "a"):  # state schools only; independents don't publish cut-offs
            by_la[int(r[c["la"]])].append({"urn": r[c["urn"]], "name": r[c["name"]], "ph": r[c["ph"]]})

    fx = Fetcher(args.cache)
    all_best, reports = [], []
    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    for b in cfg["boroughs"]:
        if only and b["name"].lower() not in only:
            continue
        schools = [s for la in b["las"] for s in by_la.get(la, [])]
        matcher = Matcher(schools)
        log(f"\n== {b['name']} ({len(schools)} state schools)")
        records, rep = crawl_borough(fx, b, matcher)
        best = pick_best(records)
        rep["schools_with_cutoff"] = len(best)
        rep["schools_total"] = len(schools)
        years = collections.Counter(r["year"] for r in best)
        rep["years"] = dict(years)
        log(f"   pages {rep['fetched_pages']}, docs {rep['fetched_docs']}, errors {len(rep['errors'])}; "
            f"cut-offs for {len(best)} schools; years {dict(years)}")
        all_best.extend(best)
        reports.append(rep)

    out = collections.defaultdict(list)
    for r in all_best:
        out[str(r["urn"])].append({k: r[k] for k in ("entry", "year", "mi", "basis", "row", "src", "borough")})
    with open(os.path.join(OUT, "catchment.json"), "w", encoding="utf-8") as f:
        json.dump({"built": dt.date.today().isoformat(), "region": args.region, "schools": out}, f,
                  ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "catchment_report.json"), "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=1)
    log(f"\nWrote cut-off distances for {len(out)} schools across {len(reports)} boroughs")


if __name__ == "__main__":
    main()
