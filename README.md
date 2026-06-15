# NVR Browser

A purpose-built Home Assistant sidebar panel for browsing the home-grown NVR
motion clips in `/config/nvr` — a flat, newest-first thumbnail gallery with
camera and object filters, replacing the painful Media-browser folder drill-down.

It is **read-only and additive**: it never touches the recording automations or
any file under `/config/nvr`. Thumbnails are cached to a separate dir
(`/config/nvr_thumbs`).

## What it does

- **Flat newest-first feed** of every clip across all cameras, infinite-scroll.
- **Filter dropdowns** for camera and detected object (person, cat, …), both
  derived from the existing folder layout (one entry per camera/object folder).
- **Thumbnails** generated on demand by Home Assistant's bundled `ffmpeg`
  (seeks ~10s in, scaled to 320px), cached to disk, throttled to 3 concurrent grabs.
- **Click to play** inline in a lightbox, with a download link.

## How it reads the tree

The recording automations produce two shapes per hour folder:

```
<date>/<hour>/<camera>/HH:MM:SS.mp4           # the canonical clip
<date>/<hour>/<object>/HH:MM:SS-<camera>.mp4  # hard link tagging that clip
```

The integration joins them on `(time, camera)`: a folder whose files match
`HH:MM:SS.mp4` is a camera; a folder whose files match `HH:MM:SS-<camera>.mp4`
is an object label. Playback always uses the canonical clip, streamed via the
authed `/api/nvr_browser/clip` endpoint (clips live outside `www/`, so they are
not exposed by HA's unauthenticated `/local/` route).

## Endpoints

- `GET /api/nvr_browser/events?offset=&limit=&camera=&object=&start=&end=` — authed
  JSON list (`start`/`end` are inclusive `YYYY-MM-DD` bounds).
- `GET /api/nvr_browser/thumb?path=<rel>` — JPEG thumbnail (authed; the events
  view hands the frontend short-lived signed URLs so a plain `<img src>` still
  works only for the logged-in user; path is sanitised against traversal).
- `GET /api/nvr_browser/clip?path=<rel>` — original clip stream (authed, signed
  the same way; supports HTTP range requests for `<video>` seeking). Replaces the
  old public `/local/nvr/...` route.

## Requirements

- A Home Assistant install whose config directory is `/config` — HAOS,
  Supervised, or the official Container image. Paths are hardcoded to
  `/config/nvr` and `/config/nvr_thumbs`, so a Core/venv install with a
  different config path won't work as-is.
- `ffmpeg` available to Home Assistant (bundled on HAOS / Supervised / Container).
- Your motion clips already under `/config/nvr/` in the layout above (your
  recording automations must write there, **not** under `www/`).

## Installation

### HACS

1. In HACS, open the ⋮ menu → **Custom repositories**.
2. Add this repository's URL with category **Integration**, then download
   **NVR Browser**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/nvr_browser/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Configuration

Enable the integration with one line in `configuration.yaml`:

```yaml
nvr_browser:
```

Restart Home Assistant. An **NVR** item (cctv icon) then appears in the sidebar.

## Deep-linking (URL parameters)

The panel reads its filters from the page URL, so you can open it pre-filtered
from anywhere — a dashboard button, a live-camera card's tap action, a Markdown
link, etc. The supported query params match the event API:

| Param    | Meaning                          | Example                |
| -------- | -------------------------------- | ---------------------- |
| `camera` | only this camera                 | `camera=front_door`    |
| `object` | only clips tagged this object    | `object=person`        |
| `start`  | inclusive lower date bound       | `start=2026-06-01`     |
| `end`    | inclusive upper date bound       | `end=2026-06-08`       |

Combine freely, e.g. `/nvr-browser?camera=front_door&object=person`. From a
Lovelace card, point a navigation at that URL:

```yaml
tap_action:
  action: navigate
  navigation_path: /nvr-browser?camera=front_door
```

The URL also stays in sync as you change filters in the panel, so the address
bar always holds a shareable, bookmarkable link to the current view.

## Removal

Remove the `nvr_browser:` line from `configuration.yaml`, uninstall via HACS (or
delete `custom_components/nvr_browser`), and restart Home Assistant. The
thumbnail cache at `/config/nvr_thumbs` is safe to delete anytime.

## Thumbnail cache cleanup

Thumbnails are cached under `/config/nvr_thumbs`, keyed by a hash of the clip
path. Because clips rotate out as the recording retention deletes old days, the
integration **prunes orphaned thumbnails** (those whose source clip no longer
exists) once at startup and every `PRUNE_INTERVAL` (default 24h). It also clears
stale `.part.jpg` temp files from any crashed grab. This tracks your retention
automatically — no manual cleanup needed. The whole cache is still safe to
`rm -rf` anytime; it just regenerates on next view.

## Tunables (in `__init__.py`)

- `_THUMB_SEM = asyncio.Semaphore(3)` — concurrent ffmpeg frame-grabs.
- `scale=320:-1` in `_generate_thumb` — thumbnail width.
- seek list in `_generate_thumb` (`00:00:10`, then fallbacks) — frame offset.
- `PRUNE_INTERVAL` — how often orphaned thumbnails are swept.
- `THUMB_DIR` — cache location.
