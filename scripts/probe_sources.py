"""Temporary: print the shape of each upstream data source so the build script can target it."""
import csv, datetime, io, json, re, sys, urllib.request, urllib.parse, zipfile

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
      "Accept": "text/html,application/json,*/*", "Accept-Language": "en-GB,en;q=0.9"}

def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
        return data if binary else data.decode("utf-8", "replace")

def show_csv(raw, label, n=2, skip=0):
    text = raw.decode("utf-8-sig", "replace") if isinstance(raw, bytes) else raw
    rows = list(csv.reader(io.StringIO(text)))
    print(f"--- {label}: {len(rows)} rows")
    for r in rows[:2 + n]:
        print("ROW:", r[:120])

def section(t):
    print("\n" + "=" * 20, t, "=" * 20, flush=True)

def safe(fn):
    try:
        fn()
    except Exception as e:
        print("ERROR", type(e).__name__, e)

def ofsted():
    section("OFSTED")
    j = json.loads(get("https://www.gov.uk/api/content/government/statistical-data-sets/monthly-management-information-ofsteds-school-inspections-outcomes"))
    atts = j.get("details", {}).get("attachments", [])
    recent = [a for a in atts if re.search(r"202[4-6]", a.get("title", ""))]
    for a in recent[:40]:
        print(" ", a.get("title"), "|", a.get("url"), "|", a.get("content_type"), "|", a.get("id"))
    print("first 3 raw keys:", list(atts[0].keys()))
    print("links keys:", list(j.get("links", {}).keys()))
    for k, v in j.get("links", {}).items():
        for x in v[:8]:
            print("  link", k, x.get("title"), x.get("base_path"))
    body = j.get("details", {}).get("body", "")
    print("BODY excerpt:", re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", body))[:1500])
    csvs = [a for a in recent if str(a.get("url", "")).lower().endswith(".csv")]
    for a in csvs[:2]:
        safe(lambda: show_csv(get(a["url"], binary=True), a.get("title"), 3))

def ees_content():
    section("EES content api variants")
    base = "https://content.explore-education-statistics.service.gov.uk/api"
    slug = "secondary-and-primary-school-applications-and-offers"
    for path in [f"/publications/{slug}", f"/publications/{slug}/releases/latest", f"/publications/{slug}/releases",
                 f"/publications/{slug}/release-series", f"/publications/{slug}/releases/latest/summary",
                 f"/publications/{slug}/summary", f"/publications/{slug}/title", f"/publications/{slug}/releases/latest/files"]:
        try:
            t = get(base + path)
            print("OK", path, len(t), t[:600])
        except Exception as e:
            print("ERR", path, e)
    try:
        html = get(f"https://explore-education-statistics.service.gov.uk/find-statistics/{slug}")
        print("find-statistics html", len(html))
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            nd = json.loads(m.group(1))
            s = json.dumps(nd)
            print("NEXT_DATA len", len(s))
            for mm in re.finditer(r'"(id|releaseId|fileName|name|dataSetFileId|subjectId)":\s*"([^"]{1,120})"', s):
                pass
            print(s[:3000])
        else:
            print("no NEXT_DATA; links:", sorted(set(re.findall(r'href="([^"]*(?:download|files|data-catalogue|releases)[^"]*)"', html)))[:60])
            print("uuids:", sorted(set(re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', html)))[:40])
    except Exception as e:
        print("ERR find-statistics", e)

def ees_api():
    section("EES public API data sets")
    base = "https://api.education.gov.uk/statistics/v1"
    pubs = {}
    page = 1
    while True:
        j = json.loads(get(f"{base}/publications?page={page}&pageSize=40"))
        for p in j["results"]:
            pubs[p["slug"]] = p["id"]
        if page >= j["paging"]["totalPages"]:
            break
        page += 1
    print("publications with API data:", len(pubs))
    for s in sorted(pubs):
        print("  pub", s)
    for slug in ["key-stage-4-performance", "key-stage-2-attainment", "a-level-and-other-16-to-18-results",
                 "secondary-and-primary-school-applications-and-offers", "school-pupils-and-their-characteristics"]:
        if slug not in pubs:
            print("NOT IN API:", slug); continue
        j = json.loads(get(f"{base}/publications/{pubs[slug]}/data-sets?pageSize=40"))
        print("\n##", slug)
        for d in j["results"]:
            lv = d.get("latestVersion", {})
            print("  ds", d["id"], "|", d["title"], "|", lv.get("version"), "|", lv.get("geographicLevels"), "|", lv.get("timePeriods"))

def cscp():
    section("Compare school performance")
    for url in ["https://www.compare-school-performance.service.gov.uk/download-data",
                "https://www.compare-school-performance.service.gov.uk/",
                "https://www.find-school-performance-data.service.gov.uk/",
                "https://www.compare-school-performance.service.gov.uk/download-data?download=true&regions=0&filters=KS2&fileformat=csv&year=2023-2024&meta=false"]:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
                print("OK", url, r.status, r.headers.get("content-type"), len(b), r.geturl())
                print(b[:300])
        except urllib.error.HTTPError as e:
            print("HTTP", url, e.code, dict(e.headers).get("Server"), e.read()[:300])
        except Exception as e:
            print("ERR", url, e)

for f in [ofsted, ees_content, ees_api, cscp]:
    safe(f)
