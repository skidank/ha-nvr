# NVR Browser

A purpose-built Home Assistant sidebar panel for browsing the home-grown
`www/nvr` motion clips — a flat, newest-first thumbnail gallery with camera and
object filters, replacing the painful Media-browser folder drill-down.

It is **read-only and additive**: it never touches the recording automations or
any file under `www/nvr`. Thumbnails are cached to a separate dir
(`/config/nvr_thumbs`).

## What it does

- **Flat newest-first feed** of every clip across all cameras, infinite-scroll.
- **Filter chips** by camera (backyard, driveway, deepyard, porch, sidewalk) and
  by detected object (person, cat, …), derived from the existing folder layout.
- **Thumbnails** generated on demand by the container's bundled `ffmpeg`
  (`-ss 1`, scaled to 320px), cached to disk, throttled to 3 concurrent grabs.
- **Click to play** inline in a lightbox, with a download link.

## How it reads the tree

The recording automations produce two shapes per hour folder:

```
<date>/<hour>/<camera>/HH:MM:SS.mp4           # the canonical clip
<date>/<hour>/<object>/HH:MM:SS-<camera>.mp4  # hard link tagging that clip
```

The integration joins them on `(time, camera)`: a folder whose files match
`HH:MM:SS.mp4` is a camera; a folder whose files match `HH:MM:SS-<camera>.mp4`
is an object label. Playback always uses the canonical clip via the existing
`/local/nvr/...` static route, so no extra video serving is added.

## Endpoints

- `GET /api/nvr_browser/events?offset=&limit=&camera=&object=` — authed JSON list.
- `GET /api/nvr_browser/thumb?path=<rel>` — JPEG thumbnail (unauthed, so it works
  as a plain `<img src>`; path is sanitised against traversal).

## Deploy (to the live HA, when ready)

The HA container only mounts `/config` (and `/share`), so a symlink into this dev
dir would dangle inside the container. **Copy** the component instead:

```bash
cp -r ~/ha-integrations/nvr_browser/custom_components/nvr_browser \
      ~/services/home-assistant/custom_components/
```

Then enable it by adding one line to `configuration.yaml`:

```yaml
nvr_browser:
```

Restart Home Assistant:

```bash
podman container stop -t 120 home-assistant   # systemd restart=unless-stopped brings it back
# or: systemctl restart <your home-assistant unit>
```

After restart an **NVR** item (cctv icon) appears in the sidebar.

To remove: delete the `nvr_browser:` line, delete
`custom_components/nvr_browser`, restart, and optionally `rm -rf
/config/nvr_thumbs` (the thumbnail cache, safe to delete anytime).

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
