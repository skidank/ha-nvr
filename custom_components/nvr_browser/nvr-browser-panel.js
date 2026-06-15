// NVR Browser — flat newest-first gallery panel for the home-grown www/nvr clips.
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
    this._cameras = new Set();
    this._objects = new Set();
    this._booted = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._booted) {
      this._booted = true;
      this._boot();
    }
  }

  _boot() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; height: 100%; background: var(--primary-background-color, #111); color: var(--primary-text-color, #e1e1e1); }
        .bar { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
               padding: 10px 14px; background: var(--app-header-background-color, var(--primary-color, #1a1a1a));
               color: var(--app-header-text-color, #fff); box-shadow: 0 2px 6px rgba(0,0,0,.4); }
        .bar .title { font-size: 18px; font-weight: 600; margin-right: 6px; }
        .bar .spacer { flex: 1; }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; }
        .chip { cursor: pointer; border: 1px solid rgba(255,255,255,.35); border-radius: 14px; padding: 3px 11px;
                font-size: 13px; background: transparent; color: inherit; line-height: 1.5; }
        .chip.on { background: #fff; color: #000; border-color: #fff; }
        .label { opacity: .7; font-size: 12px; text-transform: uppercase; letter-spacing: .5px; margin: 0 2px 0 6px; }
        .date { background: rgba(255,255,255,.12); color: inherit; border: 1px solid rgba(255,255,255,.35);
                border-radius: 6px; padding: 3px 6px; font-size: 13px; color-scheme: dark; }
        .dash { opacity: .6; }
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
        <span class="label">Day</span><div class="chips" id="days"></div>
        <input type="date" class="date" id="from" title="From (inclusive)">
        <span class="dash">&ndash;</span>
        <input type="date" class="date" id="to" title="To (inclusive)">
        <span class="label">Camera</span><div class="chips" id="cams"></div>
        <span class="label">Object</span><div class="chips" id="objs"></div>
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
    this._days = this.shadowRoot.getElementById("days");
    this._cams = this.shadowRoot.getElementById("cams");
    this._objs = this.shadowRoot.getElementById("objs");
    this._from = this.shadowRoot.getElementById("from");
    this._to = this.shadowRoot.getElementById("to");
    this._lb = this.shadowRoot.getElementById("lb");
    this._lbv = this.shadowRoot.getElementById("lbv");
    this._lbi = this.shadowRoot.getElementById("lbi");

    this._from.addEventListener("change", () => this._onRangeInput());
    this._to.addEventListener("change", () => this._onRangeInput());
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
      if (days.length) {
        // bound the native pickers to what actually exists
        this._from.max = this._to.max = days[0];
        this._from.min = this._to.min = days[days.length - 1];
      }
      this._dayList = days;
      this._renderDays(days);
    } catch (err) {
      /* day chips are a convenience; ignore failures */
    }
  }

  _renderDays(days) {
    this._dayList = days;
    this._days.innerHTML = "";
    const mk = (label, value) => {
      const c = document.createElement("button");
      // a day chip is "on" only when the range is exactly that single day
      const on = value ? this._start === value && this._end === value
                       : !this._start && !this._end;
      c.className = "chip" + (on ? " on" : "");
      c.textContent = label;
      c.addEventListener("click", () => {
        if (value) { this._start = this._end = value; }
        else { this._start = this._end = ""; }
        this._from.value = this._start;
        this._to.value = this._end;
        this._renderDays(days);
        this._reset();
      });
      return c;
    };
    this._days.appendChild(mk("All", ""));
    for (const d of days) this._days.appendChild(mk(this._dayLabel(d), d));
  }

  _dayLabel(d) {
    // "2026-06-14" -> "Jun 14"
    const [y, m, day] = d.split("-").map(Number);
    const mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m - 1] || d;
    return `${mon} ${day}`;
  }

  _onRangeInput() {
    this._start = this._from.value || "";
    this._end = this._to.value || "";
    // keep From <= To
    if (this._start && this._end && this._start > this._end) {
      this._end = this._start;
      this._to.value = this._end;
    }
    if (this._dayList) this._renderDays(this._dayList);
    this._reset();
  }

  _reset() {
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
      this._renderChips(this._cams, [...this._cameras].sort(), "_camera");
    }
    let added = false;
    for (const o of ev.objects || []) {
      if (!this._objects.has(o)) { this._objects.add(o); added = true; }
    }
    if (added) this._renderChips(this._objs, [...this._objects].sort(), "_object");
  }

  _renderChips(container, values, field) {
    container.innerHTML = "";
    const mk = (label, value) => {
      const c = document.createElement("button");
      c.className = "chip" + (this[field] === value ? " on" : "");
      c.textContent = label;
      c.addEventListener("click", () => {
        this[field] = this[field] === value ? "" : value;
        this._renderChips(container, values, field);
        this._reset();
      });
      return c;
    };
    container.appendChild(mk("All", ""));
    for (const v of values) container.appendChild(mk(v, v));
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
    this._lbi.innerHTML =
      `<span>${ev.camera} &middot; ${ev.datetime}${objs ? " &middot; " + objs : ""}</span>` +
      `<a href="${ev.url}" download>Download</a>`;
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
