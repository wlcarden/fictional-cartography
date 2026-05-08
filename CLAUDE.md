# Fictional Cartography

## Project Overview
A tool for creating stylized post-apocalyptic parchment maps from real-world terrain data with fictional overlays. Maps are defined entirely by YAML config files — the rendering pipeline is map-agnostic.

## Architecture

### Config-Driven Pipeline
Each map is a YAML file in `config/`. The pipeline reads the config and produces a rendered PNG:
```
config/jersea-wastes.yaml → pipeline → output/jersea-wastes.png
```

### Data Flow
1. **SRTM tiles** → mosaic → crop to bounds → downsample → DEM array
2. **Overpass API** → roads, land use → cached JSON → road segments, urban density raster
3. DEM + sea level → **water mask** (what's flooded)
4. DEM + water mask → **hillshade + base colors** (elevation gradient)
5. Urban density → **urbanization tint** (grey over built-up areas)
6. Region configs → **regional tints** (color shifts per named zone)
7. Terrain noise + paper texture + vignette → **final terrain image**
8. Roads drawn on canvas, **masked by water**
9. **Collision-aware labels** placed in priority order
10. **Cartographic furniture** (compass, scale bar, legend, title, ornaments)

### Key Modules
- `src/terrain/` — SRTM download, mosaic, hillshade, sea level modification
- `src/data/` — Overpass API queries with caching, boundary fetching, density rasterization
- `src/styling/` — Regional tints, paper texture, road rendering
- `src/labels/` — LabelPlacer collision engine, marker/region/water renderers
- `src/furniture/` — Compass rose, scale bar, legend, title cartouche, corner ornaments
- `src/pipeline.py` — Orchestrator that reads YAML and calls all modules

### Config Schema
See `config/jersea-wastes.yaml` for a complete example. Key sections:
- `bounds` — geographic extent and aspect ratio
- `terrain` — sea level, color palette, noise, shade parameters
- `settlements` — named locations with lat/lon, color, radius, label side, notes
- `regions` — named territories with tints (radial color shifts, desaturation)
- `roads` — which OSM road networks to fetch and draw
- `legend` / `canvas` / `water_labels` — cartographic furniture

### Label Placement
Labels use a score-based collision system:
- Settlements placed in tier order (major first, they get best positions)
- Each label tries 48 candidate positions (8 angles × 6 distances spiraling from anchor)
- Score = overlap_penalty × 50 + distance × 1.0
- Hard reject: anything outside map terrain area (no border bleed)
- `label_side` in config sets preferred angle (right=0°, left=180°, above=270°, below=90°)

### Caching
- SRTM `.hgt` files cached in `cache/srtm/`
- Overpass responses cached in `cache/overpass/`
- State boundary JSON cached in `cache/boundaries/`
- Urban density `.npy` cached in `cache/density/`
- Water mask `.npy` cached in `cache/water/`

Cache is keyed by map bounds + parameters. Re-running the same config reuses cached data.

### Color System
Named colors defined in config `colors:` section. Settlement `color:` field references these names. Custom RGB can also be specified as `[r, g, b]` arrays.

## Development Notes

### Dependencies
- Python 3.10+
- numpy, scipy, Pillow (core rendering)
- PyYAML (config loading)
- Flask (web UI) — optional, not needed for CLI

### Running
```bash
# CLI: render a map
python -m src.pipeline render config/jersea-wastes.yaml --output output/jersea-wastes.png

# CLI: render at high resolution (scale factor)
python -m src.pipeline render config/jersea-wastes.yaml --scale 4

# CLI: export Discord-ready JPEG
python -m src.pipeline render config/jersea-wastes.yaml --format jpg --quality 88

# Web UI
python -m src.ui
```

### Adding a New Map
1. Copy `config/_template.yaml` 
2. Set bounds, sea level, settlements, regions
3. Run `python -m src.pipeline render config/your-map.yaml`
4. Iterate on the YAML, re-render

### File Size Targets
- Working resolution (downsample=4): ~2000×2700px, ~12MB PNG, ~2MB JPEG
- Full resolution (downsample=1): ~8000×10800px, ~150MB PNG
- Discord-ready: JPEG quality 88, typically 2-3MB
