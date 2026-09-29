"""Temporary: print the shape of each upstream data source so the build script can target it."""
import csv, datetime, io, json, re, sys, urllib.request, urllib.parse, zipfile, collections

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
      "Accept": "text/html,application/json,*/*", "Accept-Language": "en-GB,en;q=0.9"}

def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
        return data if binary else data.decode("utf-8", "replace")

def rows_of(raw):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    return list(csv.reader(io.StringIO(text)))

def section(t):
    print("\n" + "=" * 20, t, "=" * 20, flush=True)

def safe(fn):
    try:
        fn()
    except Exception as e:
        import traceback; traceback.print_exc(); print("ERROR", type(e).__name__, e, flush=True)

def ofsted():
    section("OFSTED latest")
    j = json.loads(get("https://www.gov.uk/api/content/government/statistical-data-sets/monthly-management-information-ofsteds-school-inspections-outcomes"))
    atts = j["details"]["attachments"]
    cands = [a for a in atts if "latest inspections" in a.get("title", "").lower() and a["url"].lower().endswith(".csv")]
    print("latest-inspection csvs:", len(cands))
    for a in cands[-6:]:
        print("  ", a["title"], a["url"])
    # position in list of the newest
    for i, a in enumerate(atts):
        if "2026" in a.get("title", ""):
            print("  idx", i, a["title"], a["url"][-60:])
    a = [c for c in cands if "2026" in c["title"]]
    a = a[-1] if a else cands[-1]
    for c in cands:
        if "31 August 2026" in c["title"] or "31 Aug 2026" in c["title"]:
            a = c
    print("USING", a["title"])
    rows = rows_of(get(a["url"], binary=True))
    hi = 0
    while "URN" not in rows[hi]:
        hi += 1
    hdr = rows[hi]
    print("header row index", hi, "n cols", len(hdr), "n rows", len(rows) - hi - 1)
    for i, h in enumerate(hdr):
        vals = collections.Counter(r[i] for r in rows[hi + 1:] if i < len(r))
        print(f"  [{i}] {h!r}: {vals.most_common(6)}")

def ees():
    section("EES admissions")
    slug = "primary-and-secondary-school-applications-and-offers"
    html = get(f"https://explore-education-statistics.service.gov.uk/find-statistics/{slug}")
    nd = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S).group(1))
    pp = nd["props"]["pageProps"]
    print("pageProps keys:", list(pp.keys()))
    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("fileName", "fileId", "dataSetFileId", "subjectId", "downloadFiles", "dataSets", "name") and not isinstance(v, (dict, list)):
                    print("  ", path + "." + k, "=", str(v)[:150])
                walk(v, path + "." + k)
        elif isinstance(o, list):
            for i, v in enumerate(o[:60]):
                walk(v, f"{path}[{i}]")
    walk(pp)
    rv = pp["releaseVersionSummary"]["id"]
    base = "https://content.explore-education-statistics.service.gov.uk/api"
    for path in [f"/releases/{rv}/files", f"/release-versions/{rv}/files", f"/releases/{rv}/data-sets",
                 f"/publications/{slug}/releases/latest", f"/releases/{rv}", f"/publications/{slug}/releases/2026-27/data-sets"]:
        try:
            b = get(base + path, binary=True)
            print("OK", path, len(b), b[:4])
            if b[:2] == b"PK":
                z = zipfile.ZipFile(io.BytesIO(b))
                for n in z.namelist():
                    print("    zip:", n, z.getinfo(n).file_size)
                for n in z.namelist():
                    if "school" in n.lower() and n.lower().endswith(".csv"):
                        rows = rows_of(z.read(n))
                        print("  ", n, len(rows), rows[0])
                        for r in rows[1:4]:
                            print("     ", r)
            else:
                print(b[:800])
        except Exception as e:
            print("ERR", path, e)
    try:
        dc = get(f"https://explore-education-statistics.service.gov.uk/data-catalogue?publicationSlug={slug}&releaseSlug=2026-27")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', dc, re.S)
        print("catalogue NEXT_DATA:", m.group(1)[:2500] if m else "none")
    except Exception as e:
        print("ERR catalogue", e)

def cscp():
    section("Compare school performance")
    base = "https://www.compare-school-performance.service.gov.uk/download-data?download=true&regions=0&filters={f}&fileformat=csv&year={y}&meta={m}"
    for f in ["KS2", "KS4", "KS5"]:
        for y in ["2025-2026", "2024-2025", "2023-2024"]:
            try:
                b = get(base.format(f=f, y=y, m="false"), binary=True)
                rows = rows_of(b)
                print(f"{f} {y}: {len(b)} bytes, {len(rows)} rows, head={b[:60]!r}")
                if len(rows) > 100:
                    print("   HEADERS:", rows[0])
                    r = rows[1]
                    print("   ROW1:", dict(zip(rows[0], r)))
                    try:
                        mb = get(base.format(f=f, y=y, m="true"), binary=True)
                        mrows = rows_of(mb)
                        print("   META rows", len(mrows), mrows[0])
                        keep = re.compile(r"^(PTRWM|READ_AVERAGE|MAT_AVERAGE|GPS_AVERAGE|READPROG|MATPROG|WRITPROG|ATT8SCR$|P8MEA$|PTL2BASICS|PTEBACC|EBACCAPS$|TALLPPE|TALLPPEGRD|PTAAB|TPUP|TELIG|RECTYPE|NFTYPE|PT.*DEST)")
                        for mr in mrows[1:]:
                            if mr and keep.match(mr[1] if len(mr) > 1 else mr[0]) or (mr and keep.match(mr[0])):
                                print("     META", mr[:4])
                    except Exception as e:
                        print("   meta ERR", e)
                    break
            except Exception as e:
                print(f"{f} {y} ERR {e}")

for f in [cscp, ofsted, ees]:
    safe(f)
