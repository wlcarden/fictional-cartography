"""Flask app exposing the cartography pipeline to the browser prototypes.

  GET  /                          → redirect to /place
  GET  /place, /paint             → serve the corresponding HTML from web/
  GET  /preview/<name>.jpg        → serve the last rendered preview
  GET  /api/configs               → list available map configs
  GET  /api/config/<name>         → YAML → JSON
  PUT  /api/config/<name>         → JSON → YAML (atomic write)
  POST /api/render/<name>         → trigger render, return JSON {url, ms}

Run:
    python3 -m src.server [--port 5000] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import time
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, redirect, request, send_file, send_from_directory
from PIL import Image

from src.pipeline import load_config, project_root, render_map


ROOT = project_root()
WEB_DIR = ROOT / "web"
CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = ROOT / "output"
PREVIEW_DIR = ROOT / "web" / "_previews"  # served from /preview/<name>.jpg

# Maps a workflow-stage name (as seen by the browser) to the cached PNG that
# represents that stage. `final` is special — it lives in output/, not in the
# stage cache. Stage-preview hydration writes JPEGs to PREVIEW_DIR named
# <config>__<stage>.jpg so they can be served via the existing /preview route.
STAGE_FILES: dict[str, str | None] = {
    "terrain": "terrain_styled.png",   # terrain raster only (no paper, no roads, no labels)
    "canvas":  "canvas_roads.png",     # paper + terrain + roads, NO labels
    "final":   None,                   # served from output/<name>.png
}


def _hydrate_previews_from_output() -> None:
    """Generate web/_previews/<name>.jpg for any existing render in output/.

    Tolerates truncated/corrupt PNGs (e.g. an interrupted render): logs and
    skips them so server boot never fails on a single bad file.
    """
    if not OUTPUT_DIR.exists():
        return
    for png in OUTPUT_DIR.glob("*.png"):
        preview = PREVIEW_DIR / f"{png.stem}.jpg"
        if preview.exists() and preview.stat().st_mtime >= png.stat().st_mtime:
            continue
        try:
            with Image.open(png) as im:
                im.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
                im.convert("RGB").save(preview, quality=82, optimize=True)
            print(f"  hydrated preview: {preview.name}")
        except (OSError, ValueError) as e:
            print(f"  [warn] skipped {png.name} — {type(e).__name__}: {e}")


def _stage_source_png(name: str, stage: str) -> Path | None:
    """Resolve the cached PNG for a (config, stage) pair. None if not present."""
    if stage == "final":
        src = OUTPUT_DIR / f"{name}.png"
    else:
        fn = STAGE_FILES.get(stage)
        if not fn:
            return None
        src = ROOT / "cache" / "stages" / name / fn
    return src if src.exists() else None


def _hydrate_stage_preview(name: str, stage: str) -> Path | None:
    """Make web/_previews/<name>__<stage>.jpg from the cached source PNG.

    Returns the JPEG path if the source exists (regenerating only when stale),
    or None if the stage hasn't been rendered yet.
    """
    src = _stage_source_png(name, stage)
    if src is None:
        return None
    preview = PREVIEW_DIR / f"{name}__{stage}.jpg"
    if preview.exists() and preview.stat().st_mtime >= src.stat().st_mtime:
        return preview
    with Image.open(src) as im:
        im.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
        im.convert("RGB").save(preview, quality=82, optimize=True)
    return preview


def create_app() -> Flask:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    _hydrate_previews_from_output()
    app = Flask(__name__, static_folder=None)

    # ---- prototype HTML pages ------------------------------------------------
    @app.get("/")
    def index():
        return redirect("/place")

    @app.get("/place")
    def place_page():
        return send_from_directory(WEB_DIR, "place.html")

    @app.get("/paint")
    def paint_page():
        return send_from_directory(WEB_DIR, "paint.html")

    @app.get("/routes")
    def routes_page():
        return send_from_directory(WEB_DIR, "routes.html")

    @app.get("/decoration")
    def decoration_page():
        return send_from_directory(WEB_DIR, "decoration.html")

    @app.get("/narrative")
    def narrative_page():
        return send_from_directory(WEB_DIR, "narrative.html")

    @app.get("/_shared.js")
    def shared_js():
        # Shared editor library: Save (debounced PUT) + History (undo/redo)
        # + Cmd-Z keybindings. Loaded by every editor page.
        resp = send_from_directory(WEB_DIR, "_shared.js", max_age=0)
        resp.headers["Content-Type"] = "application/javascript"
        return resp

    @app.get("/render")
    def render_page():
        return send_from_directory(WEB_DIR, "render.html")

    @app.get("/setup")
    def setup_page():
        return send_from_directory(WEB_DIR, "setup.html")

    # ---- preview images ------------------------------------------------------
    @app.get("/preview/<path:filename>")
    def preview(filename: str):
        return send_from_directory(PREVIEW_DIR, filename, max_age=0)

    # ---- config CRUD ---------------------------------------------------------
    @app.get("/api/render-info/<name>")
    def render_info(name: str):
        """Effective post-aspect-crop bounds + terrain/canvas dims for a config.

        Reads the stage-1 cache (terrain_dem.meta.json), which the pipeline
        writes whenever it loads / re-uses the DEM. Returns a 404 if the
        config has never been rendered yet.
        """
        # Validate name (same rules as _config_path).
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        cache_path = ROOT / "cache" / "stages" / name / "terrain_dem.meta.json"
        if not cache_path.exists():
            abort(404, "config has not been rendered yet — POST /api/render first")
        meta = json.loads(cache_path.read_text())
        h = int(meta.get("h", 0))
        w = int(meta.get("w", 0))
        # Paper border isn't stored explicitly; read it from the YAML.
        cfg = load_config(_config_path(name))
        border = int(cfg.get("canvas", {}).get("border", 100))
        return jsonify({
            "bounds":    meta.get("bounds", {}),
            "terrain_h": h,
            "terrain_w": w,
            "border":    border,
            "canvas_h":  h + 2 * border,
            "canvas_w":  w + 2 * border,
        })

    @app.get("/api/stage-preview/<name>/<stage>")
    def stage_preview(name: str, stage: str):
        """JPEG preview for a workflow stage of a config.

        Stages: 'terrain' (terrain_styled, no paper/roads/labels),
                'canvas'  (canvas_roads, paper + terrain + roads, NO labels),
                'final'   (output/<name>.png — fully rendered map).

        Returns 404 if the stage hasn't been rendered yet, so callers can
        gracefully fall back (e.g., to `final` which is the most likely to
        exist since it's what /api/render produces).
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        if stage not in STAGE_FILES:
            abort(400, f"invalid stage {stage!r}; expected one of {list(STAGE_FILES)}")
        p = _hydrate_stage_preview(name, stage)
        if p is None:
            abort(404, f"stage {stage!r} not cached for {name!r}")
        import uuid
        return jsonify({
            "url":   f"/preview/{p.name}?v={uuid.uuid4().hex[:10]}",
            "stage": stage,
        })

    @app.get("/api/roads-vector/<name>")
    def roads_vector(name: str):
        """Return parsed routes as JSON for client-side SVG rendering.

        The pick-mode UI draws every road as a thin fluorescent line on
        engagement; we serve the same data the pipeline uses (parsed
        Overpass routes for the configured networks). Cached on disk
        per-config so subsequent loads are <100ms.

        Returns:
          {
            "routes": [
              {"network": "US:I", "ref": "95",
               "segments": [[[lat,lon],[lat,lon],...], ...]},
              ...
            ],
            "bounds": {lat_n, lat_s, lon_w, lon_e},
            "n_routes": int,
            "n_points": int,
          }
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        cfg_path = _config_path(name)
        cfg = load_config(cfg_path)
        roads_cfg = cfg.get("roads") or {}
        # Use the same default networks the pipeline does.
        networks = roads_cfg.get("networks", ["US:I", "US:US"])
        bounds_dict = cfg.get("bounds", {})

        from src.pipeline import MapBounds
        bounds = MapBounds(
            lat_n=float(bounds_dict["lat_n"]), lat_s=float(bounds_dict["lat_s"]),
            lon_w=float(bounds_dict["lon_w"]), lon_e=float(bounds_dict["lon_e"]),
        )
        from src.data.overpass import fetch_routes, parse_routes
        try:
            routes_data = fetch_routes(bounds, networks, cache_name=f"routes_{name}")
            routes = parse_routes(routes_data)
        except Exception as e:
            return jsonify({"routes": [], "bounds": bounds_dict,
                            "warnings": [f"fetch failed: {e}"]}), 500

        # routes is {(network, ref): [[(lat, lon), ...], ...]}.
        # Flatten to a JSON-friendly list keyed by network/ref.
        out_routes = []
        n_points = 0
        for (network, ref), segs in routes.items():
            seg_list = []
            for seg in segs:
                if len(seg) < 2:
                    continue
                pts = [[float(la), float(lo)] for (la, lo) in seg]
                seg_list.append(pts)
                n_points += len(pts)
            if not seg_list:
                continue
            out_routes.append({
                "network": network, "ref": ref, "segments": seg_list,
            })

        return jsonify({
            "routes": out_routes,
            "bounds": bounds_dict,
            "n_routes": len(out_routes),
            "n_points": n_points,
        })

    @app.post("/api/snap/<name>")
    def snap_to_road(name: str):
        """Find the nearest OSM road graph node to a (lat, lon) input.

        Body:  {lat, lon, road_types?: [str, ...]}
        Returns:
          {
            "snapped_lat": float, "snapped_lon": float,
            "node_id": int, "in_main_component": bool,
            "component_size": int, "main_component_size": int,
            "distance_deg": float,
          }
        Returns 404 if config has no road graph cache.
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        body = request.get_json(force=True) or {}
        lat = float(body.get("lat", 0))
        lon = float(body.get("lon", 0))
        cfg_path = _config_path(name)
        cfg = load_config(cfg_path)
        road_types = body.get("road_types") or _default_barrier_road_types(cfg)

        graph = _ensure_graph_cached(name, cfg, road_types)
        if graph is None:
            abort(404, "road graph unavailable for this config")
        nodes, adj, comp = graph

        from src.styling.barriers import find_nearest_node
        nid = find_nearest_node(nodes, lat, lon)
        if nid is None:
            abort(404, "empty graph")
        nlat, nlon = nodes[nid]
        comp_id = comp.get(nid)
        comp_sizes = _component_sizes(comp)
        main_size = comp_sizes.get(0, 0)
        comp_size = comp_sizes.get(comp_id, 0)
        d2 = (lat - nlat) ** 2 + (lon - nlon) ** 2
        return jsonify({
            "snapped_lat": float(nlat),
            "snapped_lon": float(nlon),
            "node_id": int(nid),
            "in_main_component": comp_id == 0,
            "component_size": int(comp_size),
            "main_component_size": int(main_size),
            "distance_deg": float(d2 ** 0.5),
        })

    @app.post("/api/contamination-sources/<name>")
    def contamination_sources(name: str):
        """Fetch the OSM node sources for a contamination configuration.

        Body (all optional — falls back to cfg.contamination.sources):
          {query?: str, refresh?: bool}

        Returns:
          {
            "sources": [[lat, lon], ...],  # within bounds + small overscan
            "out_of_bounds": int,            # nodes outside the canvas
            "total": int,
            "warnings": [str, ...],
          }

        The Overpass result is disk-cached by `contamination_<name>_<query-hash>`
        so re-clicking FETCH SOURCES is instant. `refresh: true` forces a
        cache miss for cases where the OSM data has changed upstream.
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        body = request.get_json(silent=True) or {}
        cfg_path = _config_path(name)
        cfg = load_config(cfg_path)
        contam_cfg = cfg.get("contamination") or {}
        sources_cfg = contam_cfg.get("sources") or {}
        # If body has its own query (live edit not yet saved), prefer it
        raw_query = body.get("query") or sources_cfg.get("query") or ""
        node_filter = raw_query.strip()
        if node_filter.startswith("node"):
            node_filter = node_filter[4:]
        if not node_filter:
            return jsonify({"sources": [], "out_of_bounds": 0,
                            "total": 0,
                            "warnings": ["empty query — set sources.query"]})

        bounds_dict = cfg.get("bounds") or {}
        from src.pipeline import MapBounds
        bounds = MapBounds(
            lat_n=float(bounds_dict["lat_n"]), lat_s=float(bounds_dict["lat_s"]),
            lon_w=float(bounds_dict["lon_w"]), lon_e=float(bounds_dict["lon_e"]),
        )
        # Mirror the pipeline's cache naming so the editor reuses the
        # exact JSON the renderer produced. Pipeline uses:
        #   f"contamination_{config_name}_{safe_name}"  where safe_name
        # is derived from cfg.contamination.name.
        # When body has a custom (unsaved) query that differs from the
        # config, fall back to a hash so we don't clobber the saved
        # cache.
        saved_query = (sources_cfg.get("query") or "").strip()
        if raw_query == saved_query and contam_cfg.get("name"):
            contam_name = contam_cfg["name"]
            safe_name = "".join(c if c.isalnum() else "_"
                                 for c in contam_name).strip("_")[:40]
            cache_name = f"contamination_{name}_{safe_name}"
        else:
            import hashlib
            h = hashlib.md5(node_filter.encode()).hexdigest()[:8]
            cache_name = f"contamination_{name}_{h}"

        from src.data.overpass import fetch_osm_nodes
        try:
            data = fetch_osm_nodes(bounds, node_filter, cache_name)
        except Exception as e:
            return jsonify({"sources": [], "out_of_bounds": 0, "total": 0,
                            "warnings": [f"overpass failed: {e}"]}), 500

        elements = (data or {}).get("elements", [])
        sources = []
        out_of_bounds = 0
        # Match the pipeline's overscan tolerance (0.05°) so previews match
        # exactly what compute_intensity will use.
        overscan = 0.05
        for el in elements:
            if el.get("type") != "node":
                continue
            try:
                lat = float(el["lat"])
                lon = float(el["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if (bounds.lat_s - overscan <= lat <= bounds.lat_n + overscan and
                bounds.lon_w - overscan <= lon <= bounds.lon_e + overscan):
                sources.append([lat, lon])
            else:
                out_of_bounds += 1

        return jsonify({
            "sources": sources,
            "out_of_bounds": out_of_bounds,
            "total": len(sources) + out_of_bounds,
            "query": raw_query,
            "warnings": [] if sources else
                ["no sources matched the query in this bounds"],
        })

    @app.post("/api/barrier-preview/<name>")
    def barrier_preview(name: str):
        """Run A* with proposed endpoint config and return geometry.

        Body schema (one of):
          {barrier_index: int, override?: {endpoints | corridor | ...}}
            — replays the existing barriers[i] config with optional fields
              overridden, e.g. just-set endpoints during pick mode
          {method, endpoints, corridor?, road_types?, ...}
            — full config-style payload, useful for "what-if" previews

        Returns:
          {
            path: [[lat,lon],...],
            length_km: float,
            used_main_component: bool,
            warnings: [str, ...],
          }

        This is the dry-run backing for pick mode: after the user sets both
        endpoints, we POST here to get the path before saving.
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        body = request.get_json(force=True) or {}
        cfg_path = _config_path(name)
        cfg = load_config(cfg_path)

        # Resolve to an effective barrier config + the list of barriers
        # to run before reaching it. astar_offset barriers depend on
        # earlier barriers being present in `prior_paths`, so we replay
        # all barriers up to and including the target index, applying
        # the override only at the target.
        barriers = cfg.get("barriers") or []
        prior_run: list[dict] = []
        if "barrier_index" in body:
            idx = int(body["barrier_index"])
            if idx < 0 or idx >= len(barriers):
                abort(400, f"barrier_index {idx} out of range")
            override = body.get("override") or {}
            target = dict(barriers[idx])
            for k, v in override.items():
                target[k] = v
            target.setdefault("name", "__preview_target__")
            # All barriers BEFORE the target run unmodified (their saved
            # configs); the target runs with the override applied.
            for i, b in enumerate(barriers):
                if i < idx:
                    prior_run.append(b)
                elif i == idx:
                    prior_run.append(target)
                    break
            target_name = target["name"]
        else:
            # Free-form config — no prior context. astar_offset would
            # need its reference_barrier provided in body if this path
            # is used; today only astar_road_network is exercised here.
            b_cfg = dict(body)
            b_cfg.setdefault("name", "__preview_target__")
            prior_run = [b_cfg]
            target_name = b_cfg["name"]

        # Load bounds + water mask + dimensions same as the barrier-paths
        # endpoint does. Skip path trimming if water mask is absent.
        cache_path = ROOT / "cache" / "stages" / name / "terrain_dem.meta.json"
        if not cache_path.exists():
            abort(404, "render once before previewing barriers")
        meta = json.loads(cache_path.read_text())
        bounds_dict = meta.get("bounds", {})
        h = int(meta.get("h", 0))
        w = int(meta.get("w", 0))

        from src.pipeline import MapBounds
        bounds = MapBounds(
            lat_n=float(bounds_dict["lat_n"]), lat_s=float(bounds_dict["lat_s"]),
            lon_w=float(bounds_dict["lon_w"]), lon_e=float(bounds_dict["lon_e"]),
        )
        import numpy as np
        water_path = ROOT / "cache" / "stages" / name / "terrain_styled.npy"
        water = np.load(water_path) if water_path.exists() else None

        # Run the prior_run sequence so astar_offset can find its
        # reference. Slight extra cost (~5s for The Wall preview when
        # previewing The Patrol Line) — accepted because previews are
        # explicit user actions, not slider drag fallout.
        from src.styling.barriers import compute_barrier_paths
        try:
            out = compute_barrier_paths(
                prior_run, bounds,
                cache_name_prefix=f"barriers_{name}",
                water_mask=water, terrain_h=h or None, terrain_w=w or None,
            )
        except Exception as e:
            return jsonify({"path": [], "length_km": 0,
                            "warnings": [f"preview failed: {e}"]}), 500
        path = out.get(target_name) or []

        return jsonify({
            "path": [[float(la), float(lo)] for (la, lo) in path],
            "length_km": _path_length_km_jsonable(path),
            "used_main_component": True,   # compute_barrier_paths always uses main comp
            "warnings": [] if path else
                ["A* returned no path — endpoints may be too far apart, "
                 "or the corridor penalty too restrictive"],
        })

    @app.get("/api/barrier-paths/<name>")
    def barrier_paths(name: str):
        """Return computed barrier paths for a config as JSON.

        Used by the Narrative page to render barrier polylines as SVG
        overlays without re-rendering the full map. Reuses the on-disk
        OSM road graph cache + the same compute_barrier_paths function
        the pipeline calls.

        Returns:
          {
            "barriers": [{"name": str, "path": [[lat, lon], ...],
                          "style": dict, "label": dict, "method": str,
                          "length_km": float, "trimmed_underwater": int}],
            "buffer_zone": {"between": [a, b], "polygon": [[lat,lon],...] | null}
                | null,
            "bounds": {lat_n, lat_s, lon_w, lon_e},
            "warnings": [str, ...]
          }

        Returns 404 if the config has not been rendered yet (since we
        need the cached water_mask to trim paths to land).
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        cache_path = ROOT / "cache" / "stages" / name / "terrain_dem.meta.json"
        if not cache_path.exists():
            abort(404, "config not yet rendered — render once to populate the cache")
        cfg_path = _config_path(name)
        cfg = load_config(cfg_path)
        cfg_barriers = cfg.get("barriers") or []
        if not cfg_barriers:
            return jsonify({
                "barriers": [],
                "buffer_zone": None,
                "bounds": json.loads(cache_path.read_text()).get("bounds", {}),
                "warnings": ["no barriers configured"],
            })

        # Load DEM + water mask exactly as the pipeline would, but stop
        # before stage 2 styling — we only need geometry for the path
        # computation. The water mask is needed for path trimming.
        meta = json.loads(cache_path.read_text())
        bounds_dict = meta.get("bounds", {})
        h = int(meta.get("h", 0))
        w = int(meta.get("w", 0))

        from src.pipeline import MapBounds
        bounds = MapBounds(
            lat_n=float(bounds_dict["lat_n"]), lat_s=float(bounds_dict["lat_s"]),
            lon_w=float(bounds_dict["lon_w"]), lon_e=float(bounds_dict["lon_e"]),
        )

        # Water mask is saved alongside the terrain_styled stage as
        # `<name>/terrain_styled.npy` (per cache.save_image_and_mask).
        # Fall back to recomputing if the stage hasn't been rendered.
        import numpy as np
        water_mask_path = ROOT / "cache" / "stages" / name / "terrain_styled.npy"
        if water_mask_path.exists():
            water = np.load(water_mask_path)
        else:
            # Recompute. This is the same logic the pipeline runs.
            from src.terrain.modifications import (
                water_mask as compute_water_mask, apply_sinking,
            )
            dem = np.load(ROOT / "cache" / "stages" / name / "terrain_dem.npy")
            sea = float(cfg.get("terrain", {}).get("sea_level_rise", 0))
            water = compute_water_mask(dem, sea)
            sink_cfg = cfg.get("sinking") or {}
            if sink_cfg.get("enabled"):
                water, _ = apply_sinking(water, sink_cfg, bounds, ROOT)

        from src.styling.barriers import compute_barrier_paths, band_mask
        try:
            paths = compute_barrier_paths(
                cfg_barriers, bounds, cache_name_prefix=f"barriers_{name}",
                water_mask=water, terrain_h=h, terrain_w=w,
            )
        except Exception as e:
            return jsonify({
                "barriers": [], "buffer_zone": None,
                "bounds": bounds_dict,
                "warnings": [f"path computation failed: {e}"],
            }), 500

        out_barriers = []
        for b_cfg in cfg_barriers:
            b_name = b_cfg.get("name") or "<unnamed>"
            path = paths.get(b_name)
            entry = {
                "name": b_name,
                "method": b_cfg.get("method"),
                "type": b_cfg.get("type"),
                "style": b_cfg.get("style") or {},
                "label": b_cfg.get("label"),
                "path": [[lat, lon] for (lat, lon) in (path or [])],
                "length_km": _path_length_km_jsonable(path) if path else 0,
            }
            out_barriers.append(entry)

        # Buffer zone polygon (if configured)
        buffer_out = None
        bz = cfg.get("buffer_zone") or {}
        if bz.get("enabled"):
            between = bz.get("between") or []
            if len(between) == 2:
                a_path = paths.get(between[0])
                b_path = paths.get(between[1])
                if a_path and b_path:
                    polygon = list(a_path) + list(reversed(b_path))
                    buffer_out = {
                        "between": between,
                        "polygon": [[lat, lon] for (lat, lon) in polygon],
                        "hatching": bz.get("hatching"),
                        "symbols": bz.get("symbols"),
                        "glyphs": bz.get("glyphs"),
                    }

        return jsonify({
            "barriers": out_barriers,
            "buffer_zone": buffer_out,
            "bounds": bounds_dict,
            "warnings": [],
        })

    @app.get("/api/configs")
    def list_configs():
        names = sorted(
            p.stem for p in CONFIG_DIR.glob("*.yaml")
            if not p.stem.startswith("_")
        )
        return jsonify(names)

    @app.get("/api/config/<name>")
    def get_config(name: str):
        path = _config_path(name)
        return jsonify(load_config(path))

    @app.put("/api/config/<name>")
    def put_config(name: str):
        path = _config_path(name, must_exist=False)
        body = request.get_json(force=True)
        if not isinstance(body, dict):
            abort(400, "expected a JSON object")
        # Atomic write: serialize to temp file then rename.
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, dir=path.parent, suffix=".yaml.tmp"
        ) as tf:
            yaml.safe_dump(body, tf, sort_keys=False, default_flow_style=False)
            tmp = tf.name
        os.replace(tmp, path)
        return jsonify({"ok": True, "path": str(path)})

    # ---- render --------------------------------------------------------------
    SUPPORTED_FORMATS = ("png", "jpg", "jpeg", "webp", "tif", "tiff", "pdf")

    def _validate_render_opts(opts: dict) -> dict:
        """Shared validation for /api/render and /api/render-queue.

        Returns a normalized dict; aborts with 400 on any invalid value.
        Centralizing this avoids the two endpoints drifting apart.
        """
        scale = int(opts.get("scale", 1))
        if scale not in (1, 2, 4, 8):
            abort(400, "scale must be one of 1, 2, 4, 8")
        fmt = str(opts.get("format", "png")).lower()
        if fmt not in SUPPORTED_FORMATS:
            abort(400, f"format must be one of {SUPPORTED_FORMATS}")
        quality = int(opts.get("quality", 88))
        if not 60 <= quality <= 100:
            abort(400, "quality must be between 60 and 100")
        # DPI: 0/null disables; otherwise a sane print-DPI range. We don't
        # try to validate against the output dimensions (that's for the
        # caller to surface).
        dpi_raw = opts.get("dpi")
        dpi = int(dpi_raw) if dpi_raw not in (None, "", 0) else None
        if dpi is not None and not 72 <= dpi <= 1200:
            abort(400, "dpi must be between 72 and 1200, or null")
        embed_metadata = bool(opts.get("embed_metadata", False))
        reference_mode = bool(opts.get("reference", False))
        return {
            "scale": scale,
            "format": fmt,
            "quality": quality,
            "dpi": dpi,
            "embed_metadata": embed_metadata,
            "reference_mode": reference_mode,
        }

    @app.post("/api/render/<name>")
    def render(name: str):
        """Render a map. Body (all optional):
            scale:           int in {1,2,4,8}     — 1 = working res, 4 = full res
            format:          png|jpg|webp|tiff|pdf — default "png"
            quality:         int 60-100           — JPEG/WebP only, default 88
            dpi:             int 72-1200 or null  — embed physical DPI hint
            embed_metadata:  bool                 — write title/author/credit + render time
            reference:       bool                 — overlay state borders + reference cities

        The full-size output goes to output/<name>.<ext> regardless of
        format. A small preview JPEG is always written alongside so the
        editor pages can show a fresh thumbnail.
        """
        path = _config_path(name)
        opts = _validate_render_opts(request.get_json(silent=True) or {})
        scale = opts["scale"]
        fmt = opts["format"]
        quality = opts["quality"]

        t0 = time.perf_counter()
        out_path = render_map(
            config_path=path,
            scale_factor=scale,
            output_format=fmt,
            jpeg_quality=quality,
            reference_mode=opts["reference_mode"],
            dpi=opts["dpi"],
            embed_metadata=opts["embed_metadata"],
        )
        ms_render = int((time.perf_counter() - t0) * 1000)

        # Generate the small editor-preview JPEG. When the full output is
        # already a JPEG we still need a thumbnail-sized one (the full-size
        # JPEG can be tens of MB at scale 4+).
        #
        # PDF can't be opened by Pillow without Ghostscript, so for PDF/PS
        # outputs we read the cached final-stage PNG instead. That stage
        # is saved unconditionally by render_map() at the same dimensions,
        # so the thumbnail is identical to what you'd get from the PDF.
        preview_path = PREVIEW_DIR / f"{name}.jpg"
        thumb_source = out_path
        if out_path.suffix.lower() in (".pdf", ".ps", ".eps"):
            cached_final = ROOT / "cache" / "stages" / name / "final.png"
            if cached_final.exists():
                thumb_source = cached_final
        with Image.open(thumb_source) as im:
            im.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
            im.convert("RGB").save(preview_path, quality=82, optimize=True)
        ms_total = int((time.perf_counter() - t0) * 1000)

        try:
            output_size = out_path.stat().st_size
        except OSError:
            output_size = 0

        # The on-disk extension may differ from the format alias we
        # received (e.g. fmt="jpeg" → file is .jpg, fmt="tif" → file is
        # .tiff). Read it from the actual saved path so download links
        # always work.
        out_ext = out_path.suffix.lstrip(".")

        import uuid
        return jsonify({
            "url":         f"/preview/{name}.jpg?v={uuid.uuid4().hex[:10]}",
            "render_ms":   ms_render,
            "total_ms":    ms_total,
            "png":         str(out_path.relative_to(ROOT)),  # legacy field name; may be .jpg/.webp/.tiff/.pdf now
            "output_path": str(out_path.relative_to(ROOT)),
            "output_url":  f"/api/output/{name}.{out_ext}",
            "output_size": output_size,
            "format":      fmt,
            "extension":   out_ext,
            "scale":       scale,
            "quality":     quality if fmt in ("jpg", "jpeg", "webp") else None,
            "dpi":         opts["dpi"],
            "embed_metadata": opts["embed_metadata"],
            "reference":   opts["reference_mode"],
        })

    @app.post("/api/render-queue/<name>")
    def render_queue(name: str):
        """Queue a render job and return immediately with a job_id.

        Body shape matches POST /api/render/<name>:
          {scale?, format?, quality?, dpi?, embed_metadata?, reference?}

        Returns:
          {job_id: str}

        The client polls GET /api/render-status/<job_id> for progress.
        Distinct from synchronous /api/render which blocks until done.
        """
        path = _config_path(name)   # validates name and existence
        opts = _validate_render_opts(request.get_json(silent=True) or {})

        _gc_old_jobs()
        job_id = _create_render_job(name, opts)
        return jsonify({"job_id": job_id})

    @app.get("/api/render-status/<job_id>")
    def render_status(job_id: str):
        """Get the status of a queued render job.

        Returns:
          {state: 'queued'|'running'|'done'|'error',
           stages: [{stage, type, ts}, ...],
           result?: {...},          # when done
           error?: str,             # when error
           elapsed_ms: int,
          }

        404 if job_id is unknown.
        """
        with _RENDER_LOCK:
            job = _RENDER_JOBS.get(job_id)
            if job is None:
                abort(404, f"unknown job_id {job_id!r}")
            # Snapshot under lock so the caller doesn't see partial state
            import time
            elapsed = 0
            if job.get("started_at"):
                end = job.get("completed_at") or time.time()
                elapsed = int((end - job["started_at"]) * 1000)
            snapshot = {
                "id": job["id"],
                "config": job["config"],
                "state": job["state"],
                "stages": list(job["stages"]),
                "elapsed_ms": elapsed,
                "result": job.get("result"),
                "error": job.get("error"),
            }
        return jsonify(snapshot)

    @app.get("/api/output/<filename>")
    def download_output(filename: str):
        """Serve a rendered map as a download.

        Filename must be `<config>.<ext>` where ext is one of the supported
        export formats (png, jpg, webp, tiff, pdf). Anything with path
        separators is rejected to prevent escape from output/.
        """
        if "/" in filename or ".." in filename or filename.startswith("."):
            abort(400, "invalid filename")
        allowed_exts = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".pdf")
        if not filename.endswith(allowed_exts):
            abort(400, f"extension must be one of {allowed_exts}")
        path = (OUTPUT_DIR / filename).resolve()
        if not str(path).startswith(str(OUTPUT_DIR.resolve())):
            abort(400, "invalid filename")
        if not path.exists():
            abort(404, f"{filename!r} not rendered yet")
        return send_from_directory(
            OUTPUT_DIR, filename, as_attachment=True, max_age=0,
        )

    # ─── Per-stage downloads ───────────────────────────────────────────
    # The pipeline saves each cached stage as a PNG under
    #   cache/stages/<config>/<stage>.png
    # These are useful for layered work in image editors: download the
    # bare paper canvas (terrain_styled), the terrain+roads layer, or the
    # full final composite, and combine them externally.

    EXPORTABLE_STAGES = ("terrain_styled", "canvas_roads", "final")

    @app.get("/api/stages/<name>")
    def list_stages(name: str):
        """Return a manifest of which cached stages exist for this config.

        Response shape:
          [{stage: str, available: bool, size: int, width: int, height: int}]

        The UI uses this to decide which per-stage download links to show.
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        stage_dir = ROOT / "cache" / "stages" / name
        out = []
        for stage in EXPORTABLE_STAGES:
            png = stage_dir / f"{stage}.png"
            if png.exists():
                meta_path = stage_dir / f"{stage}.meta.json"
                w = h = 0
                if meta_path.exists():
                    try:
                        m = json.loads(meta_path.read_text())
                        w = int(m.get("w", 0))
                        h = int(m.get("h", 0))
                    except (ValueError, OSError):
                        pass
                out.append({
                    "stage": stage,
                    "available": True,
                    "size": png.stat().st_size,
                    "width": w,
                    "height": h,
                })
            else:
                out.append({
                    "stage": stage, "available": False,
                    "size": 0, "width": 0, "height": 0,
                })
        return jsonify(out)

    @app.get("/api/stage-output/<name>/<stage>")
    def download_stage(name: str, stage: str):
        """Serve a cached pipeline stage as a downloadable PNG.

        `stage` must be one of EXPORTABLE_STAGES — anything else is
        rejected (in particular `terrain_dem` which is a numpy array,
        not an image).
        """
        if "/" in name or ".." in name or name.startswith("."):
            abort(400, "invalid config name")
        if stage not in EXPORTABLE_STAGES:
            abort(400, f"stage must be one of {EXPORTABLE_STAGES}")
        stage_dir = ROOT / "cache" / "stages" / name
        png = stage_dir / f"{stage}.png"
        if not png.exists():
            abort(404, f"stage {stage!r} not cached — render once to populate")
        return send_from_directory(
            stage_dir,
            f"{stage}.png",
            as_attachment=True,
            download_name=f"{name}_{stage}.png",
            max_age=0,
        )

    return app


# Module-level cache of road graphs (nodes, adj, components_dict) keyed
# by (config_name, sorted_road_types_tuple). Populated lazily by the
# snap and barrier-preview endpoints; reused across requests so we don't
# re-parse 1.5M-node OSM data on every mousemove-driven snap call.
_GRAPH_CACHE: dict[tuple, tuple] = {}


def _default_barrier_road_types(cfg: dict) -> list:
    """If a barrier exists with road_types, use the first one's; else default."""
    barriers = cfg.get("barriers") or []
    for b in barriers:
        rts = b.get("road_types")
        if rts:
            return list(rts)
    return ["motorway", "trunk", "primary", "secondary", "tertiary", "residential"]


def _ensure_graph_cached(name: str, cfg: dict, road_types: list):
    """Build (or return cached) (nodes, adj, components_dict) for a config."""
    key = (name, tuple(sorted(road_types)))
    if key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]
    bounds_dict = cfg.get("bounds") or {}
    if not bounds_dict:
        return None
    from src.pipeline import MapBounds
    bounds = MapBounds(
        lat_n=float(bounds_dict["lat_n"]), lat_s=float(bounds_dict["lat_s"]),
        lon_w=float(bounds_dict["lon_w"]), lon_e=float(bounds_dict["lon_e"]),
    )
    from src.data.overpass import fetch_road_graph
    from src.styling.barriers import build_road_graph, _connected_components
    cache_name = f"barriers_{name}_{'_'.join(sorted(road_types))[:60]}"
    try:
        data = fetch_road_graph(bounds, list(road_types), cache_name)
    except Exception:
        return None
    nodes, adj = build_road_graph(data or {})
    if not nodes:
        return None
    comp = _connected_components(adj, nodes)
    _GRAPH_CACHE[key] = (nodes, adj, comp)
    return _GRAPH_CACHE[key]


def _component_sizes(comp_dict: dict) -> dict:
    """Returns {component_id → size}."""
    out: dict = {}
    for cid in comp_dict.values():
        out[cid] = out.get(cid, 0) + 1
    return out


# ============================================================
#  Render queue (in-process)
# ============================================================
# Module-level state for the render queue. Each job goes through:
#   queued → running (with stage updates) → done OR error
# In-process means: server restart loses in-flight jobs. For a local
# dev tool that's acceptable; document upgrade path (Redis/SQLite) when
# multi-host deployment becomes a goal.
import threading
import uuid

_RENDER_LOCK = threading.Lock()
_RENDER_JOBS: dict[str, dict] = {}
_RENDER_THREADS: dict[str, threading.Thread] = {}
# Stage names the pipeline emits in its print() output. Used by the
# print-hook to derive structured progress events.
_PIPELINE_STAGE_MARKERS = (
    ("[hit]  terrain_dem",    "terrain_dem", "hit"),
    ("[miss] terrain_dem",    "terrain_dem", "miss"),
    ("[hit]  terrain_styled", "terrain_styled", "hit"),
    ("[miss] terrain_styled", "terrain_styled", "miss"),
    ("[hit]  canvas_roads",   "canvas_roads", "hit"),
    ("[miss] canvas_roads",   "canvas_roads", "miss"),
    ("[hit]  final",          "final", "hit"),
    ("[miss] final",          "final", "miss"),
)


def _create_render_job(name: str, opts: dict) -> str:
    """Allocate a job_id, register state, and start a worker thread."""
    job_id = uuid.uuid4().hex
    with _RENDER_LOCK:
        _RENDER_JOBS[job_id] = {
            "id": job_id,
            "config": name,
            "state": "queued",
            "stages": [],   # list of {stage, type ('hit'|'miss'|'done'), ts}
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "opts": dict(opts),
        }
    thread = threading.Thread(
        target=_run_render_job, args=(job_id, name, opts), daemon=True,
    )
    with _RENDER_LOCK:
        _RENDER_THREADS[job_id] = thread
    thread.start()
    return job_id


def _run_render_job(job_id: str, name: str, opts: dict) -> None:
    """Background worker: marks state running, captures stage events via
    print() interception, runs render_map(), updates state on completion.

    Caller must NOT block on this — it's the body of a background thread.
    """
    import time
    t0 = time.perf_counter()
    with _RENDER_LOCK:
        job = _RENDER_JOBS.get(job_id)
        if job is None:
            return
        job["state"] = "running"
        job["started_at"] = time.time()

    # Patch builtins.print for the duration of this render so stage logs
    # become structured events. We restore on the same thread so other
    # concurrent renders see the original print (note: this is per-thread
    # only because Python's print is process-wide, so concurrent renders
    # would interleave their stage events — acceptable for a dev tool).
    import builtins
    real_print = builtins.print

    def _intercepting_print(*args, **kwargs):
        msg = " ".join(str(a) for a in args)
        for marker, stage, kind in _PIPELINE_STAGE_MARKERS:
            if marker in msg:
                with _RENDER_LOCK:
                    j = _RENDER_JOBS.get(job_id)
                    if j is not None:
                        j["stages"].append({
                            "stage": stage, "type": kind,
                            # Elapsed seconds since job start. Use the
                            # SAME clock as t0 (perf_counter) to keep
                            # numbers monotonic and meaningful.
                            "ts": round(time.perf_counter() - t0, 3),
                        })
                break
        # Always pass through to the real print so server logs still work
        return real_print(*args, **kwargs)

    builtins.print = _intercepting_print
    try:
        from src.pipeline import render_map
        cfg_path = (CONFIG_DIR / f"{name}.yaml").resolve()
        out_path = render_map(
            config_path=cfg_path,
            scale_factor=int(opts.get("scale", 1)),
            output_format=str(opts.get("format", "png")).lower(),
            jpeg_quality=int(opts.get("quality", 88)),
            reference_mode=bool(opts.get("reference_mode", opts.get("reference", False))),
            dpi=opts.get("dpi"),
            embed_metadata=bool(opts.get("embed_metadata", False)),
        )
        ms_total = int((time.perf_counter() - t0) * 1000)
        # Generate the small editor-preview JPEG just like /api/render does.
        # Fall back to the cached final stage when the output format isn't
        # Pillow-readable (PDF/PS without Ghostscript).
        preview_path = PREVIEW_DIR / f"{name}.jpg"
        thumb_source = out_path
        if out_path.suffix.lower() in (".pdf", ".ps", ".eps"):
            cached_final = ROOT / "cache" / "stages" / name / "final.png"
            if cached_final.exists():
                thumb_source = cached_final
        with Image.open(thumb_source) as im:
            im.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
            im.convert("RGB").save(preview_path, quality=82, optimize=True)
        out_ext = out_path.suffix.lstrip(".")
        with _RENDER_LOCK:
            j = _RENDER_JOBS.get(job_id)
            if j is not None:
                j["state"] = "done"
                j["completed_at"] = time.time()
                j["result"] = {
                    "url": f"/preview/{name}.jpg?v={uuid.uuid4().hex[:10]}",
                    "render_ms": ms_total,
                    "output_path": str(out_path.relative_to(ROOT)),
                    # Use the on-disk extension so the link works for
                    # tiff/pdf/webp where format alias != extension.
                    "output_url": f"/api/output/{name}.{out_ext}",
                    "format": str(opts.get("format", "png")).lower(),
                    "extension": out_ext,
                }
    except Exception as e:
        with _RENDER_LOCK:
            j = _RENDER_JOBS.get(job_id)
            if j is not None:
                j["state"] = "error"
                j["completed_at"] = time.time()
                j["error"] = f"{type(e).__name__}: {e}"
    finally:
        builtins.print = real_print


def _gc_old_jobs() -> None:
    """Drop completed jobs older than 1 hour to bound memory."""
    import time
    cutoff = time.time() - 3600
    with _RENDER_LOCK:
        stale = [jid for jid, j in _RENDER_JOBS.items()
                  if j.get("completed_at") and j["completed_at"] < cutoff]
        for jid in stale:
            _RENDER_JOBS.pop(jid, None)
            _RENDER_THREADS.pop(jid, None)


def _path_length_km_jsonable(path) -> float:
    """Pixel-distance-free path length used by API responses (great-circle km)."""
    if not path or len(path) < 2:
        return 0.0
    import math
    R = 6371.0
    total = 0.0
    for (la1, lo1), (la2, lo2) in zip(path, path[1:]):
        p1 = math.radians(la1); p2 = math.radians(la2)
        dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
        a = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        total += 2 * R * math.asin(math.sqrt(a))
    return round(total, 1)


def _config_path(name: str, must_exist: bool = True) -> Path:
    """Resolve a config name to its YAML path; refuse paths that escape config/."""
    if "/" in name or ".." in name or name.startswith("."):
        abort(400, "invalid config name")
    path = (CONFIG_DIR / f"{name}.yaml").resolve()
    if not str(path).startswith(str(CONFIG_DIR.resolve())):
        abort(400, "invalid config name")
    if must_exist and not path.exists():
        abort(404, f"config {name!r} not found")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Cartograph local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5000, type=int)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app = create_app()
    print(f"\n  Cartograph → http://{args.host}:{args.port}/\n")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
