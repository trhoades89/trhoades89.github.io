/* England School Map — static front end. Data is built by scripts/build_school_data.py. */
(function () {
  "use strict";

  // ---------- Constants ----------
  const PHASES = [
    ["N", "Nursery"], ["P", "Primary"], ["S", "Secondary"], ["A", "All-through"],
    ["C", "16–19"], ["X", "Special"], ["R", "Alternative provision"],
  ];
  const SECTORS = [["m", "LA maintained"], ["a", "Academy / free school"], ["i", "Independent"], ["c", "College"]];
  const OFSTED = [
    [1, "Outstanding", "--r-outstanding"], [2, "Good", "--r-good"],
    [3, "Requires improvement", "--r-ri"], [4, "Inadequate", "--r-inadequate"],
    [5, "Newer inspection, no single grade", "--r-new"],
    [0, "Not yet inspected / no data", "--r-none"],
  ];
  const RAMP = ["#d4e4f7", "#9cc3ec", "#5c9bdc", "#2b6cc0", "#123f82"];
  const NO_DATA = "#b5bcc3";
  const METRICS = {
    ks2: { col: "ks2", label: "% meeting expected standard", fmt: (v) => v + "%" },
    ks4: { col: "ks4", label: "Attainment 8", fmt: (v) => v.toFixed(1) },
    ks5: { col: "ks5", label: "A level avg points per entry", fmt: (v) => v.toFixed(1) },
    demand: { col: "dem", label: "1st preferences per offer", fmt: (v) => v.toFixed(2) },
  };

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- State ----------
  const state = {
    data: null,          // raw JSON
    S: [],               // school objects
    byUrn: new Map(),
    visible: [],         // indices passing filters
    phases: new Set(PHASES.map((p) => p[0]).filter((p) => p !== "N")),
    ofsted: new Set(OFSTED.map((o) => o[0])),
    sectors: new Set(SECTORS.map((s) => s[0])),
    grammar: false, noFaith: false, mixed: false,
    colourBy: "ofsted",
    breaks: {},          // metric -> quantile breaks
    selected: null,
    home: null,          // {lat,lng,label}
    detailCache: new Map(),
    cutoffs: {},         // urn -> council-published last distance offered records (where collected)
  };

  // ---------- Map ----------
  const map = L.map("map", { zoomControl: true, minZoom: 6, maxZoom: 18, worldCopyJump: false })
    .fitBounds([[49.9, -6.4], [55.8, 1.8]]);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · School data: DfE &amp; Ofsted (OGL v3.0)',
  }).addTo(map);

  const catchmentLayer = L.layerGroup().addTo(map);
  let homeMarker = null;

  // Canvas layer drawing every visible school as a dot, with zoom animation support.
  const DotLayer = L.Layer.extend({
    onAdd(m) {
      this._canvas = L.DomUtil.create("canvas", "leaflet-zoom-animated");
      this._canvas.style.pointerEvents = "none";
      m.getPanes().overlayPane.appendChild(this._canvas);
      m.on("moveend resize", this.redraw, this);
      m.on("zoomanim", this._animateZoom, this);
      this.redraw();
    },
    onRemove(m) {
      L.DomUtil.remove(this._canvas);
      m.off("moveend resize", this.redraw, this);
      m.off("zoomanim", this._animateZoom, this);
    },
    _animateZoom(e) {
      const scale = this._map.getZoomScale(e.zoom);
      const offset = this._map._latLngBoundsToNewLayerBounds(this._map.getBounds(), e.zoom, e.center).min;
      L.DomUtil.setTransform(this._canvas, offset, scale);
    },
    redraw() {
      const m = this._map;
      if (!m) return;
      const size = m.getSize();
      const dpr = window.devicePixelRatio || 1;
      const c = this._canvas;
      L.DomUtil.setPosition(c, m.containerPointToLayerPoint([0, 0]));
      c.width = size.x * dpr; c.height = size.y * dpr;
      c.style.width = size.x + "px"; c.style.height = size.y + "px";
      const ctx = c.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const z = m.getZoom();
      const r = z <= 7 ? 1.8 : z <= 9 ? 2.8 : z <= 11 ? 4.2 : z <= 13 ? 5.5 : 7;
      const b = m.getBounds().pad(0.05);
      const pts = [];
      // Draw no-data first so rated schools sit on top.
      const order = state.visible.slice().sort((a, b2) => rank(state.S[a]) - rank(state.S[b2]));
      ctx.lineWidth = r > 3 ? 1 : 0.5;
      ctx.strokeStyle = "rgba(20,30,40,0.55)";
      for (const i of order) {
        const s = state.S[i];
        if (!b.contains([s.lat, s.lng])) continue;
        const p = m.latLngToContainerPoint([s.lat, s.lng]);
        pts.push(i, p.x, p.y);
        ctx.beginPath();
        ctx.arc(p.x, p.y, r, 0, 6.2832);
        ctx.fillStyle = colourOf(s);
        ctx.fill();
        if (r > 2) ctx.stroke();
      }
      if (state.selected) {
        const s = state.selected;
        const p = m.latLngToContainerPoint([s.lat, s.lng]);
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 5, 0, 6.2832);
        ctx.lineWidth = 3; ctx.strokeStyle = css("--accent"); ctx.stroke();
      }
      this._pts = pts; this._r = r;
    },
    hit(containerPoint) {
      const pts = this._pts || [];
      const tol = Math.max(this._r + 4, 8);
      let best = -1, bestD = tol * tol;
      for (let k = 0; k < pts.length; k += 3) {
        const dx = pts[k + 1] - containerPoint.x, dy = pts[k + 2] - containerPoint.y;
        const d = dx * dx + dy * dy;
        if (d <= bestD) { bestD = d; best = pts[k]; }
      }
      return best;
    },
  });
  const dots = new DotLayer().addTo(map);

  const tip = L.tooltip({ direction: "top", offset: [0, -6], className: "school-tip" });
  map.on("mousemove", (e) => {
    const i = dots.hit(e.containerPoint);
    map.getContainer().style.cursor = i >= 0 ? "pointer" : "";
    if (i >= 0) {
      const s = state.S[i];
      tip.setLatLng([s.lat, s.lng]).setContent(esc(s.name) + " · " + esc(ratingLabel(s)));
      if (!map.hasLayer(tip)) tip.addTo(map);
    } else if (map.hasLayer(tip)) {
      map.removeLayer(tip);
    }
  });
  map.on("click", (e) => {
    const i = dots.hit(e.containerPoint);
    if (i >= 0) selectSchool(state.S[i]);
  });

  // ---------- Colour ----------
  const ofstedRow = (o) => OFSTED.find((x) => x[0] === o) || OFSTED[OFSTED.length - 1];
  function ofstedName(o) { return ofstedRow(o)[1]; }
  // Label for a specific school: independents aren't in Ofsted's state-funded dataset.
  const ratingLabel = (s) => (s.o === 0 && s.sec === "i" ? "Independent (inspected separately)" : ofstedName(s.o));
  function ofstedColour(o) { return css(ofstedRow(o)[2]); }
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDate(d) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || "");
    return m ? `${+m[3]} ${MONTHS[+m[2] - 1]} ${m[1]}` : d || "";
  }
  let ofstedColours = {};
  function refreshColours() { ofstedColours = Object.fromEntries(OFSTED.map((o) => [o[0], css(o[2])])); }

  function rank(s) {
    if (state.colourBy === "ofsted") return s.o === 0 ? -1 : s.o === 5 ? 0.5 : 5 - s.o;
    const v = s[METRICS[state.colourBy].col];
    return v == null ? -1 : v;
  }
  function colourOf(s) {
    if (state.colourBy === "ofsted") return ofstedColours[s.o] || NO_DATA;
    const v = s[METRICS[state.colourBy].col];
    if (v == null) return NO_DATA;
    const br = state.breaks[state.colourBy];
    let k = 0;
    while (k < br.length && v > br[k]) k++;
    return RAMP[k];
  }
  function computeBreaks() {
    for (const [key, m] of Object.entries(METRICS)) {
      const vals = state.S.map((s) => s[m.col]).filter((v) => v != null).sort((a, b) => a - b);
      if (!vals.length) { state.breaks[key] = []; continue; }
      const q = (p) => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))];
      state.breaks[key] = [q(0.2), q(0.4), q(0.6), q(0.8)];
    }
  }

  function renderLegend() {
    const el = $("legend");
    if (state.colourBy === "ofsted") {
      el.innerHTML = OFSTED.map((o) => `<span><i style="background:${ofstedColours[o[0]]}"></i>${o[1]}</span>`).join("");
      return;
    }
    const m = METRICS[state.colourBy];
    const br = state.breaks[state.colourBy] || [];
    if (!br.length) { el.innerHTML = '<span class="muted">No data available yet for this measure.</span>'; return; }
    const labels = [
      `≤ ${m.fmt(br[0])}`, `${m.fmt(br[0])}–${m.fmt(br[1])}`, `${m.fmt(br[1])}–${m.fmt(br[2])}`,
      `${m.fmt(br[2])}–${m.fmt(br[3])}`, `> ${m.fmt(br[3])}`,
    ];
    el.innerHTML = labels.map((l, k) => `<span><i style="background:${RAMP[k]}"></i>${l}</span>`).join("") +
      `<span><i style="background:${NO_DATA}"></i>No data</span>` +
      `<span class="muted" style="flex-basis:100%">${esc(m.label)}; each colour covers a fifth of schools with data.</span>`;
  }

  // ---------- Filters ----------
  function passes(s) {
    if (!state.phases.has(s.ph)) return false;
    if (!state.sectors.has(s.sec)) return false;
    if (!state.ofsted.has(s.o)) return false;
    if (state.grammar && !s.sel) return false;
    if (state.noFaith && s.faith) return false;
    if (state.mixed && s.g !== "M") return false;
    return true;
  }
  function applyFilters() {
    state.visible = [];
    state.S.forEach((s, i) => { if (passes(s)) state.visible.push(i); });
    $("countLine").textContent = `${state.visible.length.toLocaleString()} of ${state.S.length.toLocaleString()} schools shown`;
    dots.redraw();
    if (state.home) renderNearby();
  }

  function buildChips(container, items, set, colourFn) {
    const el = $(container);
    el.innerHTML = "";
    for (const [key, label] of items) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip";
      b.setAttribute("aria-pressed", set.has(key) ? "true" : "false");
      b.innerHTML = (colourFn ? `<span class="dot" style="background:${colourFn(key)}"></span>` : "") + esc(label);
      b.addEventListener("click", () => {
        if (set.has(key)) set.delete(key); else set.add(key);
        b.setAttribute("aria-pressed", set.has(key) ? "true" : "false");
        applyFilters();
      });
      el.appendChild(b);
    }
  }

  // ---------- Geo helpers ----------
  function distKm(a, b) {
    const R = 6371, toR = Math.PI / 180;
    const dLat = (b.lat - a.lat) * toR, dLng = (b.lng - a.lng) * toR;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * toR) * Math.cos(b.lat * toR) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  const fmtDist = (km) => (km < 1 ? Math.round(km * 1000) + " m" : km.toFixed(km < 10 ? 1 : 0) + " km") +
    ` (${(km * 0.621371).toFixed(1)} mi)`;

  // Approximate catchment: the Voronoi cell of this school among nearby open state schools of the same phase.
  function catchmentPolygon(s) {
    const peers = state.S.filter((o) =>
      o.sec !== "i" && samePhaseGroup(o.ph, s.ph) && Math.abs(o.lat - s.lat) < 0.25 && Math.abs(o.lng - s.lng) < 0.4);
    if (!peers.includes(s)) peers.push(s);
    const k = Math.cos(s.lat * Math.PI / 180);
    const pts = peers.map((o) => [(o.lng - s.lng) * k * 111.32, (o.lat - s.lat) * 110.57]);
    const idx = peers.indexOf(s);
    const R = peers.length > 1 ? 30 : 3; // km box
    const vor = d3.Delaunay.from(pts).voronoi([-R, -R, R, R]);
    const cell = vor.cellPolygon(idx);
    if (!cell) return null;
    return { ring: cell.map(([x, y]) => [s.lat + y / 110.57, s.lng + x / (k * 111.32)]), peers: peers.length };
  }
  function samePhaseGroup(a, b) {
    const g = (p) => (p === "P" ? "P" : p === "S" || p === "A" ? "S" : p);
    return g(a) === g(b) || (b === "A" && (a === "P" || a === "S"));
  }

  // Council-published "last distance offered" figures for a school, most relevant entry first.
  function cutoffsFor(s) {
    const recs = state.cutoffs[s.urn] || [];
    const order = { "Reception": 0, "Year 3": 1, "Year 7": 2 };
    return recs.slice().sort((a, b) => (s.ph === "S" ? -1 : 1) * ((order[a.entry] ?? 3) - (order[b.entry] ?? 3)));
  }
  const MI_M = 1609.344;
  const fmtMi = (mi) => `${mi.toFixed(2)} mi (${Math.round(mi * MI_M).toLocaleString()} m)`;
  const isStraight = (r) => /straight/i.test(r.basis || "");
  function homeVsCutoff(s, r) {
    if (!state.home) return null;
    const mi = distKm(state.home, s) * 0.621371;
    if (!isStraight(r)) return { mi, inside: null };
    return { mi, inside: mi <= r.mi };
  }

  // ---------- Details panel ----------
  async function loadDetail(s) {
    if (!state.detailCache.has(s.la)) {
      state.detailCache.set(s.la, fetch(`data/la/${s.la}.json`).then((r) => (r.ok ? r.json() : {})).catch(() => ({})));
    }
    const la = await state.detailCache.get(s.la);
    return la[s.urn] || {};
  }

  const isNarrow = () => window.matchMedia("(max-width: 820px)").matches;
  function setSidebar(open) {
    $("sidebar").classList.toggle("collapsed", !open);
    $("toggleSidebar").setAttribute("aria-expanded", String(open));
  }

  async function selectSchool(s, opts = {}) {
    if (isNarrow()) setSidebar(false);
    state.selected = s;
    catchmentLayer.clearLayers();
    history.replaceState(null, "", "#urn=" + s.urn);
    $("panel").hidden = false;
    $("panelBody").innerHTML = header(s) + '<p class="muted">Loading details…</p>';
    map.invalidateSize();
    if (opts.pan !== false) {
      const target = map.getZoom() < 13 ? 14 : map.getZoom();
      map.setView([s.lat, s.lng], target, { animate: true });
    }
    dots.redraw();
    const d = await loadDetail(s);
    if (state.selected !== s) return;
    $("panelBody").innerHTML = header(s, d) + body(s, d);
    wirePanel(s);
    if (opts.catchment) toggleCatchment(s);
  }

  function header(s, d = {}) {
    const o = d.ofsted || {};
    const when = s.o === 5 ? (o.rc ? o.rc.date : o.date) : o.oeDate;
    const label = s.o === 5 ? (o.rc ? "Ofsted report card" : "Ofsted: no overall grade") : ratingLabel(s);
    const rating = `<span class="badge rating" style="background:${ofstedColour(s.o)}">${esc(label)}${when ? " · " + esc(when.slice(0, 4)) : ""}</span>`;
    const phase = (PHASES.find((p) => p[0] === s.ph) || [0, ""])[1];
    const bits = [phase, state.data.lk.type[s.t], s.g === "B" ? "Boys" : s.g === "G" ? "Girls" : null,
      s.sel ? "Selective (grammar)" : null, s.faith ? state.data.lk.faith[s.faith] : null].filter(Boolean);
    let dist = "";
    if (state.home) dist = `<p class="meta">${fmtDist(distKm(state.home, s))} from ${esc(state.home.label)} (straight line)</p>`;
    return `<h2>${esc(s.name)}</h2>
      <p class="meta">${esc(d.addr || "")}</p>${dist}
      <div class="badges">${rating}${bits.map((b) => `<span class="badge">${esc(b)}</span>`).join("")}</div>`;
  }

  function kv(rows) {
    const html = rows.filter((r) => r && r[1] != null && r[1] !== "").map(([k, v, nat, barMax, unit = ""]) => {
      let bar = "";
      if (barMax && typeof v === "number") {
        const pct = (x) => Math.max(0, Math.min(100, (x / barMax) * 100));
        bar = `<div class="bar" aria-hidden="true"><b style="width:${pct(v)}%"></b>${nat != null ? `<i style="left:${pct(nat)}%"></i>` : ""}</div>`;
      }
      const shown = (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v) + unit;
      return `<dt>${esc(k)}</dt><dd>${esc(shown)}${nat != null ? `<span class="nat">England ${esc(nat + unit)}</span>` : ""}</dd>${bar}`;
    }).join("");
    return html ? `<dl class="kv">${html}</dl>` : "";
  }

  function body(s, d) {
    const out = [];
    out.push(`<div class="actions">
      <button type="button" id="btnCatch" class="secondary">${cutoffsFor(s).length ? "Show catchment (last distance offered)" : "Show approximate catchment"}</button>
      ${d.web ? `<a class="btn" href="${esc(webUrl(d.web))}" target="_blank" rel="noopener">School website</a>` : ""}
      <a class="btn" href="https://get-information-schools.service.gov.uk/Establishments/Establishment/Details/${s.urn}" target="_blank" rel="noopener">GIAS record</a>
    </div><div id="catchNote"></div>`);

    // Ofsted
    const o = d.ofsted;
    if (o) {
      const rows = [];
      if (o.rc) {
        out.push(`<section class="sec"><h3>Ofsted report card · ${esc(fmtDate(o.rc.date))}</h3>
          <table class="grades">${o.rc.grades.map(([k, v]) => `<tr><td>${esc(k)}</td><td><span class="grade g-${esc(gradeClass(v))}">${esc(v)}</span></td></tr>`).join("")}</table>
          <p class="note">Report cards (from November 2025) grade each area on a five-point scale from <em>Urgent improvement</em> to <em>Exceptional</em>. There is no single overall grade.</p>
          ${o.rc.note ? `<p class="note">${esc(o.rc.note)}</p>` : ""}</section>`);
      }
      if (o.date) {
        rows.push(["Overall effectiveness", o.oe || "Not given (2024–25 inspection)"]);
        rows.push(["Inspection date", fmtDate(o.date)]);
        for (const [k, v] of o.sub || []) rows.push([k, v]);
      }
      if (o.ung) rows.push(["Latest ungraded inspection", `${o.ung.outcome || "Outcome not recorded"} (${fmtDate(o.ung.date)})`]);
      if (rows.length) out.push(`<section class="sec"><h3>${o.rc ? "Previous graded inspection" : "Ofsted"}</h3>${kv(rows)}
        ${o.note ? `<p class="note">${esc(o.note)}</p>` : ""}`);
      else out.push(`<section class="sec">`);
      out.push(`<p><a href="https://reports.ofsted.gov.uk/provider/21/${s.urn}" target="_blank" rel="noopener">Read the inspection reports on Ofsted’s site</a></p></section>`);
    } else if (s.sec === "i") {
      out.push(`<section class="sec"><h3>Inspection</h3><p>Independent school${d.inspectorate ? ` inspected by ${esc(d.inspectorate)}` : ""}. Ratings for independent schools aren’t included in Ofsted’s state-funded dataset.</p>
        <p><a href="https://reports.ofsted.gov.uk/search?q=${s.urn}" target="_blank" rel="noopener">Search Ofsted reports</a>${d.inspectorate && /ISI|Independent Schools Inspectorate/i.test(d.inspectorate) ? ` · <a href="https://www.isi.net/" target="_blank" rel="noopener">Independent Schools Inspectorate</a>` : ""}</p></section>`);
    } else {
      out.push(`<section class="sec"><h3>Ofsted</h3><p class="muted">No published inspection yet under this URN, which is common for new schools and academy conversions.</p></section>`);
    }

    // Council cut-off distances
    const cuts = cutoffsFor(s);
    if (cuts.length) {
      out.push(`<section class="sec"><h3>Catchment: last distance offered</h3>${cuts.map((r) => {
        const hv = homeVsCutoff(s, r);
        const you = !hv ? "" : hv.inside === null
          ? `<p class="muted">Your postcode is ${hv.mi.toFixed(2)} mi away in a straight line. The council measures by ${esc(r.basis)}, which is usually longer.</p>`
          : `<p class="${hv.inside ? "ok" : "warn"}">${hv.inside ? "✓ Inside" : "✗ Outside"} the ${esc(r.year || "")} cut-off: your postcode is ${hv.mi.toFixed(2)} mi away.</p>`;
        return `<h4>${esc(r.entry)}${r.year ? ` · offers in ${esc(r.year)}` : ""}</h4>
          <dl class="kv"><dt>Furthest distance offered</dt><dd>${fmtMi(r.mi)}</dd><dt>Measured by</dt><dd>${esc(r.basis || "not stated")}</dd></dl>${you}
          <p class="src">Council table row: “${esc(r.row)}”<br><a href="${esc(r.src)}" target="_blank" rel="noopener">Source: ${esc(r.borough)} Council</a></p>`;
      }).join("")}<p class="note">This is how far away the last child offered a place on distance lived. It changes every year with demand, and siblings and other priority groups are admitted first regardless of distance.</p></section>`);
    }

    // Admissions
    const a = d.adm;
    if (a) {
      out.push(`<section class="sec"><h3>Admissions (${esc(a.year)} entry, ${esc(a.entry)})</h3>${kv([
        ["Places offered", a.offers],
        ["First-preference applications", a.first],
        ["All applications (any preference)", a.any],
        ["First preferences per offer", a.ratio != null ? a.ratio : null],
        ["First-preference offers", a.firstOffers],
        ["% of 1st-preference applicants offered", a.pctFirstOffered != null ? a.pctFirstOffered + "%" : null],
        ["Applications from other local authorities", a.fromOtherLA],
      ])}${a.trend ? `<p class="muted" style="margin:8px 0 0">First preferences per offer by year: ${a.trend.map(([y, v]) => `${esc(y)} <strong>${esc(v)}</strong>`).join(" · ")}</p>` : ""}<p class="note">${a.ratio != null ? (a.ratio > 1.2 ? "Oversubscribed: more families put this school first than it had places for. The effective catchment is likely to be small." : a.ratio >= 0.9 ? "Around as many first preferences as offers." : "Fewer first preferences than offers, so places are usually available.") : ""}</p></section>`);
    }

    // Performance
    const perf = [];
    if (d.ks2) perf.push(section("Key stage 2 (end of primary)", d.ks2));
    if (d.ks4) perf.push(section("Key stage 4 (GCSE)", d.ks4));
    if (d.ks5) perf.push(section("16–18 (A level etc.)", d.ks5));
    if (perf.length) out.push(`<section class="sec"><h3>Performance</h3>${perf.join("")}</section>`);

    // About
    out.push(`<section class="sec"><h3>About</h3>${kv([
      ["Ages", d.ages], ["Pupils", d.pupils], ["Capacity", d.cap],
      ["Free school meals (%)", d.fsm != null ? d.fsm + "%" : null],
      ["Admissions policy", d.admPolicy], ["Religious character", d.faith],
      ["Nursery", d.nursery], ["Sixth form", d.sixth], ["Boarding", d.boarders],
      ["Trust", d.trust], ["Local authority", state.data.lk.la[s.la]], ["Head", d.head],
      ["Phone", d.tel], ["URN", String(s.urn)],
    ])}</section>`);
    return out.join("");
  }

  function section(title, p) {
    const rows = (p.m || []).map(([k, v, nat, max]) => [k, v, nat, max]);
    return `<h4>${esc(title)} <span class="muted">· ${esc(p.year)}${p.cohort ? `, ${esc(p.cohort)} pupils` : ""}</span></h4>${kv(rows)}${p.note ? `<p class="note">${esc(p.note)}</p>` : ""}`;
  }

  function gradeClass(v) {
    return ({ "Exceptional": "5", "Strong standard": "4", "Expected standard": "3", "Needs attention": "2",
      "Urgent improvement": "1", "Met": "4", "Not met": "1" })[v] || "0";
  }

  function webUrl(w) { return /^https?:/i.test(w) ? w : "https://" + w; }

  function wirePanel(s) {
    const btn = $("btnCatch");
    if (btn) btn.addEventListener("click", () => toggleCatchment(s));
  }

  function toggleCatchment(s) {
    const btn = $("btnCatch");
    const cuts = cutoffsFor(s);
    if (catchmentLayer.getLayers().length) {
      catchmentLayer.clearLayers();
      if (btn) btn.textContent = cuts.length ? "Show catchment (last distance offered)" : "Show approximate catchment";
      $("catchNote").innerHTML = "";
      return;
    }
    if (cuts.length) {
      const circles = cuts.slice().sort((a, b) => b.mi - a.mi).map((r, k) =>
        L.circle([s.lat, s.lng], { radius: r.mi * MI_M, color: css("--accent"), weight: 2, fillOpacity: k === 0 ? 0.12 : 0.06 })
          .bindTooltip(`${r.entry} ${r.year || ""}: last offer ${r.mi.toFixed(2)} mi`, { sticky: true }));
      circles.forEach((c) => catchmentLayer.addLayer(c));
      map.fitBounds(circles[0].getBounds(), { padding: [30, 30], maxZoom: 16 });
      if (btn) btn.textContent = "Hide catchment";
      const walk = cuts.some((r) => !isStraight(r));
      $("catchNote").innerHTML = `<p class="note">Circle: the furthest distance the council offered a place on distance (${cuts.map((r) => `${esc(r.entry)} ${esc(r.year || "")}: ${r.mi.toFixed(2)} mi`).join("; ")}).${walk ? " Distances here are measured by walking route, so the real area is smaller than the circle." : ""} It moves every year with demand.</p>`;
      return;
    }
    const poly = catchmentPolygon(s);
    if (!poly) return;
    const layer = L.polygon(poly.ring, { color: css("--accent"), weight: 2, fillOpacity: 0.12, dashArray: "6 4" });
    catchmentLayer.addLayer(layer);
    map.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 16 });
    if (btn) btn.textContent = "Hide catchment";
    const caveat = s.sec === "i" ? "Independent schools don’t have catchments."
      : s.sel ? "Grammar schools admit on test results, so distance matters less."
      : s.faith ? "Faith schools often prioritise by religious practice before distance."
      : "Most schools use distance as a tie-breaker when oversubscribed.";
    $("catchNote").innerHTML = `<p class="note">Dashed area: closer to this school than to any other state ${s.ph === "P" ? "primary" : s.ph === "S" ? "secondary" : ""} school (${poly.peers} compared). This is <strong>not</strong> an official catchment. ${caveat} Check the local authority’s last-distance-offered figures.</p>`;
  }

  $("closePanel").addEventListener("click", () => {
    state.selected = null;
    catchmentLayer.clearLayers();
    $("panel").hidden = true;
    history.replaceState(null, "", location.pathname);
    map.invalidateSize();
    dots.redraw();
  });

  // ---------- Postcode search ----------
  $("postcodeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("postcode").value.trim();
    if (!q) return;
    const msg = $("postcodeMsg");
    msg.className = "msg"; msg.textContent = "Looking up…";
    try {
      const loc = await geocode(q);
      setHome(loc);
      msg.textContent = "";
      if (isNarrow()) setSidebar(false);
    } catch (err) {
      msg.className = "msg err"; msg.textContent = err.message;
    }
  });

  async function geocode(q) {
    const clean = q.replace(/\s+/g, "").toUpperCase();
    const full = /^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$/.test(clean);
    const url = full ? `https://api.postcodes.io/postcodes/${encodeURIComponent(clean)}`
      : `https://api.postcodes.io/outcodes/${encodeURIComponent(clean)}`;
    const r = await fetch(url);
    if (!r.ok) {
      const pl = await fetch(`https://api.postcodes.io/places?q=${encodeURIComponent(q)}&limit=1`).then((x) => x.json()).catch(() => null);
      if (pl && pl.result && pl.result.length) return { lat: pl.result[0].latitude, lng: pl.result[0].longitude, label: pl.result[0].name_1 };
      throw new Error("Couldn’t find that postcode or place.");
    }
    const j = (await r.json()).result;
    if (j.latitude == null) throw new Error("That postcode has no location on record.");
    return { lat: j.latitude, lng: j.longitude, label: j.postcode || j.outcode };
  }

  function setHome(loc) {
    state.home = loc;
    if (homeMarker) map.removeLayer(homeMarker);
    homeMarker = L.marker([loc.lat, loc.lng], {
      icon: L.divIcon({ className: "home-pin", iconSize: [16, 16] }), keyboard: false, title: loc.label,
    }).addTo(map);
    map.setView([loc.lat, loc.lng], 13);
    renderNearby();
    if (state.selected) selectSchool(state.selected, { pan: false });
  }

  function renderNearby() {
    const h = state.home;
    const list = state.visible.map((i) => [i, distKm(h, state.S[i])]).sort((a, b) => a[1] - b[1]).slice(0, 15);
    $("nearby").hidden = false;
    $("nearbyList").innerHTML = list.map(([i, km]) => {
      const s = state.S[i];
      const extra = [];
      if (s.ks2 != null) extra.push(`KS2 ${s.ks2}%`);
      if (s.ks4 != null) extra.push(`Att8 ${s.ks4.toFixed(1)}`);
      if (s.dem != null) extra.push(`${s.dem.toFixed(1)} 1st prefs/place`);
      const cut = cutoffsFor(s)[0];
      if (cut) {
        const hv = homeVsCutoff(s, cut);
        extra.push(hv && hv.inside !== null ? `${hv.inside ? "✓ inside" : "✗ outside"} ${cut.year || ""} cut-off (${cut.mi.toFixed(2)} mi)` : `cut-off ${cut.mi.toFixed(2)} mi`);
      }
      return `<li data-i="${i}"><span class="dot" style="background:${ofstedColour(s.o)}"></span>
        <span class="nm">${esc(s.name)}<small>${esc(ratingLabel(s))}${extra.length ? " · " + extra.join(" · ") : ""}</small></span>
        <span class="km">${fmtDist(km).split(" (")[0]}</span></li>`;
    }).join("");
  }
  $("nearbyList").addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (li) selectSchool(state.S[+li.dataset.i]);
  });

  // ---------- Name search ----------
  let nameIndex = [];
  $("nameSearch").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    const ul = $("nameResults");
    if (q.length < 3) { ul.innerHTML = ""; return; }
    const hits = [];
    for (let i = 0; i < nameIndex.length && hits.length < 12; i++) {
      if (nameIndex[i].includes(q) || String(state.S[i].urn) === q) hits.push(i);
    }
    ul.innerHTML = hits.map((i) => {
      const s = state.S[i];
      return `<li role="option" data-i="${i}">${esc(s.name)}<small>${esc(state.data.lk.la[s.la] || "")} · ${esc(ratingLabel(s))}</small></li>`;
    }).join("");
  });
  $("nameResults").addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    $("nameResults").innerHTML = "";
    $("nameSearch").value = "";
    selectSchool(state.S[+li.dataset.i]);
  });

  // ---------- Misc UI ----------
  $("colourBy").addEventListener("change", (e) => { state.colourBy = e.target.value; renderLegend(); dots.redraw(); });
  $("fGrammar").addEventListener("change", (e) => { state.grammar = e.target.checked; applyFilters(); });
  $("fNoFaith").addEventListener("change", (e) => { state.noFaith = e.target.checked; applyFilters(); });
  $("fMixed").addEventListener("change", (e) => { state.mixed = e.target.checked; applyFilters(); });
  $("aboutLink").addEventListener("click", (e) => { e.preventDefault(); $("about").showModal(); });
  $("toggleSidebar").addEventListener("click", () => setSidebar($("sidebar").classList.contains("collapsed")));
  $("hideSidebar").addEventListener("click", () => setSidebar(false));
  if (isNarrow()) setSidebar(false);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { refreshColours(); renderLegend(); dots.redraw(); });

  // ---------- Boot ----------
  async function boot() {
    refreshColours();
    let data;
    try {
      data = await fetch("data/schools.json").then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
    } catch (err) {
      $("dataStamp").textContent = "School data hasn’t been built yet. Run the “Build school data” workflow.";
      return;
    }
    state.data = data;
    const c = Object.fromEntries(data.cols.map((k, i) => [k, i]));
    state.S = data.rows.map((r) => ({
      urn: r[c.urn], name: r[c.name], lat: r[c.lat], lng: r[c.lng], ph: r[c.ph], sec: r[c.sec], t: r[c.t],
      o: r[c.o], g: r[c.g], faith: r[c.faith], sel: r[c.sel], la: r[c.la],
      ks2: r[c.ks2], ks4: r[c.ks4], ks5: r[c.ks5], dem: r[c.dem],
    }));
    state.S.forEach((s) => state.byUrn.set(s.urn, s));
    nameIndex = state.S.map((s) => s.name.toLowerCase());
    $("dataStamp").textContent = `${state.S.length.toLocaleString()} open schools · data built ${data.built}`;
    $("sourceList").innerHTML = (data.sources || []).map((s) =>
      `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a>${s.edition ? ` (${esc(s.edition)})` : ""}</li>`).join("");

    buildChips("phaseChips", PHASES, state.phases);
    buildChips("ofstedChips", OFSTED.map((o) => [o[0], o[1]]), state.ofsted, (k) => ofstedColours[k]);
    buildChips("sectorChips", SECTORS, state.sectors);
    computeBreaks();
    renderLegend();
    try {
      const cj = await fetch("data/catchment.json").then((r) => (r.ok ? r.json() : null));
      if (cj && cj.schools) state.cutoffs = cj.schools;
    } catch (err) { /* optional dataset */ }
    applyFilters();

    const m = location.hash.match(/urn=(\d+)/);
    if (m && state.byUrn.has(+m[1])) selectSchool(state.byUrn.get(+m[1]));
  }
  boot();
})();
