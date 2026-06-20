# CLAUDE.md — nvr_browser

Guidance for working in this repo. This is a **custom Home Assistant integration**
that adds a sidebar panel for browsing the home-grown NVR clips in a live HA's
`/config/nvr`. Read this before editing.

## What it is

A read-only, additive HA integration: a flat newest-first thumbnail gallery
(camera + object filters, a Day dropdown + a range-picker calendar popup, ffmpeg
thumbnails, click-to-play lightbox) that replaces HA's clunky Media browser for
the `/config/nvr` motion clips. It must **never** modify the recording automations
or any file under `/config/nvr`.

Current dev version: **0.9.2**. The released (HACS) version may lag this working
tree — bump `VERSION` when cutting a release (see Releasing).

It also exposes a small **TV-pairing** API so the companion Roku app
([`nvr-roku`](https://github.com/skidank/nvr-roku)) can authenticate without
typing a ~250-char long-lived token on a TV keyboard. This is the only part that
touches HA auth (it mints tokens); it is still read-only w.r.t. `/config/nvr`.

Since 0.9.0 it also serves a **live camera view** to the Roku app: a `live_cameras`
config map points each NVR camera name at an HA camera entity, and the integration
hands the Roku app HA's own HLS stream URL for it (no proxy/transcode). Still
read-only w.r.t. `/config/nvr`; the only new HA surface it touches is the `camera`
component's stream API (read-only).

## Layout

```
nvr_browser/
├── CLAUDE.md                       # this file
├── README.md                       # user-facing: features + install steps
└── custom_components/nvr_browser/  # the installable component (HACS / copy this folder)
    ├── manifest.json               # YAML-config integration (config_flow: false)
    ├── __init__.py                 # async_setup: HTTP views (events/clip/thumb/proxy/cameras/live/pair) + ffmpeg thumbs/proxies + prune + panel
    └── nvr-browser-panel.js        # vanilla custom element <nvr-browser-panel>
```

## Architecture

- **`async_setup`** (YAML-triggered by `nvr_browser:` in configuration.yaml):
  registers a static path for the JS, three HTTP views, the thumb cache dir, the
  scheduled prune job, and a custom sidebar panel via
  `frontend.async_register_built_in_panel(..., "custom", ...)`.
- **`GET /api/nvr_browser/events`** — authed JSON, newest-first, paginated.
  Params: `offset`, `limit`, `camera`, `object`, `start`, `end` (inclusive
  `YYYY-MM-DD` bounds, validated against `DATE_RE`). Scan runs in an executor.
- **`GET /api/nvr_browser/days`** — authed JSON `{days: [...]}`, the available
  `YYYY-MM-DD` folders newest-first; the panel uses it to populate the Day
  dropdown, to bound the range-picker calendar, and to dot the days that have
  clips.
- **`GET /api/nvr_browser/thumb?path=<rel>`** — **authed**, generates a cached
  ffmpeg frame-grab. A plain `<img src>` can't send a bearer token, so the events
  view signs each thumb URL (`async_sign_path`, `_SIGNED_URL_TTL` = 12h, bound to
  the caller's refresh token) and the frontend uses it verbatim. `path` is
  sanitised against traversal by `_safe_rel`.
- **`GET /api/nvr_browser/clip?path=<rel>`** — **authed**, streams the original
  clip via `web.FileResponse` (honours HTTP range requests, so `<video>` seeking
  works). Same `_safe_rel` guard; the events view signs each clip URL the same way
  as thumbs (`_sign_urls`). Replaces the old public `/local/nvr/...` route.
- **`GET /api/nvr_browser/clip_proxy?path=<rel>`** — **authed**, a Roku-playable
  transcode of the clip. The source clips are ~5 MP H.264, which Roku's decoder
  can't handle (it caps H.264 at 1080p → the TV reports `-5 malformed data`), so
  this transcodes to **≤1080p H.264 + `+faststart`** on first request
  (`_generate_proxy`: VAAPI/`h264_vaapi` via `_VAAPI_DEVICE`, falling back to
  software `libx264`), caches it under `PROXY_DIR` (`_proxy_name` = `sha1.mp4`),
  and serves it range-capably. Throttled by `_PROXY_SEM` (1 at a time). Cap is
  *height* only (`scale=-2:'min(1080,ih)'`) — fine because the cameras are ~4:3,
  so width stays under Roku's 1920 limit; revisit for ultra-wide sources. The
  events list signs an extra **`proxy`** URL per clip (`_sign_urls`); the Roku app
  plays that, the web panel still uses `url`. Pruned by `_prune_proxies` (same
  keep-set as thumbs, `_valid_clip_rels`). **Originals under `/config/nvr` are
  never touched.**
- **`GET /api/nvr_browser/cameras`** — **authed**, JSON `{cameras: [{name,
  entity_id, title, available, thumb?}]}` from the `live_cameras` config map (NVR
  camera name → HA camera entity). Authoritative list for the Roku app's live-view
  picker (independent of whether a camera has clips). `available` is a liveness
  *hint* (entity present, not in `_LIVE_UNAVAILABLE_STATES`, advertises
  `CameraEntityFeature.STREAM`), not a guarantee — `/live` can still fail. `thumb`
  (added 0.9.1, **available cameras only**) is a *signed* URL to a current still —
  `async_sign_path` over HA's `/api/camera_proxy/<entity_id>` (the shared
  `_get_signer`, same mechanism as clip thumbs) so the Roku `Poster` loads it with
  no bearer; absent for a down camera (client falls back to a placeholder tile).
  Stills are served on demand by HA's camera component — no cache/prune/ffmpeg of
  our own. Empty map → `{cameras: []}`.
- **`GET /api/nvr_browser/live?camera=<name>`** — **authed**, JSON `{camera, url,
  streamFormat: "hls"}`. Roku has no WebRTC, so live uses **HLS**. The integration
  runs inside HA, so it asks the `camera` component for the stream URL
  (`camera.async_request_stream(hass, entity_id, "hls")`, capped by
  `_LIVE_STREAM_TIMEOUT` so a dead source fails fast) and returns HA's **native,
  root-relative, token-in-path HLS URL** verbatim (e.g.
  `/api/hls/<token>/master_playlist.m3u8`; the client resolves it against its base
  URL — that's why live works remotely) — **no proxy, no transcode, no go2rtc
  coupling**; HA owns the rolling-window muxing and idle teardown. Auth is
  two-tier like events→signed-clip: the bearer gates *getting* the URL, the
  stream token (HA stream views are `requires_auth = False`) gates *fetching* it,
  so the Roku `<video>` needs no header. **Gotchas:** (1) HA *remuxes, doesn't
  transcode*, so the mapped entity MUST already be ≤1080p H.264 (Roku's decoder
  limit — same `-5` failure as raw clips); point `live_cameras` at a low-res
  substream. This is enforced only by config + docs (a 5 MP entity returns `200`
  but fails on the TV). (2) The URL is **ephemeral** — HA idles a stream out ~30s
  after playback stops (and on restart), invalidating its token; the client must
  re-request `/live` on a playback error, not reuse a stale URL. Unknown/absent
  camera → `404`; unavailable/failed-to-start → `503` (never `502`). `manifest.json`
  lists `camera`/`stream` in `after_dependencies` so they load first when present.
- **TV pairing** (device-authorization flow for the Roku app; a TV can't type a
  long-lived token). Three endpoints + an in-memory pending store
  (`hass.data[DOMAIN]["pairings"]`, keyed by display code, 5-min `_PAIR_TTL`,
  capped at `_PAIR_MAX_PENDING`, purged by `_purge_pairings`):
  - **`POST /api/nvr_browser/pair/new`** — **unauthed**. Mints a short display
    code (`_PAIR_CODE_ALPHABET`, no ambiguous chars) + a 32-byte poll `secret`,
    stores a pending session, returns both. No token issued here, so leaving it
    unauthed is safe; the bound+TTL'd store caps abuse.
  - **`GET /api/nvr_browser/pair/claim?secret=`** — **unauthed**. The TV polls
    with its secret (matched via `secrets.compare_digest`); returns
    `{status: pending|approved|expired}` and, once approved, the `token`
    **once** (the session is then dropped — single-use).
  - **`POST /api/nvr_browser/pair/approve`** — **authed**. A logged-in user
    submits the code shown on the TV; mints a **long-lived access token** bound
    to *their* account (`async_create_refresh_token(..., token_type=
    TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN)` → `async_create_access_token`) and
    attaches it to the session. This is the ONLY place a token is created.
  - **Security notes:** the minted token has full HA scope (HA has no
    per-integration scoping) — only pair trusted TVs. Approval is the classic
    device-grant trust point: a user must only approve a code shown on their own
    TV (the panel copy says so). The panel's "Pair TV" button drives
    `pair/approve`; the Roku side lives in the `nvr-roku` repo.
- **Thumbnails** (`_generate_thumb`): seek ~10s in (`00:00:10`, falling back to
  `3s`/`0s` for short clips), `scale=320:-1`, throttled by `_THUMB_SEM` (3). Cache
  filename = `_thumb_name(rel)` = `sha1(rel).jpg`. **Gotcha that already bit us:**
  the atomic temp file must end in `.jpg` AND we pass `-f image2` — ffmpeg infers
  format from the extension, and a `.tmp` suffix makes every grab fail.
- **Pruning** (`_prune_thumbs`, scheduled at startup + every `PRUNE_INTERVAL`,
  default 24h): deletes cached thumbs whose source clip has rotated out (keep-set
  from `_valid_thumb_names`), and stale `.part.jpg` temps older than
  `_PART_STALE_SECONDS` (1h) without racing a live grab.
- **Deep-linking / shareable URLs** (frontend, `_filterParams`/`_applyUrlParams`/
  `_syncUrl`/`_syncFromUrl`): the panel reads its filters from
  `window.location.search` (`?camera=&object=&start=&end=`, param names matching
  the events API) so it can be opened pre-filtered from anywhere (e.g. a
  live-camera card's `navigate` tap_action), and mirrors the active filters back
  into the URL via `history.replaceState` on every filter change so the address
  bar is always a shareable link. `_filterParams()` is the single source of truth
  for which filters round-trip — add a row there (param↔state-field, optional
  validator) to expose a new one; nothing else needs to change. A deep-linked
  camera/object is pre-seeded into its facet set so the dropdown shows it even
  when the filter matches zero clips.
  - **Why it reacts to navigation, not just boot:** HA may either tear down and
    rebuild the panel element or keep and re-attach it when you leave and return —
    its choice, not guaranteed. So besides reading params in `_boot()`,
    `connectedCallback` registers `location-changed` (HA's in-app `navigate`) +
    `popstate` (back/forward) listeners and `_syncFromUrl()` re-applies any change;
    a re-attached cached instance also re-syncs on `connectedCallback`. This makes
    deep links land regardless of HA's panel-lifecycle caching. `_syncFromUrl` is
    a no-op unless params actually changed (so `_reset`→`_syncUrl`'s `replaceState`
    can't loop) and bails when `location.pathname` no longer matches the mount
    path (so navigating to another panel is ignored). Listeners are removed in
    `disconnectedCallback`.
- **Rendering safety:** `camera`/`object`/`date`/`time` all come from folder &
  file names on disk, so they're untrusted when injected into `innerHTML`. Any
  such value interpolated into markup MUST go through `_esc()` (and the object
  name, which doubles as a `.badge.<name>` CSS class, is only emitted as a class
  when it matches `^[A-Za-z0-9_-]+$`). Prefer `textContent`/element properties
  (as `_renderSelect` and `this._lbv.src` do) over raw `innerHTML` for new code.
- **Element lifecycle:** document-/window-level listeners (`location-changed`,
  `popstate`, outside-click, Escape) and the `IntersectionObserver` outlive the
  element, so they're (re)bound in `connectedCallback` from stable handler refs
  and torn down in `disconnectedCallback` — otherwise each navigated-away panel
  leaks and keeps firing app-wide. `_boot()` only builds the shadow DOM/observer
  once (guarded by `_booted`); the observer is re-`observe`d on re-attach.
- **Infinite scroll:** the `IntersectionObserver` only fires on intersection
  *transitions*, so a sentinel that never leaves the viewport (few results, or a
  tall/wide screen) won't re-trigger. `_loadMore` therefore re-calls itself after
  a successful page while `_sentinelInView()` is still true (guarded on success
  so a failing endpoint isn't hammered).
- **Playback** uses the authed `/api/nvr_browser/clip` endpoint (signed URLs) —
  the `<video src>` and Download link just use the signed `ev.url`. The frontend
  never imports HA frontend internals; it only uses `hass.callApi` + plain
  `<img>`/`<video>`.
  - **Why not `/local/`:** clips used to live under `www/` and play from HA's
    `/local/nvr/...` static route, which is served **without auth**. Moving clips
    to `/config/nvr` (v0.6.0) takes them out of `www/`, so no public route reaches
    them — the only access is the authed, signed clip endpoint. The recording
    automations must therefore write to `/config/nvr`, not `www/`.

## How the tree is parsed (the core domain logic)

The recording automations write two shapes per hour folder:

```
<date>/<hour>/<camera>/HH:MM:SS.mp4           # canonical clip; folder name = camera
<date>/<hour>/<object>/HH:MM:SS-<camera>.mp4  # HARD LINK; folder name = object label
```

`_build_hour` collapses these into events keyed by `(time, camera)`:
- file matching `BASE_RE` (`HH:MM:SS.mp4`) → the folder is a **camera**, this is the clip.
- file matching `LINK_RE` (`HH:MM:SS-<camera>.mp4`) → the folder is an **object** tag.

If you change the recording automation's naming, update `BASE_RE`/`LINK_RE` and
`_build_hour`. Only top-level dirs matching `DATE_RE` (`YYYY-MM-DD`) are scanned —
this deliberately skips the `today`/`yesterday` aliases so events aren't doubled.

## Environment constraints (important)

- Paths in `__init__.py` are hardcoded to Home Assistant's standard config dir:
  `/config/nvr` (the clips), `/config/nvr_thumbs` (thumbnail cache), and
  `/config/nvr_proxies` (transcoded Roku proxies). This holds on HAOS / Supervised
  / Container installs (config dir = `/config`); it will not match a Core/venv
  install whose config lives elsewhere.
- Thumbnails **and** clip-proxy transcodes shell out to `ffmpeg` on `PATH`. HAOS /
  Supervised / Container installs bundle `ffmpeg`; a Core install needs it
  installed separately. The proxy's hardware path also needs VAAPI (`h264_vaapi`)
  and the iGPU at `_VAAPI_DEVICE` (`/dev/dri/renderD128`) available in the
  container; if it isn't, `_generate_proxy` falls back to software `libx264`.
- Installed by dropping `custom_components/nvr_browser/` into the HA config's
  `custom_components/` (HACS does this for users). Copy the folder, never symlink.

## Testing without deploying

No-HA local checks (run from this repo):

- `python3 -m py_compile custom_components/nvr_browser/__init__.py` — Python syntax.
- `node --check custom_components/nvr_browser/nvr-browser-panel.js` — JS syntax.

`__init__.py` imports `homeassistant` at module load, so exercising the domain
logic (`_scan`, `_build_hour`, `_safe_rel`, …) needs `homeassistant` importable —
do it inside any HA install (a dev venv with `homeassistant`, or `exec` into an HA
container) against a real or sample `/config/nvr`-shaped tree, overriding `NVR_DIR`
after loading the module. Never write into a live `custom_components/` to test:

The live endpoints (`/cameras`, `/live`) can't be exercised by the `NVR_DIR`
override — they read `hass.states` and call `camera.async_request_stream`, so test
them in a running HA with `stream:` + a real camera entity in `live_cameras`: hit
`/api/nvr_browser/live?camera=<name>` and play the returned URL, and hit
`/api/nvr_browser/cameras` to confirm each available camera carries a `thumb` that
loads with no `Authorization` header.

```python
import importlib.util
spec = importlib.util.spec_from_file_location("t", "custom_components/nvr_browser/__init__.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.NVR_DIR = "/path/to/a/nvr/copy"         # point at a real or sample tree
for e in m._scan(0, 8, None, None):
    print(e["datetime"], e["camera"], e["objects"])
```

## Releasing

Distributed as a HACS-installable custom integration (users can also copy it into
`custom_components/` manually). There is **no private-host deploy step** in this
repo — cut a release by bumping the version and publishing a **GitHub Release**,
and HACS serves that release to users. User-facing install steps live in README.md.

To cut a release: bump the version (see below), commit, then
`gh release create <vX.Y.Z> --title <vX.Y.Z> --notes "<summary>"`. **HACS picks up
new versions from published GitHub Releases, not from bare git tags** — a tag
alone (e.g. `git tag` + `git push --tags`) will NOT surface the update to users,
so always publish an actual Release (`gh release create` tags and releases in one
step). Match the release tag to the bumped `version`.

HACS wiring lives in `hacs.json` (root) + the `version`/`documentation`/
`issue_tracker`/`codeowners` keys in `manifest.json`. `.github/workflows/validate.yml`
runs the **HACS** action and **hassfest** on every push/PR, so packaging
breakage (bad manifest, missing keys) is caught in CI. Keep `manifest.json` keys
ordered `domain`, `name`, then alphabetical — hassfest enforces it. The HACS job
sets `ignore: brands` (a custom-repo install needs no brand icon; to get an icon
+ default-store eligibility, submit the domain to `home-assistant/brands`).

- **Python change** (`__init__.py`): users must **restart Home Assistant** to pick
  it up.
- **Frontend-only change** (`nvr-browser-panel.js`): no restart — the panel's
  `module_url` carries `?v=<VERSION>`, so a bumped `VERSION` cache-busts on the
  next load (users hard-refresh, Cmd/Ctrl-Shift-R).

Always bump `VERSION` for a JS change so the `?v=` actually changes.

## Conventions

- **Keep this CLAUDE.md current.** Whenever you change behaviour — add/remove an
  endpoint or param, change the file layout, add a feature (filters, pruning,
  etc.), or learn a gotcha worth remembering — update the relevant section in the
  same change. Treat a stale CLAUDE.md as a bug. Update the "Current dev version"
  line when you bump `VERSION`.
- No build step / no dependencies for the frontend — keep `nvr-browser-panel.js`
  as a single vanilla custom element.
- Keep the integration read-only w.r.t. the clips dir (`/config/nvr`); the only
  thing it writes is the thumbnail cache under `/config/nvr_thumbs`.
- Bump `version` in both `manifest.json` and `VERSION` in `__init__.py` together
  (the panel's `module_url` carries `?v=<VERSION>` for cache-busting).
- **Companion client / API stability.** The HTTP API (`events`, `days`, `thumb`,
  `clip`, `clip_proxy`, `cameras`, `live`, `pair/*`) is a published contract
  consumed by the `nvr-roku` Roku app (https://github.com/skidank/nvr-roku) — not
  just the bundled panel. Prefer **additive** changes. If you make a **breaking** change (remove or
  rename an endpoint/param, change a response field's name or shape, change
  signed-URL or pairing behavior), open a tracking issue in nvr-roku so the client
  is adapted:

  ```
  gh issue create -R skidank/nvr-roku \
    --title "Adapt to ha-nvr: <what changed>" \
    --body "<link to this PR/commit>. <what the client must change>."
  ```

  The mirror reminder lives in nvr-roku's CLAUDE.md ("Compatibility with
  `ha-nvr`"). Reference repos by GitHub URL, never local paths.
