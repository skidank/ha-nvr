# CLAUDE.md — nvr_browser

Guidance for working in this repo. This is a **custom Home Assistant integration**
that adds a sidebar panel for browsing the home-grown NVR clips in a live HA's
`www/nvr`. Read this before editing.

## What it is

A read-only, additive HA integration: a flat newest-first thumbnail gallery
(camera + object filters, day chips + From/To date-range picker, ffmpeg
thumbnails, click-to-play lightbox) that replaces HA's clunky Media browser for
the `www/nvr` motion clips. It must **never** modify the recording automations or
any file under `www/nvr`.

Current dev version: **0.3.0**. What's deployed to the live HA may lag — Mike
controls deploys; never assume the live copy matches this tree.

## Layout

```
nvr_browser/
├── CLAUDE.md                       # this file
├── README.md                       # user-facing: features + deploy steps
└── custom_components/nvr_browser/  # the deployable component (copy this folder)
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
  `YYYY-MM-DD` folders newest-first; the panel uses it for day chips + to bound
  the From/To date inputs.
- **`GET /api/nvr_browser/thumb?path=<rel>`** — **unauthed** (so it works as a plain
  `<img src>`), generates a cached ffmpeg frame-grab. `path` is sanitised against
  traversal by `_safe_rel`.
- **Thumbnails** (`_generate_thumb`): seek ~10s in (`00:00:10`, falling back to
  `3s`/`0s` for short clips), `scale=320:-1`, throttled by `_THUMB_SEM` (3). Cache
  filename = `_thumb_name(rel)` = `sha1(rel).jpg`. **Gotcha that already bit us:**
  the atomic temp file must end in `.jpg` AND we pass `-f image2` — ffmpeg infers
  format from the extension, and a `.tmp` suffix makes every grab fail.
- **Pruning** (`_prune_thumbs`, scheduled at startup + every `PRUNE_INTERVAL`,
  default 24h): deletes cached thumbs whose source clip has rotated out (keep-set
  from `_valid_thumb_names`), and stale `.part.jpg` temps older than
  `_PART_STALE_SECONDS` (1h) without racing a live grab.
- **Playback** uses HA's existing `/local/nvr/...` static route — no extra video
  serving. The frontend never imports HA frontend internals; it only uses
  `hass.callApi` + plain `<img>`/`<video>`.

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

- The live HA runs in a **podman container** `home-assistant`
  (`ghcr.io/home-assistant/home-assistant:stable`), config mounted `-v <root>:/config`.
  All paths in `__init__.py` are container-internal: `/config/www/nvr`,
  `/config/nvr_thumbs`.
- The container mounts only `/config` and `/share`. A symlink from
  `custom_components/` into this dev dir would dangle inside the container, so
  **deploy by copying** the `custom_components/nvr_browser` folder, never symlink.
- `ffmpeg`/`ffprobe` 6.x are present **inside the container** (not assumed on the
  host). Thumbnails shell out to `ffmpeg` on the container PATH.

## Testing without deploying

Validate against the real tree inside the running container — never write into the
live `custom_components/` to test:

```bash
podman cp custom_components/nvr_browser/__init__.py home-assistant:/tmp/nvr_init.py
podman exec -i home-assistant python3 - <<'PY'   # NOTE: -i is required for the heredoc
import importlib.util
spec = importlib.util.spec_from_file_location("t", "/tmp/nvr_init.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for e in m._scan(0, 8, None, None):
    print(e["datetime"], e["camera"], e["objects"])
PY
podman exec home-assistant rm -f /tmp/nvr_init.py
```

`python3 -m py_compile` on the host is a quick syntax check (HA deps aren't on the
host, so import-level testing must happen in the container).

## Deploy (only with Mike's explicit OK)

The target `~/services/home-assistant` is a **live deployment** — do not touch it
without his go-ahead. When approved, copy the changed file(s) into
`~/services/home-assistant/custom_components/nvr_browser/`. First-ever install
also needs `nvr_browser:` in configuration.yaml. Then:

- **Python change** (`__init__.py`): requires an **HA restart** —
  `podman container stop -t 120 home-assistant` (the `restart=unless-stopped`
  policy brings it back).
- **Frontend-only change** (`nvr-browser-panel.js`): no restart; just copy the
  file and hard-refresh the panel (Cmd/Ctrl-Shift-R) to pull the new `?v=<VERSION>`.

Bump `VERSION` for any JS change so the `?v=` cache-bust actually fires. Full
user-facing steps are in README.md.

## Conventions

- **Keep this CLAUDE.md current.** Whenever you change behaviour — add/remove an
  endpoint or param, change the file layout, add a feature (filters, pruning,
  etc.), or learn a gotcha worth remembering — update the relevant section in the
  same change. Treat a stale CLAUDE.md as a bug. Update the "Current dev version"
  line when you bump `VERSION`.
- No build step / no dependencies for the frontend — keep `nvr-browser-panel.js`
  as a single vanilla custom element.
- Keep the integration read-only w.r.t. `www/nvr`; the only thing it writes is the
  thumbnail cache under `/config/nvr_thumbs`.
- Bump `version` in both `manifest.json` and `VERSION` in `__init__.py` together
  (the panel's `module_url` carries `?v=<VERSION>` for cache-busting).
