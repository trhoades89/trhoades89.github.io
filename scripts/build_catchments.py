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
import concurrent.futures
import datetime as dt
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_school_data import OUT, ROOT, UA, Fetcher, decode, log  # noqa: E402

MAX_PAGES = 30
MAX_DOCS = 60
BOROUGH_BUDGET_S = 420
THIS_YEAR = dt.date.today().year

LINK_RX = re.compile(
    r"how (school )?places (were|are) (offered|allocated)|allocation|allocated|offers? (were )?made|cut.?off|"
    r"furthest|last (child|distance|place|allocated)|distance offered|on.?time offers?|school statistics|"
    r"applications and offers|offer day|grid for reception|previous years?|previous allocation|admissions data|"
    r"places were offered|offer outcomes|catchment.?radius|offers map|distance|statistic|recent years|"
    r"appeals data|admissions (and appeals )?data|national offer day",
    re.I)
# Parent guides often quote last year's cut-offs; only followed when they are documents.
GUIDE_LINK_RX = re.compile(r"guide|booklet|brochure|prospectus|education in \w+|starting (primary|secondary)? ?school|"
                           r"transfer to secondary|choose a .* school", re.I)
BLOCKED_RX = re.compile(r"just a moment|cf-chl|captcha|access denied|request unsuccessful|incapsula|are you a robot|"
                        r"enable javascript and cookies", re.I)
SKIP_RX = re.compile(
    r"arrangements|consultation|polic(y|ies)|privacy|cookie|supplementary|in.?year|nursery|appeal(s)? (form|hearing)|"
    r"transport|travel|uniform|term dates|free school meals|\.(jpg|png|gif|svg)$|mailto:|tel:|javascript:|"
    r"housing|council.?tax|parking|planning|eforms|feedback|/news|jobs|careers|login|sign.?in|facebook|twitter|"
    r"linkedin|instagram|youtube|contact.?us|accessibility",
    re.I)
DOC_URL_RX = re.compile(r"\.(pdf|xlsx?|docx?|csv)(\?|$)|/download|/media/document|/__data/assets|/downloads/file", re.I)
SECONDARY_RX = re.compile(r"secondary|year 7|transfer to sec|\btss\b|high school|y7", re.I)
PRIMARY_RX = re.compile(r"primary|reception|infant|junior|starting school", re.I)
GUIDE_RX = re.compile(r"guide|prospectus|brochure|booklet", re.I)

UNIT_RX = re.compile(r"(\d{1,5}(?:[.,]\d+)?)\s*(miles?|mi\b|mls\b|kms?\b|kilomet\w*|metres|meters|mtrs|m\b)", re.I)
NUM_RX = re.compile(r"^\s*(\d{1,5}(?:\.\d+)?)\s*$")


# ------------------------------------------------------------------ HTML helpers

BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "dt", "dd", "section", "article",
              "table", "ul", "ol", "caption", "summary", "details", "td", "th"}


class PageParser(HTMLParser):
    """Collect links (href, text), tables (rows of cell text) and block-level text lines from a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.tables, self.lines = [], [], []
        self._a = None
        self._tables = []  # stack of tables being built
        self._cell = None
        self._line = ""
        self._skip = 0

    def _flush(self):
        t = " ".join(self._line.split())
        if t:
            self.lines.append(t)
        self._line = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag in BLOCK_TAGS or tag == "br":
            self._flush() if tag not in ("td", "th") else None
        if tag == "a" and a.get("href"):
            self._a = [a["href"], ""]
        elif tag == "table":
            self._tables.append([])
        elif tag == "tr" and self._tables:
            self._tables[-1].append([])
        elif tag in ("td", "th") and self._tables:
            self._cell = ""
            self._line += " | "
        elif tag == "br" and self._cell is not None:
            self._cell += " "

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
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
        if tag in BLOCK_TAGS and tag not in ("td", "th"):
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._a is not None:
            self._a[1] += data
        if self._cell is not None:
            self._cell += data
        self._line += data


def page_title(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    return html.unescape(" ".join(m.group(1).split())) if m else ""


# ------------------------------------------------------------------ document -> rows

def rows_from_pdf(raw):
    """Table rows and the text around them, in reading order (headings often sit above their table)."""
    import pdfplumber
    rows = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages[:80]:
            items = []  # (top, [cells]) in page order
            try:
                tables = page.find_tables()
            except Exception:
                tables = []
            boxes = []
            for t in tables:
                boxes.append(t.bbox)
                body = [[" ".join((c or "").split()) for c in r] for r in (t.extract() or [])]
                body = [r for r in body if any(r)]
                if body:
                    items.append((t.bbox[1], body + [["<table end>"]]))
            try:
                lines = page.extract_text_lines()
            except Exception:
                lines = [{"text": ln, "top": 0, "x0": 0, "x1": 0, "bottom": 0} for ln in (page.extract_text() or "").splitlines()]
            for ln in lines:
                cx, cy = (ln.get("x0", 0) + ln.get("x1", 0)) / 2, (ln.get("top", 0) + ln.get("bottom", 0)) / 2
                if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in boxes):
                    continue  # already captured as part of a table
                text = ln.get("text", "").strip()
                if text:
                    cells = [c.strip() for c in re.split(r"\s{2,}|\t", text) if c.strip()] or [text]
                    items.append((ln.get("top", 0), [cells]))
            for _, rs in sorted(items, key=lambda it: it[0]):
                rows.extend(rs)
            rows.append(["<table end>"])
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
    """Tables first; then the page's text lines (for pages that list schools in paragraphs or accordions)."""
    p = PageParser()
    try:
        p.feed(text)
        p._flush()
    except Exception:
        pass
    rows = []
    for t in p.tables:
        rows.extend(t)
        rows.append(["<table end>"])
    rows.append(["<text>"])
    rows.extend([[c.strip() for c in ln.split(" | ") if c.strip()] or [ln] for ln in p.lines if "|" not in ln or True])
    rows.append(["<table end>"])
    return rows, p.links, "\n".join(p.lines)


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
    "school", "schools", "primary", "secondary", "academy", "the", "and", "of", "community", "voluntary", "aided",
    "controlled", "foundation", "free", "federation", "trust", "college", "church", "england", "ce", "rc", "cofe",
    "vc", "va", "catholic", "roman", "c", "e", "cte", "specialist", "sports", "language", "arts", "technology",
    "science", "mixed", "day", "with", "for", "a", "an", "at", "in", "london", "borough", "nursery", "centre",
    "unit", "children", "childrens", "centres",
}
DENOMS = {"ce", "rc", "jewish", "muslim", "islamic", "sikh", "hindu", "methodist", "christian"}
COMMON_FIRST = {"st", "holy", "our", "saint", "sacred", "christ", "all", "new", "north", "south", "east", "west",
                "upper", "lower", "great", "little", "oasis", "harris", "ark", "unity", "hope", "grace", "trinity"}


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
            for k in (1, 2, 3):
                if len(c) > k:
                    prefixes[" ".join(c[:k])].add(urn)
        for p, urns in prefixes.items():
            one_word = " " not in p
            if len(urns) == 1 and p not in self.aliases and (
                    (not one_word and len(p) >= 8) or (one_word and len(p) >= 6 and p not in COMMON_FIRST)):
                self.aliases[p] |= urns
        self.denoms = {s["urn"]: set(norm_tokens(s["name"])) & DENOMS for s in schools}
        self.by_urn = {s["urn"]: s for s in schools}

    def distinct(self, text):
        """How many different schools a line names (headings that name two are ambiguous)."""
        r = " " + " ".join(core(norm_tokens(text))) + " "
        hits = {frozenset(u) for a, u in self.aliases.items() if len(a) >= 5 and f" {a} " in r}
        return len(hits)

    def match(self, text, phase=None):
        toks = norm_tokens(text)
        r = " " + " ".join(core(toks)) + " "
        present = set(toks) & DENOMS
        best, best_len, tie = None, 0, False
        for alias, urns in self.aliases.items():
            if len(alias) < 3 or f" {alias} " not in r:
                continue
            cands = list(urns)
            if len(cands) > 1 and phase:
                # e.g. "Avanti House School" (secondary) vs "Avanti House Primary School"
                same = [u for u in cands if (self.by_urn[u]["ph"] == "P") == (phase == "P")]
                if len(same) == 1:
                    cands = same
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

MILES_DEC = re.compile(r"\d{1,2}\.\d{3,4}")
METRES_DEC = re.compile(r"\d{2,5}\.\d{1,2}")
YEAR_CELL = re.compile(r"(?<!\d)(20[12]\d)(?!\d)|^(?:NOD\s*)?(\d{2})\s*/\s*\d{2}$")
LABEL_RX = re.compile(r"^\s*(band|criteri|crit\b|distance|any other|other|remaining|community|open|catchment|"
                      r"non.?sibling|all other|general|random|priority|last|furthest|offered|places?|oversub|"
                      r"[A-E]\b|\d(\.\d)?\b|20[12]\d\b|tier|inner|outer|zone|nearest|home|local)", re.I)
SIBLING_RX = re.compile(r"sibling", re.I)
FEEDER_RX = re.compile(r"feeder|attending|attends|linked (infant|junior)|children (at|from) ", re.I)
FAITH_RX = re.compile(r"faith|catholic|baptis|church|practis|religio|sikh|jewish|muslim|hindu|christian|parish|worship|"
                      r"bursary|music|aptitude|sport|scholarship|nursery|feeder|staff|medical|social|looked after|ehcp|send\b", re.I)
PREFER_RX = re.compile(r"distance|any other|other applicant|remaining|community|open|band|last|furthest|general|all other", re.I)


def to_miles(v, unit):
    unit = unit.lower()
    if unit.startswith("mi") or unit.startswith("ml"):
        return v
    if unit.startswith("k"):
        return v / 1.609344
    return v / 1609.344  # metres


def doc_unit_hint(rows):
    text = " ".join(" ".join(r) for r in rows[:600]).lower()
    counts = {"miles": len(re.findall(r"\bmiles?\b", text)),
              "metres": len(re.findall(r"\bmet(re|er)s\b|\(m\)|\bmetres\b", text)),
              "km": len(re.findall(r"\bkms?\b|kilomet", text))}
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


def year_columns(row):
    """Header cells that are years (e.g. 2024 | 2025 | 2026, or 24/25): {index: year}."""
    out = {}
    for i, c in enumerate(row):
        c = c.strip()
        if len(c) > 30:
            continue
        m = YEAR_CELL.search(c)
        if m:
            y = int(m.group(1)) if m.group(1) else 2000 + int(m.group(2))
            if 2010 <= y <= THIS_YEAR:
                out[i] = y
    return out if len(out) >= 2 else {}


def value_in(cell, unit_hint):
    """Parse one cell as a distance in miles, or None."""
    m = UNIT_RX.search(cell)
    if m:
        return to_miles(float(m.group(1).replace(",", ".")), m.group(2))
    c = cell.strip().replace(",", "")
    if NUM_RX.match(c):
        v = float(c)
        if unit_hint:
            return to_miles(v, unit_hint)
        if "." in c:
            return to_miles(v, "metres" if v >= 20 else "miles")
    return None


def distance_from_row(cells, col, col_unit, hint, year_cols=None):
    """Return (miles, how, year_or_None)."""
    text = " | ".join(cells)
    if (re.search(r"\ball\b.{0,40}(offered|applicants|preferences met)|no distance|not oversubscribed|undersubscribed|"
                  r"no cut.?off|places? available", text, re.I) and not UNIT_RX.search(text)):
        return None, "all offered", None
    # 1) Columns headed by year: take the newest year with a value
    if year_cols:
        for i, y in sorted(year_cols.items(), key=lambda kv: -kv[1]):
            if i < len(cells):
                v = value_in(cells[i], col_unit or hint)
                if v is not None:
                    return v, f"year column {y}", y
    # 2) The column the header says holds the distance
    if col is not None and col < len(cells):
        v = value_in(cells[col], col_unit or hint)
        if v is not None:
            return v, "column", None
    # 3) Any value written with a unit in the row (use the last one: tables put the cut-off last)
    ms = [m for m in UNIT_RX.finditer(text)
          if not SIBLING_RX.search(text[max(0, m.start() - 40):m.start()])
          and not re.search(r"within\s+(a\s+)?$|up to\s+a\s+$", text[max(0, m.start() - 12):m.start()], re.I)
          and not re.match(r"\s*(radius|catchment)", text[m.end():m.end() + 12], re.I)]
    if ms:
        m = ms[-1]
        return to_miles(float(m.group(1).replace(",", ".")), m.group(2)), "unit in row", None
    # 4) Bare decimals: 0.532 style in miles/km documents, 1234.56 style in metres documents.
    #    Not in prose or near test-score wording, where numbers are scores, averages or counts.
    #    Judge each number by the words just before it: scores, averages and sums sit next to that wording.
    toks = []
    for m in re.finditer(r"(?<![\d.])\d{1,5}\.\d{1,4}(?![\d.])", text):
        before = text[max(0, m.start() - 60):m.start()]
        if not re.search(r"score|test|candidate|\bsat\b|points|mark|divide|average|ratio|%|=", before, re.I):
            toks.append(m.group(0))
    if hint in ("miles", "km", None):
        vals = [float(t) for t in toks if MILES_DEC.fullmatch(t)]
        if vals:
            return to_miles(vals[-1], hint or "miles"), f"decimal ({hint or 'assumed miles'})", None
    if True:  # a 2-decimal figure of 20+ can only be metres, whatever units the rest of the document uses
        vals = [float(t) for t in toks if METRES_DEC.fullmatch(t) and float(t) >= 20]
        if vals:
            return to_miles(vals[-1], "metres"), "decimal (metres)", None
    return None, None, None


# ------------------------------------------------------------------ per-document processing

def years_in(s):
    """Years mentioned in a title or URL, reading ranges like 2018-26 as ending in 2026."""
    ys = [int(y) for y in re.findall(r"(?<!\d)(20[12]\d)(?!\d)", s)]
    ys += [2000 + int(m.group(1)) for m in re.finditer(r"20[12]\d\s*[-–]\s*(\d{2})(?!\d)", s)]
    return [y for y in ys if y <= THIS_YEAR]


def doc_year(title, url, rows):
    head = " ".join(" ".join(r) for r in rows[:30])
    cand = years_in(f"{title} {urllib.parse.unquote(url)}")
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


def choose_block(lines):
    """From the distance lines under a school heading, pick the general cut-off.

    Sibling and faith-priority lines are ignored when other lines exist; among the rest, prefer lines
    that name distance/other applicants/bands and take the widest (bands each have their own cut-off).
    """
    newest = max((x[3] for x in lines if x[3]), default=None)
    if newest:  # "previous years" tables: one line per year under each school
        lines = [x for x in lines if x[3] == newest]
    pool = [x for x in lines if not SIBLING_RX.search(x[2]) or re.search(r"non.?sibling", x[2], re.I)] or lines
    pool = [x for x in pool if not FAITH_RX.search(x[2])] or pool
    pool = [x for x in pool if PREFER_RX.search(x[2])] or pool
    return max(pool, key=lambda x: x[0])[:3] + (newest,)


def process_doc(rows, matcher, title, url, borough, context=""):
    hint = doc_unit_hint(rows)
    year = doc_year(title, url, rows)
    phase = doc_phase(title, url + " " + context, rows)
    found, unmatched = [], []
    col = col_unit = None
    year_cols = {}
    section_year = None
    in_text = False
    have_table_hits = False
    cur = None  # (urn, heading text, [distance lines], rows since heading, all_offered)
    title_urn = matcher.match(title, phase) if title and title not in ("seed", "known doc", "link") else None
    if title_urn:
        cur = (title_urn, title[:160], [], -40, False)  # one-school documents (e.g. Lambeth secondaries)

    def add(urn, mi, how, row, y):
        school = matcher.by_urn[urn]
        if school.get("sel") and mi is not None and not how.replace("block: ", "").startswith(("unit", "column")):
            how, mi = "grammar school: needs an explicit distance", None
        entry = entry_for(school, phase)
        if entry is None or (phase == "S" and school["ph"] == "P") or (phase == "P" and school["ph"] == "S"):
            return
        if mi is not None and not (0.02 <= mi <= 15):
            how, mi = f"rejected {mi:.2f} mi", None
        found.append({"urn": urn, "entry": entry, "year": y or section_year or year,
                      "mi": round(mi, 3) if mi is not None else None, "how": how, "row": row[:300], "src": url,
                      "title": title[:150], "borough": borough["name"], "basis": borough.get("basis")})

    def flush():
        nonlocal cur
        if cur:
            urn, head, lines, _, all_off = cur
            if lines:
                mi, how, text, ly = choose_block(lines)
                add(urn, mi, f"block: {how}", f"{head} … {text}", ly)
            elif all_off:
                add(urn, None, "all offered", head, None)
            else:
                add(urn, None, None, head, None)
        cur = None

    for cells in rows:
        if cells == ["<text>"]:
            flush()
            in_text = True
            col = col_unit = None
            year_cols = {}
            continue
        if in_text and have_table_hits:
            break  # the page's tables already gave us the data; its text repeats them
        if cells == ["<table end>"]:
            if cur and cur[2]:
                flush()  # keep an open school heading alive: its figures are often in the next table
            col = col_unit = None
            year_cols = {}
            continue
        text = " | ".join(cells)
        urn = matcher.match(text, phase)
        if urn is not None and (FEEDER_RX.search(text) or matcher.distinct(text) > 1):
            urn = None  # "attending Millbank (feeder school)" or two schools side by side: not a heading
        # Header rows: a distance column, or one column per year
        yc = year_columns(cells)
        h_col, h_unit = header_info(cells)
        if urn is None and (yc or h_col is not None) and not re.search(r"\d\.\d", text):
            if yc:
                year_cols = yc
                col_unit = h_unit or col_unit
            if h_col is not None:
                col, col_unit = h_col, h_unit
            continue
        mi, how, y = distance_from_row(cells, col, col_unit, hint, year_cols)
        if urn is None:
            # Year sub-heading ("2026", "Offers made in April 2025") inside multi-year documents
            ym = re.search(r"(?<!\d)(20[12]\d)(?!\d)", text)
            if ym and mi is None and len(text) <= 80 and int(ym.group(1)) <= THIS_YEAR and not cur:
                flush()
                section_year = int(ym.group(1))
                continue
            first = next((c for c in cells if c.strip()), "")
            label_like = LABEL_RX.match(first) or len([c for c in cells if c.strip()]) <= 2
            if cur and label_like and cur[3] < 40 and not matcher.distinct(text):
                if mi is not None:
                    ly = re.match(r"\s*(20[12]\d)\b", first)
                    cur[2].append((mi, how, text, int(ly.group(1)) if ly else None))
                elif how == "all offered":
                    cur = (cur[0], cur[1], cur[2], cur[3], True)
                cur = (cur[0], cur[1], cur[2], cur[3] + 1, cur[4])
            elif mi is not None and len(unmatched) < 25:
                unmatched.append(text[:200])
            continue
        flush()
        if mi is not None or how == "all offered":
            add(urn, mi, how, text, y)
            have_table_hits = have_table_hits or (mi is not None and not in_text)
        else:
            cur = (urn, text[:160], [], 0, False)
    flush()
    return found, unmatched, {"unit": hint, "year": year, "phase": phase}


# ------------------------------------------------------------------ crawl

def fetch(fx, url):
    """Download with the shared fetcher; if the site refuses Python's client, retry once with curl."""
    try:
        return fx.get(url, tries=2, timeout=45)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 406, 429, 301, 302):
            raise
        first = e
    except Exception as e:  # redirect loops needing cookies, TLS quirks
        first = e
    try:
        out = subprocess.run(
            ["curl", "-sSL", "--compressed", "--max-time", "60", "-b", "", "-A", UA["User-Agent"],
             "-H", "Accept-Language: en-GB,en;q=0.9", "-H", "Accept: text/html,application/pdf,*/*", "-f", url],
            capture_output=True, timeout=90)
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except Exception:
        pass
    raise first


def same_site(a, b):
    ha, hb = urllib.parse.urlparse(a).netloc.lower(), urllib.parse.urlparse(b).netloc.lower()
    base = lambda h: ".".join(h.split(".")[-3:]) if h.endswith(".gov.uk") or h.endswith(".org.uk") else ".".join(h.split(".")[-2:])
    return base(ha) == base(hb)


def crawl_borough(fx, b, matcher):
    report = {"name": b["name"], "pages": [], "docs": [], "errors": []}
    queue = [(u, 0, "seed", "") for u in b.get("seeds", [])] + [(u, 0, "known doc", "") for u in b.get("docs", [])]
    seen, results = set(), []
    pages = docs = 0
    started = time.time()
    while queue:
        if time.time() - started > BOROUGH_BUDGET_S:
            report["errors"].append(f"stopped after {BOROUGH_BUDGET_S}s with {len(queue)} links left")
            break
        url, depth, why, parent = queue.pop(0)
        url = url.split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        is_doc_url = bool(DOC_URL_RX.search(url))
        if (is_doc_url and docs >= MAX_DOCS) or (not is_doc_url and pages >= MAX_PAGES):
            continue
        try:
            raw = fetch(fx, url)
        except Exception as e:
            report["errors"].append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
            continue
        kind = sniff(raw, url)
        title = why
        links, page_text = [], ""
        try:
            if kind == "html":
                pages += 1
                text = decode(raw)
                if len(text) < 60000 and BLOCKED_RX.search(text[:20000]):
                    report["errors"].append(f"{url}: blocked by the site's bot protection")
                    continue
                title = page_title(text) or why
                rows, links, page_text = rows_from_html(text)
                if len(page_text.splitlines()) < 5:
                    report["errors"].append(f"{url}: page is empty without JavaScript (or behind a bot check)")
                # Interactive maps (e.g. Greenwich) keep their figures in a script or JSON file beside the page.
                if re.search(r"catchment|radius|offers.?map|distance", url + " " + title, re.I):
                    for src in re.findall(r'<script[^>]+src=["\']([^"\']+)', text, re.I)[:8]:
                        su = urllib.parse.urljoin(url, html.unescape(src))
                        if same_site(su, url) and not re.search(r"jquery|analytics|gtag|bootstrap|leaflet|cookie", su, re.I):
                            try:
                                js = decode(fetch(fx, su))
                                rows.append(["<table end>"])
                                rows.extend([[x.strip()] for x in re.split(r"[\n;{}\[\]]", js) if x.strip()][:20000])
                            except Exception as e:
                                report["errors"].append(f"{su}: script: {type(e).__name__}")
            else:
                docs += 1
                rows = {"pdf": rows_from_pdf, "xlsx": rows_from_xlsx, "xls": rows_from_xls, "docx": rows_from_docx}[kind](raw)
                title = why if why not in ("seed", "known doc", "link") else urllib.parse.unquote(url.rsplit("/", 1)[-1])
        except Exception as e:
            report["errors"].append(f"{url}: parse {kind}: {type(e).__name__}: {str(e)[:120]}")
            continue
        found, unmatched, meta = process_doc(rows, matcher, title, url, b, context=urllib.parse.unquote(parent))
        with_dist = [f for f in found if f["mi"] is not None]
        entry = {"url": url, "kind": kind, "title": title[:150], "rows": len(rows), "matched": len(found),
                 "with_distance": len(with_dist), **meta}
        if unmatched:
            entry["unmatched_with_distance"] = unmatched[:12]
        if found:
            entry["sample"] = [f"{matcher.by_urn[f['urn']]['name']} -> {f['mi']} ({f['how']}) :: {f['row'][:140]}" for f in found[:10]]
        if not with_dist:
            # Diagnostics for pages/documents that gave nothing: where might the data be?
            if kind == "html":
                entry["links"] = [f"{t[:60]} -> {urllib.parse.urljoin(url, h)[:160]}" for h, t in links
                                  if re.search(r"offer|alloc|distance|places|statist|data|\.pdf|\.xls|download", f"{t} {h}", re.I)][:40]
                m = re.search(r"miles?\b|metres|cut.?off|furthest", page_text, re.I)
                if m:
                    entry["snippet"] = page_text[max(0, m.start() - 300):m.start() + 500]
            if kind != "html" or found:
                entry["head"] = [" | ".join(r)[:160] for r in rows[:40]]
        (report["pages"] if kind == "html" else report["docs"]).append(entry)
        results.extend(found)
        # Follow links
        for href, text in links:
            href = html.unescape(href.strip())
            absu = urllib.parse.urljoin(url, href)
            if not absu.startswith("http") or absu in seen:
                continue
            label = re.sub(r"[-_+]|%20", " ", f"{text} {urllib.parse.unquote(absu)}")
            is_doc = bool(DOC_URL_RX.search(absu))
            if SKIP_RX.search(label) or not (LINK_RX.search(label) or (is_doc and GUIDE_LINK_RX.search(label))):
                continue
            if is_doc:
                queue.append((absu, depth + 1, text or "link", url))
            elif depth < 2 and same_site(absu, url):
                queue.append((absu, depth + 1, text or "link", url))
        time.sleep(0.3)
    report["fetched_pages"], report["fetched_docs"] = pages, docs
    return results, report


MIN_YEAR = THIS_YEAR - 3  # older cut-offs say little about today's demand


def pick_best(records):
    """One record per (urn, entry): newest year, then from the document with most matches, with a distance."""
    doc_matches = collections.Counter(r["src"] for r in records if r["mi"] is not None)
    best = {}
    for r in records:
        if r["mi"] is None or (r["year"] is not None and r["year"] < MIN_YEAR):
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
            by_la[int(r[c["la"]])].append({"urn": r[c["urn"]], "name": r[c["name"]], "ph": r[c["ph"]], "sel": r[c["sel"]]})

    fx = Fetcher(args.cache)
    all_best, reports = [], []
    lock = threading.Lock()
    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    todo = [b for b in cfg["boroughs"] if not only or b["name"].lower() in only]

    def write_outputs():
        out = collections.defaultdict(list)
        for r in all_best:
            out[str(r["urn"])].append({k: r[k] for k in ("entry", "year", "mi", "basis", "row", "src", "borough")})
        with open(os.path.join(OUT, "catchment.json"), "w", encoding="utf-8") as f:
            json.dump({"built": dt.date.today().isoformat(), "region": args.region, "schools": out}, f,
                      ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(OUT, "catchment_report.json"), "w", encoding="utf-8") as f:
            json.dump(sorted(reports, key=lambda r: r["name"]), f, ensure_ascii=False, indent=1)
        return len(out)

    def run(b):
        t0 = time.time()
        schools = [s for la in b["las"] for s in by_la.get(la, [])]
        matcher = Matcher(schools)
        try:
            records, rep = crawl_borough(fx, b, matcher)
        except Exception as e:
            records, rep = [], {"name": b["name"], "errors": [f"crawl failed: {type(e).__name__}: {e}"]}
        best = pick_best(records)
        rep.update({"schools_with_cutoff": len(best), "schools_total": len(schools),
                    "years": dict(collections.Counter(r["year"] for r in best)), "seconds": round(time.time() - t0)})
        with lock:
            all_best.extend(best)
            reports.append(rep)
            n = write_outputs()  # after every borough, so a partial run still leaves usable output
        log(f"== {b['name']}: {len(best)}/{len(schools)} schools with cut-offs; years {rep['years']}; "
            f"pages {rep.get('fetched_pages')}, docs {rep.get('fetched_docs')}, errors {len(rep['errors'])}; "
            f"{rep['seconds']}s (running total {n})")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, todo))
    log(f"\nWrote cut-off distances for {write_outputs()} schools across {len(reports)} boroughs")


if __name__ == "__main__":
    main()
