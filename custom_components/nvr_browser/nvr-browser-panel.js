// NVR Browser — flat newest-first gallery panel for the home-grown /config/nvr clips.
// Vanilla custom element (no build step). HA injects `hass`; we use hass.callApi
// for the authed event list and plain <img>/<video> for thumbs/playback.

class NvrBrowserPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._events = [];
    this._seen = new Set();
    this._offset = 0;
    this._limit = 60;
    this._loading = false;
    this._done = false;
    this._camera = "";
    this._object = "";
    this._start = "";   // inclusive YYYY-MM-DD, "" = no lower bound
    this._end = "";     // inclusive YYYY-MM-DD, "" = no upper bound
    this._dayList = [];
    this._daySet = new Set();
    this._minDay = "";       // oldest available day (lower bound for the picker)
    this._maxDay = "";       // newest available day (upper bound for the picker)
    this._viewMonth = null;  // {y, m} month currently shown in the calendar popup
    this._pickStart = "";    // first-click anchor while picking a range
    this._calOpen = false;
    this._cameras = new Set();
    this._objects = new Set();
    this._booted = false;
    this._basePath = "";   // page path the panel mounted at; guards URL re-syncs
    this._onLocationChanged = () => this._syncFromUrl();
  }

  // The element may be torn down and rebuilt, OR kept and re-attached, when the
  // user navigates away and back — HA's choice, and not one we want to depend on.
  // Listening for navigations (HA's `location-changed` + browser back/forward)
  // and re-attachment makes deep links apply either way: a fresh element reads
  // params in _boot(); a reused one picks them up here.
  connectedCallback() {
    window.addEventListener("location-changed", this._onLocationChanged);
    window.addEventListener("popstate", this._onLocationChanged);
    if (this._booted) this._syncFromUrl();   // re-attached cached instance
  }

  disconnectedCallback() {
    window.removeEventListener("location-changed", this._onLocationChanged);
    window.removeEventListener("popstate", this._onLocationChanged);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._booted) {
      this._booted = true;
      this._boot();
    }
  }

  // Filters that round-trip through the page URL: param name <-> state field,
  // with an optional validator. This is the single source of truth for which
  // filters are deep-linkable/shareable — add a row to expose a new one. Param
  // names intentionally match the events API (camera/object/start/end).
  _filterParams() {
    const isDate = (v) => /^\d{4}-\d{2}-\d{2}$/.test(v);
    return [
      { param: "camera", field: "_camera" },
      { param: "object", field: "_object" },
      { param: "start", field: "_start", valid: isDate },
      { param: "end", field: "_end", valid: isDate },
    ];
  }

  // Read filter state from the page URL so the panel can be deep-linked, e.g.
  // /nvr-browser?camera=front_door&object=person&start=2026-06-01&end=2026-06-08.
  // Called once on boot, before the first fetch, so the initial load is filtered.
  _applyUrlParams() {
    const P = new URLSearchParams(window.location.search);
    for (const { param, field, valid } of this._filterParams()) {
      const v = P.get(param);
      if (v && (!valid || valid(v))) this[field] = v;
    }
    // Pre-seed the facet sets so a deep-linked value shows in its dropdown even
    // if no matching event reveals that facet (e.g. the filter matches nothing).
    if (this._camera) this._cameras.add(this._camera);
    if (this._object) this._objects.add(this._object);
  }

  // Reflect the active filters back into the page URL (without navigating), so
  // the current view is shareable/bookmarkable. replaceState keeps each filter
  // change out of the back-history; we preserve HA's router state object.
  _syncUrl() {
    const P = new URLSearchParams();
    for (const { param, field } of this._filterParams()) {
      if (this[field]) P.set(param, this[field]);
    }
    const qs = P.toString();
    const url = window.location.pathname + (qs ? `?${qs}` : "");
    try {
      window.history.replaceState(window.history.state, "", url);
    } catch (e) {
      /* non-fatal: URL just won't update; the filters still apply */
    }
  }

  // Re-read filters from the URL and, if they changed, apply them and reload.
  // Fires on in-app navigation, browser back/forward, and re-attachment. Guarded
  // so navigating to a *different* panel (URL no longer ours) is ignored, and a
  // no-op when nothing changed — which also breaks the replaceState feedback loop
  // (_reset -> _syncUrl writes the same URL without firing a navigation event).
  _syncFromUrl() {
    if (!this._booted) return;
    if (window.location.pathname !== this._basePath) return;
    const P = new URLSearchParams(window.location.search);
    let changed = false;
    for (const { param, field, valid } of this._filterParams()) {
      let v = P.get(param) || "";
      if (v && valid && !valid(v)) v = "";   // drop a malformed value
      if (this[field] !== v) { this[field] = v; changed = true; }
    }
    if (!changed) return;
    if (this._camera) this._cameras.add(this._camera);
    if (this._object) this._objects.add(this._object);
    this._renderSelect(this._cams, [...this._cameras].sort(), "_camera", "All cameras");
    this._renderSelect(this._objs, [...this._objects].sort(), "_object", "All objects");
    this._syncDateControls();
    this._reset();
  }

  _boot() {
    this._applyUrlParams();
    this._basePath = window.location.pathname;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; height: 100%; background: var(--primary-background-color, #111); color: var(--primary-text-color, #e1e1e1); }
        .bar { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
               padding: 10px 14px; background: var(--app-header-background-color, var(--primary-color, #1a1a1a));
               color: var(--app-header-text-color, #fff); box-shadow: 0 2px 6px rgba(0,0,0,.4); }
        .bar .title { font-size: 18px; font-weight: 600; margin-right: 6px; }
        .bar .spacer { flex: 1; }
        .label { opacity: .7; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; margin: 0 2px 0 6px; }
        .sel { background: rgba(255,255,255,.12); color: inherit; border: 1px solid rgba(255,255,255,.35);
               border-radius: 6px; padding: 3px 6px; font-size: 13px; color-scheme: dark; }
        .calwrap { position: relative; display: inline-flex; }
        .iconbtn { cursor: pointer; border: 1px solid rgba(255,255,255,.35); border-radius: 6px;
                   padding: 3px 8px; font-size: 13px; line-height: 1.4; background: rgba(255,255,255,.12); color: inherit; }
        .iconbtn.on { background: #fff; border-color: #fff; }
        .cal { position: absolute; top: calc(100% + 6px); left: 0; z-index: 20; width: 236px;
               background: #1f1f1f; color: #e1e1e1; border: 1px solid rgba(255,255,255,.18);
               border-radius: 10px; padding: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.5); }
        .cal[hidden] { display: none; }
        .calhd { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .calmon { font-size: 13px; font-weight: 600; }
        .navbtn { cursor: pointer; background: transparent; border: 1px solid rgba(255,255,255,.25);
                  color: inherit; border-radius: 6px; padding: 2px 8px; font-size: 13px; }
        .calgrid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
        .calwk .wd { text-align: center; font-size: 11px; opacity: .55; padding: 2px 0; }
        .caldays { margin-top: 2px; }
        .day { cursor: pointer; background: transparent; border: 1px solid transparent; color: inherit;
               border-radius: 6px; padding: 4px 0; font-size: 12px; position: relative;
               font-variant-numeric: tabular-nums; }
        .day.empty { visibility: hidden; cursor: default; }
        .day:hover:not(.disabled):not(.empty) { background: rgba(255,255,255,.12); }
        .day.has::after { content: ""; position: absolute; bottom: 3px; left: 50%; transform: translateX(-50%);
                          width: 3px; height: 3px; border-radius: 50%; background: #6cb6ff; }
        .day.inrange { background: rgba(108,182,255,.18); }
        .day.sel { background: #6cb6ff; color: #000; border-color: #6cb6ff; }
        .day.sel.has::after { background: #000; }
        .day.disabled { opacity: .25; cursor: default; }
        .calft { margin-top: 8px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .calhint { font-size: 11px; opacity: .5; }
        .btn { cursor: pointer; border: 1px solid rgba(255,255,255,.35); border-radius: 6px; padding: 4px 10px;
               background: transparent; color: inherit; font-size: 13px; }
        .grid { display: grid; gap: 12px; padding: 14px;
                grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
        .card { background: #000; border-radius: 10px; overflow: hidden; cursor: pointer; position: relative;
                box-shadow: 0 1px 4px rgba(0,0,0,.5); transition: transform .08s ease; }
        .card:hover { transform: translateY(-2px); }
        .thumb { width: 100%; aspect-ratio: 16 / 9; object-fit: cover; display: block; background: #222; }
        .meta { padding: 7px 9px; font-size: 13px; display: flex; flex-direction: column; gap: 3px; }
        .meta .when { font-variant-numeric: tabular-nums; }
        .meta .cam { opacity: .75; font-size: 12px; }
        .badges { position: absolute; top: 6px; left: 6px; display: flex; gap: 4px; }
        .badge { font-size: 11px; background: rgba(0,0,0,.65); border: 1px solid rgba(255,255,255,.4);
                 border-radius: 10px; padding: 1px 7px; text-transform: capitalize; }
        .badge.person { background: #c0392b; border-color: #e74c3c; }
        .badge.cat { background: #8e44ad; border-color: #9b59b6; }
        .status { text-align: center; padding: 20px; opacity: .6; font-size: 14px; }
        .sentinel { height: 1px; }
        /* lightbox */
        .lb { position: fixed; inset: 0; z-index: 50; background: rgba(0,0,0,.9); display: none;
              align-items: center; justify-content: center; flex-direction: column; gap: 12px; padding: 20px; }
        .lb.show { display: flex; }
        .lb video { max-width: 92vw; max-height: 78vh; border-radius: 8px; background: #000; }
        .lb .info { color: #ddd; font-size: 14px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
        .lb a { color: #6cb6ff; text-decoration: none; }
        .lb .x { position: absolute; top: 14px; right: 18px; font-size: 30px; cursor: pointer; color: #fff; line-height: 1; }
      </style>
      <div class="bar">
        <span class="title">NVR</span>
        <span class="label">Day</span>
        <select class="sel" id="day" title="Jump to a day"></select>
        <div class="calwrap">
          <button class="iconbtn" id="calbtn" title="Pick a date range" aria-haspopup="true">📅</button>
          <div class="cal" id="cal" hidden></div>
        </div>
        <span class="label">Camera</span>
        <select class="sel" id="cams" title="Filter by camera"></select>
        <span class="label">Object</span>
        <select class="sel" id="objs" title="Filter by object"></select>
        <span class="spacer"></span>
        <button class="btn" id="refresh">Refresh</button>
      </div>
      <div class="grid" id="grid"></div>
      <div class="status" id="status"></div>
      <div class="sentinel" id="sentinel"></div>
      <div class="lb" id="lb">
        <span class="x" id="lbx">&times;</span>
        <video id="lbv" controls playsinline></video>
        <div class="info" id="lbi"></div>
      </div>`;

    this._grid = this.shadowRoot.getElementById("grid");
    this._status = this.shadowRoot.getElementById("status");
    this._day = this.shadowRoot.getElementById("day");
    this._calbtn = this.shadowRoot.getElementById("calbtn");
    this._cal = this.shadowRoot.getElementById("cal");
    this._cams = this.shadowRoot.getElementById("cams");
    this._objs = this.shadowRoot.getElementById("objs");
    this._lb = this.shadowRoot.getElementById("lb");
    this._lbv = this.shadowRoot.getElementById("lbv");
    this._lbi = this.shadowRoot.getElementById("lbi");

    this._day.addEventListener("change", () => this._onDaySelect());
    this._cams.addEventListener("change", () => { this._camera = this._cams.value; this._reset(); });
    this._objs.addEventListener("change", () => { this._object = this._objs.value; this._reset(); });
    // Seed with the "All" option plus any facet already known from a deep-linked
    // filter (_applyUrlParams pre-seeds the sets); the rest fill in as events arrive.
    this._renderSelect(this._cams, [...this._cameras].sort(), "_camera", "All cameras");
    this._renderSelect(this._objs, [...this._objects].sort(), "_object", "All objects");
    this._calbtn.addEventListener("click", () => this._toggleCalendar());
    // click anywhere outside the popup (or its button) closes it
    document.addEventListener("click", (e) => {
      if (!this._calOpen) return;
      const path = e.composedPath();
      if (!path.includes(this._cal) && !path.includes(this._calbtn)) this._closeCalendar();
    });
    this.shadowRoot.getElementById("refresh").addEventListener("click", () => this._reset());
    this.shadowRoot.getElementById("lbx").addEventListener("click", () => this._closeLightbox());
    this._lb.addEventListener("click", (e) => { if (e.target === this._lb) this._closeLightbox(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") this._closeLightbox(); });

    this._observer = new IntersectionObserver(
      (entries) => { if (entries.some((e) => e.isIntersecting)) this._loadMore(); },
      { rootMargin: "600px" }
    );
    this._observer.observe(this.shadowRoot.getElementById("sentinel"));

    this._loadDays();
    this._loadMore();
  }

  async _loadDays() {
    try {
      const data = await this._hass.callApi("GET", "nvr_browser/days");
      const days = (data && data.days) || [];
      this._dayList = days;
      this._daySet = new Set(days);
      if (days.length) {
        this._maxDay = days[0];                 // newest-first
        this._minDay = days[days.length - 1];
      }
      this._syncDateControls();
    } catch (err) {
      /* the date controls are a convenience; ignore failures */
    }
  }

  // Rebuild the Day <select> from the available days and reflect the current
  // start/end selection. The dropdown handles single-day jumps ("All days" or
  // one day); a multi-day range picked in the calendar shows as a synthetic,
  // non-reusable option so the control never lies about what's filtered.
  _syncDateControls() {
    const sel = this._day;
    const isRange = this._start && this._end && this._start !== this._end;
    sel.innerHTML = "";
    const opt = (label, value) => {
      const o = document.createElement("option");
      o.textContent = label;
      o.value = value;
      sel.appendChild(o);
    };
    opt("All days", "");
    if (isRange) opt(this._rangeLabel(this._start, this._end), "__range__");
    for (const d of this._dayList) opt(this._dayLabel(d), d);
    sel.value = isRange ? "__range__" : (this._start || "");
    // light up the calendar button whenever any date filter is active
    this._calbtn.classList.toggle("on", !!(this._start || this._end));
    if (this._calOpen) this._renderCalendar();
  }

  _onDaySelect() {
    const v = this._day.value;
    if (v === "__range__") return;  // synthetic label for an active range; no-op
    this._start = this._end = v || "";
    this._syncDateControls();
    this._reset();
  }

  _dayLabel(d) {
    // "2026-06-14" -> "Jun 14"
    const [y, m, day] = d.split("-").map(Number);
    const mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m - 1] || d;
    return `${mon} ${day}`;
  }

  _rangeLabel(s, e) {
    return `${this._dayLabel(s)} – ${this._dayLabel(e)}`;
  }

  _toggleCalendar() {
    if (this._calOpen) this._closeCalendar();
    else this._openCalendar();
  }

  _openCalendar() {
    // open on the month of the current selection, else the newest available day
    const ref = this._end || this._start || this._maxDay;
    if (ref) {
      this._viewMonth = { y: +ref.slice(0, 4), m: +ref.slice(5, 7) - 1 };
    } else {
      const now = new Date();
      this._viewMonth = { y: now.getFullYear(), m: now.getMonth() };
    }
    this._pickStart = "";
    this._calOpen = true;
    this._cal.hidden = false;
    this._renderCalendar();
  }

  _closeCalendar() {
    this._calOpen = false;
    this._pickStart = "";   // discard a half-finished range pick
    this._cal.hidden = true;
  }

  _renderCalendar() {
    const MON = ["January","February","March","April","May","June",
                 "July","August","September","October","November","December"];
    const { y, m } = this._viewMonth;
    const pad = (n) => String(n).padStart(2, "0");
    const firstWeekday = new Date(y, m, 1).getDay();   // 0=Sun .. local DOW
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    const anchor = this._pickStart;   // start day highlighted between the two clicks

    let html = `
      <div class="calhd">
        <button class="navbtn" data-nav="-1" title="Previous month">‹</button>
        <span class="calmon">${MON[m]} ${y}</span>
        <button class="navbtn" data-nav="1" title="Next month">›</button>
      </div>
      <div class="calgrid calwk">
        ${["S","M","T","W","T","F","S"].map((d) => `<span class="wd">${d}</span>`).join("")}
      </div>
      <div class="calgrid caldays">`;
    for (let i = 0; i < firstWeekday; i++) html += `<span class="day empty"></span>`;
    for (let d = 1; d <= daysInMonth; d++) {
      const iso = `${y}-${pad(m + 1)}-${pad(d)}`;
      const inBounds = (!this._minDay || iso >= this._minDay) &&
                       (!this._maxDay || iso <= this._maxDay);
      let cls = "day";
      if (!inBounds) cls += " disabled";
      if (this._daySet.has(iso)) cls += " has";   // a dot marks days with clips
      if (anchor) {
        if (iso === anchor) cls += " sel";
      } else if (iso === this._start || iso === this._end) {
        cls += " sel";
      } else if (this._start && this._end && iso > this._start && iso < this._end) {
        cls += " inrange";
      }
      html += `<button class="${cls}" data-day="${iso}"${inBounds ? "" : " disabled"}>${d}</button>`;
    }
    html += `</div>
      <div class="calft">
        <button class="navbtn" data-clear="1">All days</button>
        <span class="calhint">${anchor ? "Pick the end day" : "Pick start, then end"}</span>
      </div>`;
    this._cal.innerHTML = html;

    this._cal.querySelectorAll("[data-nav]").forEach((b) =>
      b.addEventListener("click", () => this._navMonth(+b.dataset.nav)));
    this._cal.querySelector("[data-clear]").addEventListener("click", () => {
      this._start = this._end = "";
      this._closeCalendar();
      this._syncDateControls();
      this._reset();
    });
    this._cal.querySelectorAll(".day[data-day]:not(.disabled)").forEach((b) =>
      b.addEventListener("click", () => this._pickDay(b.dataset.day)));
  }

  _navMonth(delta) {
    let { y, m } = this._viewMonth;
    m += delta;
    if (m < 0) { m = 11; y -= 1; }
    else if (m > 11) { m = 0; y += 1; }
    this._viewMonth = { y, m };
    this._renderCalendar();
  }

  _pickDay(iso) {
    if (!this._pickStart) {
      // first click anchors the start; re-render to highlight it, await the end
      this._pickStart = iso;
      this._renderCalendar();
      return;
    }
    let s = this._pickStart, e = iso;
    if (e < s) { const t = s; s = e; e = t; }   // clicked before the anchor → swap
    this._start = s;
    this._end = e;
    this._closeCalendar();
    this._syncDateControls();
    this._reset();
  }

  _reset() {
    this._syncUrl();
    this._events = [];
    this._seen.clear();
    this._offset = 0;
    this._done = false;
    this._grid.innerHTML = "";
    this._loadMore();
  }

  async _loadMore() {
    if (this._loading || this._done) return;
    this._loading = true;
    this._status.textContent = "Loading…";
    const params = new URLSearchParams({ offset: this._offset, limit: this._limit });
    if (this._camera) params.set("camera", this._camera);
    if (this._object) params.set("object", this._object);
    if (this._start) params.set("start", this._start);
    if (this._end) params.set("end", this._end);
    try {
      const data = await this._hass.callApi("GET", `nvr_browser/events?${params}`);
      const events = (data && data.events) || [];
      if (events.length < this._limit) this._done = true;
      this._offset += events.length;
      for (const ev of events) {
        if (this._seen.has(ev.id)) continue;
        this._seen.add(ev.id);
        this._events.push(ev);
        this._addFacets(ev);
        this._grid.appendChild(this._card(ev));
      }
      this._status.textContent = this._done
        ? (this._events.length ? `${this._events.length} clips` : "No clips match.")
        : "";
    } catch (err) {
      this._status.textContent = `Error: ${err && err.message ? err.message : err}`;
    } finally {
      this._loading = false;
    }
  }

  _addFacets(ev) {
    if (ev.camera && !this._cameras.has(ev.camera)) {
      this._cameras.add(ev.camera);
      this._renderSelect(this._cams, [...this._cameras].sort(), "_camera", "All cameras");
    }
    let added = false;
    for (const o of ev.objects || []) {
      if (!this._objects.has(o)) { this._objects.add(o); added = true; }
    }
    if (added) this._renderSelect(this._objs, [...this._objects].sort(), "_object", "All objects");
  }

  // Rebuild a filter <select> from the discovered facet values, keeping the
  // current selection. The "All" option (value "") clears the filter.
  _renderSelect(sel, values, field, allLabel) {
    sel.innerHTML = "";
    const opt = (label, value) => {
      const o = document.createElement("option");
      o.textContent = label;
      o.value = value;
      sel.appendChild(o);
    };
    opt(allLabel, "");
    for (const v of values) opt(v, v);
    sel.value = this[field] || "";
  }

  _card(ev) {
    const card = document.createElement("div");
    card.className = "card";
    const badges = (ev.objects || [])
      .map((o) => `<span class="badge ${o}">${o}</span>`)
      .join("");
    card.innerHTML = `
      ${badges ? `<div class="badges">${badges}</div>` : ""}
      <img class="thumb" loading="lazy" src="${ev.thumb}" alt="">
      <div class="meta">
        <span class="when">${ev.time} &middot; ${ev.date}</span>
        <span class="cam">${ev.camera}</span>
      </div>`;
    card.addEventListener("click", () => this._openLightbox(ev));
    return card;
  }

  _openLightbox(ev) {
    this._lbv.src = ev.url;
    const objs = (ev.objects || []).join(", ");
    // the clip URL is now /api/.../clip?path=…&authSig=… (no .mp4 suffix), so set
    // an explicit download name to keep a sensible filename + extension
    const fname = `${ev.date}_${(ev.time || "").replace(/:/g, "-")}_${ev.camera}.mp4`;
    this._lbi.innerHTML =
      `<span>${ev.camera} &middot; ${ev.datetime}${objs ? " &middot; " + objs : ""}</span>` +
      `<a href="${ev.url}" download="${fname}">Download</a>`;
    this._lb.classList.add("show");
    this._lbv.play().catch(() => {});
  }

  _closeLightbox() {
    this._lb.classList.remove("show");
    this._lbv.pause();
    this._lbv.removeAttribute("src");
    this._lbv.load();
  }
}

customElements.define("nvr-browser-panel", NvrBrowserPanel);
