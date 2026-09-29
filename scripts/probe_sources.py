"""Temporary: print the shape of each upstream data source so the build script can target it."""
import csv, datetime, io, json, re, sys, urllib.request, zipfile

UA = {"User-Agent": "Mozilla/5.0 (school-map data probe)"}

def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
        return data if binary else data.decode("utf-8", "replace")

def show_csv(raw, label, n=2):
    text = raw.decode("cp1252", "replace") if isinstance(raw, bytes) else raw
    rows = list(csv.reader(io.StringIO(text)))
    print(f"--- {label}: {len(rows)} rows")
    print("HEADERS:", rows[0])
    for r in rows[1:1 + n]:
        print("ROW:", r)

def section(t):
    print("\n" + "=" * 20, t, "=" * 20, flush=True)

def safe(fn):
    try:
        fn()
    except Exception as e:
        print("ERROR", type(e).__name__, e)

def gias():
    section("GIAS")
    for i in range(0, 6):
        d = (datetime.date.today() - datetime.timedelta(days=i)).strftime("%Y%m%d")
        url = f"https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/edubasealldata{d}.csv"
        try:
            raw = get(url, binary=True)
            print("OK", url, len(raw))
            show_csv(raw, "gias", 1)
            return
        except Exception as e:
            print("miss", url, e)

def ofsted():
    section("OFSTED gov.uk content api")
    for slug in ["government/statistical-data-sets/monthly-management-information-ofsteds-school-inspections-outcomes"]:
        j = json.loads(get("https://www.gov.uk/api/content/" + slug))
        atts = j.get("details", {}).get("attachments", [])
        print("attachments:", len(atts))
        for a in atts[:15]:
            print(" ", a.get("title"), "|", a.get("url"), "|", a.get("content_type"))
        docs = j.get("links", {}).get("documents", [])
        for dct in docs[:5]:
            print(" doc:", dct.get("title"), dct.get("base_path"))
        csvs = [a for a in atts if str(a.get("url", "")).lower().endswith(".csv")]
        for a in csvs[:3]:
            safe(lambda: show_csv(get(a["url"], binary=True), a.get("title"), 2))

def ees():
    section("EES")
    base = "https://content.explore-education-statistics.service.gov.uk/api"
    for slug in ["secondary-and-primary-school-applications-and-offers", "key-stage-4-performance",
                 "key-stage-2-attainment", "a-level-and-other-16-to-18-results", "school-performance-tables",
                 "schools-pupils-and-their-characteristics"]:
        print("\n## ", slug)
        try:
            j = json.loads(get(f"{base}/publications/{slug}/releases/latest"))
        except Exception as e:
            print("ERR", e); continue
        print("keys:", list(j.keys())[:40])
        print("id:", j.get("id"), "title:", j.get("title"), "slug:", j.get("slug"))
        for f in j.get("downloadFiles", [])[:40]:
            print("  file:", f.get("id"), "|", f.get("fileName"), "|", f.get("name"), "|", f.get("size"))

def ees_api():
    section("EES public API")
    for q in ["applications and offers", "key stage 4", "key stage 2", "16 to 18"]:
        safe(lambda: print(q, "=>", [(p["id"], p["title"], p["slug"]) for p in json.loads(get(
            "https://api.education.gov.uk/statistics/v1/publications?search=" + urllib.parse.quote(q))).get("results", [])][:8]))

def cscp():
    section("Compare school performance")
    html = get("https://www.compare-school-performance.service.gov.uk/download-data")
    print(len(html))
    for m in sorted(set(re.findall(r'(?:href|action|value|name)="([^"]{1,200})"', html)))[:200]:
        print(" ", m)

import urllib.parse
for f in [gias, ofsted, ees, ees_api, cscp]:
    safe(f)
