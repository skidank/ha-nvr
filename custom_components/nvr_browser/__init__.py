"""NVR Browser — a purpose-built gallery for the home-grown www/nvr clips.

This integration is intentionally read-only and additive. It does not touch the
recording automations or any file under www/nvr. It exposes:

  * GET /api/nvr_browser/events  (authed) — newest-first event list, paginated
  * GET /api/nvr_browser/thumb   (no auth) — ffmpeg frame-grab, cached to disk
  * a custom sidebar panel ("NVR") rendering the gallery

Clips themselves are played straight from HA's existing /local/nvr/... static
route (the files already live under www/), so no extra video serving is needed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import timedelta
from http import HTTPStatus
from urllib.parse import quote

from aiohttp import web
import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "nvr_browser"

# Paths are container-internal (HA runs with -v <root>:/config).
NVR_DIR = "/config/www/nvr"
THUMB_DIR = "/config/nvr_thumbs"

STATIC_JS_URL = "/nvr_browser_static/nvr-browser-panel.js"
PANEL_URL_PATH = "nvr-browser"
PANEL_TITLE = "NVR"
PANEL_ICON = "mdi:cctv"
WEBCOMPONENT_NAME = "nvr-browser-panel"
VERSION = "0.3.0"

# Folder/file shapes produced by the recording automations:
#   <date>/<hour>/<camera>/HH:MM:SS.mp4           -> the canonical clip
#   <date>/<hour>/<object>/HH:MM:SS-<camera>.mp4  -> hard link tagging the clip
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^\d{2}:00$")
BASE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\.mp4$")
LINK_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})-(.+)\.mp4$")

# Keep ffmpeg from storming the box: only a few frame-grabs at a time.
_THUMB_SEM = asyncio.Semaphore(3)

# How often to sweep orphaned thumbnails (whose source clip has rotated out).
PRUNE_INTERVAL = timedelta(hours=24)
# Don't delete an in-progress ".part.jpg" younger than this (avoids racing a
# concurrent frame-grab); older ones are leftovers from a crashed run.
_PART_STALE_SECONDS = 3600


def _thumb_name(rel: str) -> str:
    """Cache filename for a clip's relative path (shared by the view and pruner)."""
    return f"{hashlib.sha1(rel.encode()).hexdigest()}.jpg"

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({}, extra=vol.ALLOW_EXTRA)}, extra=vol.ALLOW_EXTRA
)


def _safe_rel(rel: str) -> str | None:
    """Normalise an incoming relative path and reject anything escaping NVR_DIR."""
    if not rel:
        return None
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm.startswith(".."):
        return None
    full = os.path.normpath(os.path.join(NVR_DIR, norm))
    if full != NVR_DIR and not full.startswith(NVR_DIR + os.sep):
        return None
    return norm


def _build_hour(date: str, hour: str) -> dict[tuple[str, str], dict]:
    """Collapse one hour folder into events keyed by (time, camera)."""
    base = os.path.join(NVR_DIR, date, hour)
    events: dict[tuple[str, str], dict] = {}
    try:
        folders = os.listdir(base)
    except OSError:
        return events

    for folder in folders:
        fdir = os.path.join(base, folder)
        if not os.path.isdir(fdir):
            continue
        try:
            files = os.listdir(fdir)
        except OSError:
            continue
        for fn in files:
            m_base = BASE_RE.match(fn)
            if m_base:
                # folder name *is* the camera; this is the canonical clip.
                t = m_base.group(1)
                cam = folder
                ev = events.setdefault((t, cam), {"time": t, "camera": cam, "objects": set()})
                ev["camera"] = cam
                ev["time"] = t
                continue
            m_link = LINK_RE.match(fn)
            if m_link:
                # folder name is an object label (person/cat/...); camera is in the name.
                t = m_link.group(1)
                cam = m_link.group(2)
                ev = events.setdefault((t, cam), {"time": t, "camera": cam, "objects": set()})
                ev["objects"].add(folder)
    return events


def _list_days() -> list[str]:
    """Return available YYYY-MM-DD folders, newest first."""
    try:
        return sorted((d for d in os.listdir(NVR_DIR) if DATE_RE.match(d)), reverse=True)
    except OSError:
        return []


def _scan(
    offset: int,
    limit: int,
    camera: str | None,
    obj: str | None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Walk the tree newest-first, returning a paginated slice of events.

    `start`/`end` are inclusive YYYY-MM-DD bounds. Date folders sort lexically,
    so plain string comparison gives correct calendar ordering.
    """
    results: list[dict] = []
    need = offset + limit
    dates = _list_days()
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    for date in dates:
        ddir = os.path.join(NVR_DIR, date)
        try:
            hours = sorted((h for h in os.listdir(ddir) if HOUR_RE.match(h)), reverse=True)
        except OSError:
            continue
        for hour in hours:
            hour_events = _build_hour(date, hour)
            for ev in sorted(hour_events.values(), key=lambda e: e["time"], reverse=True):
                if camera and ev["camera"] != camera:
                    continue
                if obj and obj not in ev["objects"]:
                    continue
                rel = f"{date}/{hour}/{ev['camera']}/{ev['time']}.mp4"
                if not os.path.isfile(os.path.join(NVR_DIR, rel)):
                    continue
                results.append(
                    {
                        "id": rel,
                        "date": date,
                        "time": ev["time"],
                        "datetime": f"{date} {ev['time']}",
                        "camera": ev["camera"],
                        "objects": sorted(ev["objects"]),
                        "url": f"/local/nvr/{quote(rel)}",
                        "thumb": f"/api/nvr_browser/thumb?path={quote(rel)}",
                    }
                )
            if len(results) >= need:
                break
        if len(results) >= need:
            break

    return results[offset:need]


async def _generate_thumb(src: str, dst: str) -> bool:
    """Grab a single frame ~10s into the clip with the container's bundled ffmpeg.

    Seeks ~10s in to maximise the chance of catching something interesting, with
    fallbacks to earlier offsets for short clips. Writes atomically.
    """
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: os.makedirs(THUMB_DIR, exist_ok=True)
    )
    # The temp file MUST keep a .jpg suffix: ffmpeg infers the output format from
    # the extension, and a bare ".tmp" makes it fail format detection. We also
    # pass "-f image2" so the format is never left to guesswork.
    tmp = f"{dst}.{os.getpid()}.part.jpg"
    last_err = b""
    async with _THUMB_SEM:
        for seek in ("00:00:10", "00:00:03", "00:00:00"):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-y", "-ss", seek, "-i", src,
                "-frames:v", "1", "-f", "image2", "-vf", "scale=320:-1", tmp,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, last_err = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                continue
            if proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, dst)
                return True
    if os.path.isfile(tmp):
        os.remove(tmp)
    _LOGGER.warning(
        "nvr_browser: thumbnail generation failed for %s: %s",
        src, last_err.decode(errors="replace")[-300:],
    )
    return False


def _valid_thumb_names() -> set[str]:
    """Cache filenames for every clip currently on disk (the keep-set)."""
    names: set[str] = set()
    for date in _list_days():
        ddir = os.path.join(NVR_DIR, date)
        try:
            hours = [h for h in os.listdir(ddir) if HOUR_RE.match(h)]
        except OSError:
            continue
        for hour in hours:
            for folder in os.listdir(os.path.join(ddir, hour)):
                fdir = os.path.join(ddir, hour, folder)
                if not os.path.isdir(fdir):
                    continue
                try:
                    files = os.listdir(fdir)
                except OSError:
                    continue
                for fn in files:
                    # Only canonical clips (camera folders) back a thumbnail URL.
                    if BASE_RE.match(fn):
                        names.add(_thumb_name(f"{date}/{hour}/{folder}/{fn}"))
    return names


def _prune_thumbs() -> int:
    """Delete cached thumbnails whose source clip has rotated out, plus any
    stale temp files. Returns the number removed."""
    try:
        entries = os.listdir(THUMB_DIR)
    except OSError:
        return 0

    keep = _valid_thumb_names()
    now = time.time()
    removed = 0
    for name in entries:
        path = os.path.join(THUMB_DIR, name)
        if name.endswith(".part.jpg"):
            # Leftover temp from a crashed grab — but don't race a live one.
            try:
                if now - os.path.getmtime(path) > _PART_STALE_SECONDS:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
            continue
        if name.endswith(".jpg") and name not in keep:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


class NvrEventsView(HomeAssistantView):
    """Authed JSON list of events, newest first."""

    url = "/api/nvr_browser/events"
    name = "api:nvr_browser:events"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            offset = max(0, int(q.get("offset", 0)))
            limit = min(200, max(1, int(q.get("limit", 60))))
        except ValueError:
            return self.json_message("invalid paging", HTTPStatus.BAD_REQUEST)
        camera = q.get("camera") or None
        obj = q.get("object") or None
        start = q.get("start") if DATE_RE.match(q.get("start", "")) else None
        end = q.get("end") if DATE_RE.match(q.get("end", "")) else None
        events = await self.hass.async_add_executor_job(
            _scan, offset, limit, camera, obj, start, end
        )
        return self.json(
            {"events": events, "offset": offset, "limit": limit, "count": len(events)}
        )


class NvrDaysView(HomeAssistantView):
    """Authed list of available YYYY-MM-DD folders, newest first."""

    url = "/api/nvr_browser/days"
    name = "api:nvr_browser:days"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        days = await self.hass.async_add_executor_job(_list_days)
        return self.json({"days": days})


class NvrThumbView(HomeAssistantView):
    """Cached JPEG thumbnail; unauthed so it can be used as a plain <img src>."""

    url = "/api/nvr_browser/thumb"
    name = "api:nvr_browser:thumb"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        rel = _safe_rel(request.query.get("path", ""))
        if not rel:
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        src = os.path.join(NVR_DIR, rel)
        if not os.path.isfile(src):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        dst = os.path.join(THUMB_DIR, _thumb_name(rel))
        if not os.path.isfile(dst):
            if not await _generate_thumb(src, dst):
                return web.Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return web.FileResponse(dst, headers={"Cache-Control": "max-age=86400"})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the API views and the custom sidebar panel."""
    js_path = os.path.join(os.path.dirname(__file__), "nvr-browser-panel.js")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_JS_URL, js_path, False)]
    )

    hass.http.register_view(NvrEventsView(hass))
    hass.http.register_view(NvrDaysView(hass))
    hass.http.register_view(NvrThumbView(hass))

    await hass.async_add_executor_job(lambda: os.makedirs(THUMB_DIR, exist_ok=True))

    async def _prune_job(_now=None) -> None:
        removed = await hass.async_add_executor_job(_prune_thumbs)
        if removed:
            _LOGGER.info("nvr_browser: pruned %d orphaned thumbnail(s)", removed)

    # Sweep once at startup, then daily. Tracks the recording retention: when a
    # day's clips are deleted, the next sweep drops their thumbnails.
    async_track_time_interval(hass, _prune_job, PRUNE_INTERVAL)
    hass.async_create_task(_prune_job())

    frontend.async_register_built_in_panel(
        hass,
        "custom",
        PANEL_TITLE,
        PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": WEBCOMPONENT_NAME,
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{STATIC_JS_URL}?v={VERSION}",
            }
        },
        require_admin=False,
    )

    _LOGGER.info("NVR Browser ready — sidebar panel at /%s", PANEL_URL_PATH)
    return True
