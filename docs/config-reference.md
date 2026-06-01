# Config reference

Cartograph maps are defined entirely by YAML. Every render is a function of the config — the pipeline reads `config/<name>.yaml`, resolves named colors and fonts, and produces an image.

This document is the schema reference. Start with [`config/_template.yaml`](../config/_template.yaml) for a runnable starting point or [`config/dominus-columbia.yaml`](../config/dominus-columbia.yaml) for a fully-featured example.

## Top-level structure

```yaml
name: ... # required
subtitle: ... # optional
credit: ... # optional

bounds: # required — the geographic rectangle
terrain: # optional — terrain rendering knobs (sensible defaults)
roads: # optional — OSM road networks + styling
urbanization: # optional — grey wash over OSM built-up areas

settlements: # optional — point markers
edge_indicators: # optional — edge arrows
infrastructure: # optional — secondary point markers
water_labels: # optional — italicized water-body labels
regions: # optional — named territories with tints + labels

barriers: # optional — fortification lines (A* routed)
buffer_zone: # optional — hatched no-go corridor between barriers
contamination: # optional — multi-source spread visualization
sinking: # optional — force a region underwater

canvas: # optional — paper, border, frame lines
legend: # optional — legend entries
state_boundaries: # optional — US state-border overlay
reference_cities: # optional — real-world city dots (sanity-check overlay)
decoration: # optional — compass, scale bar, credit, ornaments, cartouche

colors: # optional — named-color palette
fonts: # optional — per-role font overrides
```

Anything marked optional has a sensible default in the renderer; configs without those keys still produce valid maps.

---

## Identity (top-level strings)

```yaml
name: "DOMINUS COLUMBIA"
subtitle: "Survey of the Bailey & the Omni Containment Zone"
credit: "Fan map by Leighton Carden for the Dystopia Rising Universe"
```

| Key        | Type   | Notes                                               |
| ---------- | ------ | --------------------------------------------------- |
| `name`     | string | Drawn in the title cartouche at the top of the map. |
| `subtitle` | string | Optional, drawn under the name.                     |
| `credit`   | string | Optional, drawn in the credit line at the bottom.   |

## `bounds`

```yaml
bounds:
  lat_n: 39.45 # northern extent (degrees, signed)
  lat_s: 38.20 # southern extent
  lon_w: -77.27 # western extent
  lon_e: -76.33 # eastern extent
  aspect_ratio: "3:4" # optional canvas ratio enforcement
```

| Key                                | Type   | Notes                                                                                                                                    |
| ---------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `lat_n`, `lat_s`, `lon_w`, `lon_e` | float  | Geographic rectangle. Signed (negative = S/W).                                                                                           |
| `aspect_ratio`                     | string | Forces canvas dimensions. `"free"` or absent = derive from the geographic rectangle. Common values: `"1:1"`, `"4:3"`, `"3:4"`, `"16:9"`. |

Bounds changes invalidate every render-cache stage; expect a full re-render (~60 s on cold cache).

## `terrain`

The terrain block drives everything visual about the underlying map _before_ settlements and labels.

```yaml
terrain:
  sea_level_rise: 15 # meters
  downsample: 4 # pixel-density divisor

  palette:
    low: [0.82, 0.76, 0.55] # 0–1 RGB stops
    mid: [0.65, 0.60, 0.45]
    high: [0.47, 0.50, 0.55]
    mid_break: 0.5 # 0–1 — where mid sits in the elevation range

  water:
    shallow: [0.45, 0.50, 0.55]
    deep: [0.22, 0.30, 0.40]
    depth_range: 50

  shade:
    sun_altitude: 35 # 5–85 degrees
    sun_azimuth: 315 # 0–359 degrees (compass: 0=N, 90=E, 180=S, 270=W)
    floor: 0.40 # 0–0.9 ambient minimum

  noise:
    drift_sigma: 35 # gaussian sigma in pixels
    drift_strength: 0.07 # 0–0.5
    scorch_sigma: 18
    scorch_strength: 0.05

  vignette:
    strength: 0.35 # 0–1
    floor: 0.55 # 0–1

  coastline_darken:
    dilation: 1 # pixel dilation around shoreline
    factor: 0.78 # 0.3–1 multiplier on the land side

  wasteland_wash:
    desaturate: 0.10 # 0–1
    warm_push_r: 0.012 # -0.05 to +0.05
    cool_push_b: -0.008
```

| Key                                  | Type                        | Notes                                                                                                                                            |
| ------------------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `sea_level_rise`                     | int                         | Meters above zero. Floods land below this elevation.                                                                                             |
| `downsample`                         | int                         | DEM pixel-density divisor. 1× = full res (slow), 4× = working speed, 8× = quick preview. The render-time `--scale` flag multiplies this back up. |
| `palette.{low,mid,high}`             | [r, g, b] (0–1)             | Three RGB stops driving the elevation gradient.                                                                                                  |
| `palette.mid_break`                  | float (0–1)                 | Where the `mid` stop sits along the elevation range.                                                                                             |
| `water.{shallow,deep}`               | [r, g, b] (0–1)             | Two RGB stops for water surface coloring.                                                                                                        |
| `water.depth_range`                  | int                         | Meters of bathymetry over which shallow→deep transitions.                                                                                        |
| `shade.sun_altitude`                 | int (5–85)                  | Sun height for hillshade.                                                                                                                        |
| `shade.sun_azimuth`                  | int (0–359)                 | Sun direction (compass degrees).                                                                                                                 |
| `shade.floor`                        | float (0–0.9)               | Minimum ambient brightness in shadow.                                                                                                            |
| `noise.drift_*`                      | sigma in px, strength 0–0.5 | Smooth color drift.                                                                                                                              |
| `noise.scorch_*`                     | same                        | Sharper burn-patch noise.                                                                                                                        |
| `vignette.{strength,floor}`          | floats (0–1)                | Corner darkening.                                                                                                                                |
| `coastline_darken.{dilation,factor}` | int, float                  | Darkens land side of coastlines for emphasis.                                                                                                    |
| `wasteland_wash.*`                   | float                       | Final color grade (desaturate + warm/cool push).                                                                                                 |

## `roads`

```yaml
roads:
  networks: ["US:I", "US:US", "US:MD"]
  shadow_color: [45, 38, 28]
  mask_by_water: true
  styles:
    interstate:
      network: "US:I"
      color: [185, 165, 110]
      width: 3
      shadow_width: 2
    us_highway:
      network: "US:US"
      color: [160, 145, 115]
      width: 2
      shadow_width: 1
```

| Key                                          | Type                              | Notes                                                                                                                       |
| -------------------------------------------- | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `networks[]`                                 | list of OSM `network=` tag values | E.g. `US:I` (Interstates), `US:US` (US Highways), `US:NJ` (New Jersey state routes). Each value triggers an Overpass query. |
| `shadow_color`                               | [r, g, b]                         | Single shared shadow color under all roads.                                                                                 |
| `mask_by_water`                              | bool                              | When true, roads stop at the post-flood shoreline.                                                                          |
| `styles.{name}.network`                      | string                            | Which `networks[]` value this style draws.                                                                                  |
| `styles.{name}.{color, width, shadow_width}` | [r,g,b], int, int                 | Per-style line rendering.                                                                                                   |

Multiple styles can reference the same network (e.g. a "highlighted" style overlaid on a base style).

## `urbanization`

```yaml
urbanization:
  enabled: true
  blend_strength: 0.55 # 0–1 — how much of the wash shows
  color: [0.52, 0.50, 0.48] # 0–1 RGB
```

Grey wash blended over OSM-detected built-up areas (`landuse=residential|industrial|commercial|retail`).

## `settlements`

```yaml
settlements:
  - name: "Bahlsmore"
    lat: 39.29
    lon: -76.62
    color: bright # named or [r,g,b]
    radius: 16 # marker size in px (0 = text only)
    font: major # references cfg.fonts.<role>
    marker: circled_x # circle (default) | circled_x | custom
    label_side: right # auto (default) | left | right | above | below
    label_offset: [14, 0] # optional manual offset in px
    rotation: 0 # degrees CCW (rotated labels skip leader lines)
    note: "Optional sub-label"
```

| Key            | Type              | Notes                                                                                                             |
| -------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| `lat`, `lon`   | float             | Coordinates in degrees.                                                                                           |
| `color`        | string \| [r,g,b] | Either a key into `cfg.colors` or an explicit RGB.                                                                |
| `radius`       | int               | Marker size. 0 = text-only (no marker drawn). Drives the tier (16+ major, 12+ important, 10+ standard, 8+ minor). |
| `font`         | string            | Optional — overrides the tier-derived font role. References `cfg.fonts.<role>`.                                   |
| `marker`       | string            | `circle` (default), `circled_x`, or any custom string the renderer recognizes.                                    |
| `label_side`   | string            | Where the label sits relative to the marker. `auto` lets the placer choose.                                       |
| `label_offset` | [dx, dy]          | Manual nudge in pixels. Settable via Shift+drag in the editor.                                                    |
| `rotation`     | int               | Label rotation in degrees CCW.                                                                                    |
| `note`         | string            | Optional secondary line under the main label.                                                                     |

## `edge_indicators`

Edge arrows pointing offstage.

```yaml
edge_indicators:
  - text: "← THREE MILE FIRE"
    lat: 40.15 # latitude along the chosen edge
    edge: west # west | east | north | south | center
    color: red # named or [r,g,b]
    note: "Still burning"
```

## `infrastructure`

Lower-priority point markers (bridges, gates, choke-points). Same shape as `settlements` but rendered as small text + optional note.

```yaml
infrastructure:
  - name: "Pale Horse Pike"
    lat: 39.55
    lon: -74.72
    note: "Sole road to Aysea"
```

## `water_labels`

Italicized labels for bays, rivers, oceans.

```yaml
water_labels:
  - name: "Patomic Bay"
    lat: 38.82
    lon: -77.22
    rotation: 0 # optional — degrees CCW
```

## `regions`

Named territories with tints + optional labels.

```yaml
regions:
  - name: "The Red Embankment"
    type: region_label # region_label | tint_only
    subtitle: "Optional"
    color: bright # named or [r,g,b]
    center: [39.22, -76.72] # label position (region_label only)
    rotation: 0 # label rotation in degrees CCW

    tint:
      type: radial # radial (default) | directional
      center: [39.25, -76.72] # radial: tint center; directional: source anchor
      inner_radius: 0.06 # radial only — degrees
      outer_radius: 0.25 # radial only — degrees
      direction: south # directional only — north|south|east|west|northeast|...
      range: 0.5 # directional only — degrees
      adjustments: { r: 0.12, g: -0.05, b: -0.06 } # -1 to +1 per channel
      strength: 1.0 # 0–1 — how much of the adjustment shows
      desaturate: 0.0 # 0–1
```

| Key                | Notes                                                                                                                                                                |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`             | `region_label` draws both the tint and a label; `tint_only` skips the label.                                                                                         |
| `tint.type`        | `radial` falls off from `center` between `inner_radius` and `outer_radius`. `directional` projects from a source anchor outward in `direction` over `range` degrees. |
| `tint.adjustments` | Per-channel color shift in normalized space (-1 to +1).                                                                                                              |
| `tint.strength`    | Multiplier on the adjustments.                                                                                                                                       |
| `tint.desaturate`  | 0–1 desaturation within the tinted area.                                                                                                                             |

## `barriers`

A\*-routed fortification lines. One YAML entry per barrier.

```yaml
barriers:
  - name: "The Wall"
    type: wall # arbitrary string for grouping
    method: astar_road_network # astar_road_network | astar_offset
    road_types: [motorway, trunk] # OSM highway tags to follow

    endpoints:
      west:
        lat: 39.05
        lon: -77.20
        note: "Western water boundary"
      east:
        lat: 39.22
        lon: -76.58
        note: "Patapsco inlet"

    corridor: # optional A* search-bounds
      lat_min: 38.92
      lat_max: 39.30
      penalty: 500000 # cost to leave the corridor

    style:
      color: [200, 60, 50]
      width: 6
      shadow_width: 9
      hash_marks: true
      hash_length: 12
      hash_spacing: 14

    label:
      text: "THE WALL"
      position: midpoint # midpoint | start | end
      offset: [-30, -10] # [dx, dy] in px
      color: [200, 60, 50]
      font: bold_13 # references cfg.fonts.<role>
```

| Key                | Notes                                                                                                                                                            |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `method`           | `astar_road_network` follows real OSM roads matching `road_types`. `astar_offset` parallels another barrier (specify `parallel_to: <name>` and `offset_meters`). |
| `corridor`         | Optional bounding box that caps A\* search. `penalty` is the cost added per pixel outside the corridor.                                                          |
| `style.hash_marks` | When true, draws perpendicular hash marks along the line. `hash_length` (px) and `hash_spacing` (px) tune them.                                                  |

## `buffer_zone`

Hatched no-go corridor between two named barriers.

```yaml
buffer_zone:
  enabled: true
  between: ["The Wall", "The Patrol Line"]
  hatching:
    color: [200, 60, 50]
    direction: diagonal # diagonal | horizontal | vertical
    spacing: 14 # px between hash strokes
  symbols: # optional fill symbols
    character: "☣"
    font: DejaVuSans
    font_size: 22
    color: [190, 50, 40]
    count: 12
    placement: midline # midline | random
```

## `contamination`

Multi-source spread visualization (e.g. plague spreading from infected sites).

```yaml
contamination:
  enabled: true
  name: "Omni Plague"
  method: multi_source_dijkstra

  sources:
    type: osm_query # osm_query | manual
    query: 'node["railway"="station"]["network"="WMATA"]'
    weighting: distance_from_epicenter # uniform | distance_from_epicenter
    weight_scale: 100
    epicenter:
      lat: 38.90
      lon: -77.02

  spread:
    blocked_by: [water, "The Wall"] # water + barrier names
    max_distance: 1200 # max spread in pixels

  overlay:
    color: [0.05, 0.50, 0.20] # 0–1 RGB
    max_opacity: 0.55
    noise_variation: 0.06

  edge_noise:
    seed: 666
    octaves:
      - { scale: 2, weight: 0.3 }
      - { scale: 8, weight: 0.5 }
      - { scale: 32, weight: 0.2 }
```

| Key                    | Notes                                                                                                   |
| ---------------------- | ------------------------------------------------------------------------------------------------------- |
| `sources.type`         | `osm_query` runs an Overpass query; `manual` reads from a `sources: [...]` list.                        |
| `sources.weighting`    | `uniform` weights all sources equally; `distance_from_epicenter` gives closer sources higher weight.    |
| `spread.blocked_by`    | Spread stops at any of these. `water` is special-cased; barrier names match by string.                  |
| `edge_noise.octaves[]` | Each octave: `scale` (gaussian sigma in px), `weight` (0–1 contribution). Sum of weights typically ≈ 1. |

## `sinking`

Force a region to be underwater regardless of elevation.

```yaml
sinking:
  enabled: true
  method: nan_mask
  region: Virginia
  source: "virginia_border.json" # path under cache/boundaries/
```

Only `nan_mask` is supported today — overlays a polygon mask from a GeoJSON source file.

## `canvas`

```yaml
canvas:
  border: 80 # outer margin in px
  paper_color: [210, 190, 150] # RGB
  border_lines: # nested frame lines (drawn inside the border)
    - offset: 8 # px from canvas edge
      color: [50, 40, 30]
      width: 4
    - offset: 30
      color: [80, 65, 45]
      width: 1
```

## `legend`

```yaml
legend:
  position: bottom-left # bottom-left | top-left | top-right | bottom-right
  entries:
    - { type: circle, color: bright, label: "Major settlement" }
    - { type: circle, color: red, label: "Hostile settlement" }
    - { type: line, color: gold, label: "Interstate" }
    - { type: italic_text, color: cyan, label: "Body of water" }
```

| `type`        | Renders as                                    |
| ------------- | --------------------------------------------- |
| `circle`      | Filled circle marker                          |
| `line`        | Short horizontal line                         |
| `italic_text` | Italic placeholder text in the legend's color |

`position` (default `bottom-left`) places the legend plaque in one of the four
corners. Top placements automatically drop below the title cartouche when one is
present, so a wide subtitle can't overlap a corner legend. The default keeps the
exact historical anchor, so legacy configs render unchanged.

## `state_boundaries`

State- or county-border overlay (uses OSM administrative boundaries).

```yaml
state_boundaries:
  enabled: true
  states: ["New Jersey", "Pennsylvania", "Delaware"]
  highlight: "New Jersey" # one entry drawn in highlight_color
  highlight_color: [255, 210, 70, 200] # RGBA
  other_color: [170, 170, 190, 70] # RGBA — draws the rest of `states`
  county_of: null # see below — set to a state name to draw counties
```

Despite the name, this block draws **any** named administrative boundary, not
just states — the renderer looks each name up in the boundary cache and draws
its polygon. Set `county_of` to a state name to draw **counties** (admin_level 6) instead of states: missing entries are then fetched as counties within that
state, which disambiguates repeated county names (e.g. "Sussex County" exists in
NJ, DE, and VA). Example for a county-focused map:

```yaml
state_boundaries:
  enabled: true
  county_of: New Jersey
  states: ["Sussex County"]
  highlight: "Sussex County"
  highlight_color: [225, 185, 95, 235]
```

Boundaries auto-fetch on first render (like SRTM/OSM data) and cache to
`cache/boundaries/state_boundaries.json`, so a map reproduces on a fresh clone
without manual cache population.

## `reference_cities`

Real-world city dots for sanity-checking your map's geography.

```yaml
reference_cities:
  enabled: true
  cities:
    - { name: "Washington DC", lat: 38.90, lon: -77.04 }
    - { name: "Baltimore", lat: 39.29, lon: -76.62 }
```

## `decoration`

Cartographic furniture. Each subblock has an `enabled` flag; defaults match the prior pre-decoration-block rendering.

```yaml
decoration:
  compass:
    enabled: true
    position: bottom-right # bottom-right | bottom-left | top-right | top-left
    radius_pct: 0 # 0 = auto (1/22 of min(canvas_w, canvas_h))
    offset_x: 20 # px from inner border
    offset_y: 80

  scale_bar:
    enabled: true
    position: bottom-center # bottom-center | bottom-left | bottom-right
    bar_miles: 20 # how many miles the bar represents
    segments: 4 # alternating dark/light segments
    offset_from_border: 60 # px from inner border line

  credit:
    enabled: true
    divider: true # divider line above the credit text
    offset_from_border: 28 # px above the inner border (keeps text inside frame)

  ornaments:
    enabled: true
    glyph: "⚜" # any unicode character
    size: 32 # base font size in px
    color: [85, 65, 45] # RGB; shadow auto-darkens 55%
    inset_x: 35 # px from each canvas corner
    inset_top: 30
    inset_bottom: 45 # separate top/bottom for cartouche clearance

  cartouche:
    enabled: true # title cartouche at top center
```

## `colors`

Named-color palette referenced everywhere by name (`color: gold` in a settlement, etc.).

```yaml
colors:
  bright: [235, 220, 185]
  cyan: [170, 185, 195]
  gold: [235, 220, 185]
  green: [130, 200, 110]
  parch: [200, 190, 170]
  red: [200, 130, 90]
  shadow: [30, 25, 18]
  submerged: [190, 215, 230]
  water: [200, 210, 215]
  white: [235, 225, 200]
```

Add or remove names as you want. Settlements / regions / legend / edge indicators / barrier labels can all reference any name in this palette.

## `fonts`

Per-role font definitions. The pipeline ships with sensible defaults for built-in roles; this block overrides them.

```yaml
fonts:
  major: { file: "DejaVuSerif-Bold.ttf", size: 18 }
  minor: { file: "DejaVuSerif.ttf", size: 11 }
  region: { file: "DejaVuSerif-Italic.ttf", size: 38 }
  settle: { file: "DejaVuSerif.ttf", size: 13 }
  bold_13: { file: "DejaVuSerif-Bold.ttf", size: 13 } # custom role for barrier labels
```

| Built-in role                             | Default                    | Used by                                                             |
| ----------------------------------------- | -------------------------- | ------------------------------------------------------------------- |
| `major`, `minor`, `settle`                | DejaVuSans-Bold/DejaVuSans | Settlement labels by tier                                           |
| `region`                                  | DejaVuSerif-Italic 32      | Region labels                                                       |
| `note`                                    | DejaVuSans 20              | Settlement notes + edge-indicator notes                             |
| `water`                                   | DejaVuSerif-Italic 26      | Water labels                                                        |
| `credit`                                  | DejaVuSerif-Italic 14      | Credit line + edge-indicator note fallback                          |
| `legend_title`, `legend`, `legend_italic` | various                    | Legend entries                                                      |
| `compass`, `scale`                        | various                    | Compass cardinal labels + scale-bar end labels                      |
| `fleur`                                   | DejaVuSans 32              | Corner ornaments (override via `decoration.ornaments.size` instead) |

Custom roles (`bold_13`, `italic_16`, etc.) are referenced from places that need them (`barriers[].label.font: bold_13`). Each custom role must specify both `file` and `size` — the loader skips incomplete entries.

Font files are looked up relative to the directory in `pipeline.DEFAULT_FONT_DIR` (Linux: `/usr/share/fonts/truetype/dejavu/`). On macOS or Windows, override `DEFAULT_FONT_DIR` or specify absolute paths in `fonts.<role>.file`.

---

## Caching keys

Editing a config triggers a different cost depending on which keys changed:

| Changed key                                                                                                                                                                                                                    | Cache invalidation                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------- |
| `bounds.*`, `terrain.downsample`, `sinking.*`                                                                                                                                                                                  | Stage 1 (terrain DEM) — rebuilds everything |
| `terrain.*` (other), `urbanization.*`, `regions[].tint.*`                                                                                                                                                                      | Stage 2 (terrain styled)                    |
| `roads.*`, `canvas.border`, `canvas.paper_color`                                                                                                                                                                               | Stage 3 (canvas with roads)                 |
| `settlements`, `edge_indicators`, `infrastructure`, `water_labels`, `regions[].name`, `barriers`, `buffer_zone`, `contamination`, `legend`, `decoration`, `state_boundaries`, `reference_cities`, `name`, `subtitle`, `credit` | Stage 4 (final) only                        |

This is what makes the editor responsive: moving a settlement skips stages 1–3 entirely.
