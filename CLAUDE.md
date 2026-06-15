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

Current dev version: **0.6.1**. The released (HACS) version may lag this working
tree — bump `VERSION` when cutting a release (see Releasing).

## Layout

```
nvr_browser/
├── CLAUDE.md                       # this file
├── README.md                       # user-facing: features + install steps
└── custom_components/nvr_browser/  # the installable component (HACS / copy this folder)
    ├── manifest.json               # YAML-config integration (config_flow: false)
    ├── __init__.py                 # async_setup: 3 HTTP views + ffmpeg thumbs + prune + panel
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
- **Thumbnails** (`_generate_thumb`): seek ~10s in (`00:00:10`, falling back to
  `3s`/`0s` for short clips), `scale=320:-1`, throttled by `_THUMB_SEM` (3). Cache
  filename = `_thumb_name(rel)` = `sha1(rel).jpg`. **Gotcha that already bit us:**
  the atomic temp file must end in `.jpg` AND we pass `-f image2` — ffmpeg infers
  format from the extension, and a `.tmp` suffix makes every grab fail.
- **Pruning** (`_prune_thumbs`, scheduled at startup + every `PRUNE_INTERVAL`,
  default 24h): deletes cached thumbs whose source clip has rotated out (keep-set
  from `_valid_thumb_names`), and stale `.part.jpg` temps older than
  `_PART_STALE_SECONDS` (1h) without racing a live grab.
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
  `/config/nvr` (the clips) and `/config/nvr_thumbs` (our cache). This holds
  on HAOS / Supervised / Container installs (config dir = `/config`); it will not
  match a Core/venv install whose config lives elsewhere.
- Thumbnails shell out to `ffmpeg` on `PATH`. HAOS / Supervised / Container
  installs bundle `ffmpeg`; a Core install needs it installed separately.
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
repo — cut a release by bumping the version and tagging, and HACS serves the
tagged release to users. User-facing install steps live in README.md.

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
