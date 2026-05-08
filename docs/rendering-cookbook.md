# Rendering Cookbook

Proven code patterns for each rendering phase. All operate on float32 numpy arrays (0.0–1.0 per channel) until final PIL conversion.

## Hillshade

```python
def hillshade(dem, sea_level, sun_altitude=35, sun_azimuth=315):
    d = dem.copy()
    d[np.isnan(d)] = 0
    d[dem <= sea_level] = sea_level - 5  # flatten water before gradient
    dy, dx = np.gradient(d)
    slope = np.arctan(np.sqrt(dx**2 + dy**2))
    aspect = np.arctan2(-dy, dx)
    alt_r = np.radians(sun_altitude)
    az_r = np.radians(sun_azimuth)
    return np.clip(
        np.sin(alt_r) * np.cos(slope) +
        np.cos(alt_r) * np.sin(slope) * np.cos(az_r - aspect),
        0, 1
    )
```

**Why flatten water:** Without this, coastlines produce extreme slope artifacts that look like cliff edges.

## Base Colors

```python
shade = hillshade(dem, level)
elev_t = np.clip((dem - level) / (max_elev - level), 0, 1)
water_mask = (dem <= level) | np.isnan(dem)
land_mask = ~water_mask

# Land: warm olive-brown, darkening with elevation, modulated by shade
# The 0.4 floor prevents full-black shadows
land_r = np.clip(0.82 - elev_t * 0.35, 0, 1) * (0.4 + 0.6 * shade)
land_g = np.clip(0.76 - elev_t * 0.37, 0, 1) * (0.4 + 0.6 * shade)
land_b = np.clip(0.55 - elev_t * 0.28, 0, 1) * (0.4 + 0.6 * shade)

# Water: cool grey-blue darkening with depth
depth_t = np.clip((level - dem) / 40, 0, 1)
depth_t[np.isnan(dem)] = 0.7
water_r = 0.62 - depth_t * 0.20
water_g = 0.63 - depth_t * 0.18
water_b = 0.60 - depth_t * 0.12
```

Adjust base values for different palettes: warmer for desert, cooler/greener for temperate, blue-white for arctic.

## Terrain Tinting

### Radial mask (for named regions)
```python
def radial_mask(center_lat, center_lon, inner_r, outer_r, lats_2d, lons_2d):
    dlat = lats_2d - center_lat
    dlon = (lons_2d - center_lon) * np.cos(np.radians(center_lat))
    dist = np.sqrt(dlat**2 + dlon**2)
    return np.clip(1 - (dist - inner_r) / (outer_r - inner_r), 0, 1)
```

### Directional mask (for zones relative to a line)
```python
# Using an interpolated barrier line:
from scipy.interpolate import interp1d
barrier_interp = interp1d(barrier_lons, barrier_lats, fill_value='extrapolate')
barrier_lat_arr = barrier_interp(lons_2d)

# North of barrier:
north_mask = np.clip((lats_2d - barrier_lat_arr) / fade_degrees, 0, 1)
# South of barrier:
south_mask = np.clip((barrier_lat_arr - lats_2d) / fade_degrees, 0, 1)
```

### Applying tints
```python
mask = radial_mask(39.25, -76.90, 0.06, 0.25, lats_2d, lons_2d)
mask *= land_mask  # always restrict to land
mask = gaussian_filter(mask.astype(np.float32), sigma=15)  # feather edges

# Iron/rust zone: push red, pull green/blue
land_r += mask * 0.12
land_g -= mask * 0.05
land_b -= mask * 0.06

# Desaturation (blast zones, wastelands):
grey = 0.3 * land_r + 0.59 * land_g + 0.11 * land_b  # luminance
blend = 0.8  # 80% desaturated
land_r = land_r * (1 - mask * blend) + grey * (mask * blend)
land_g = land_g * (1 - mask * blend) + grey * (mask * blend)
land_b = land_b * (1 - mask * blend) + grey * (mask * blend)
```

### Common tint recipes
| Zone type | R adjustment | G adjustment | B adjustment |
|-----------|-------------|-------------|-------------|
| Iron/rust | +0.08 to +0.12 | -0.03 to -0.05 | -0.04 to -0.06 |
| Dark hills | -0.06 | -0.05 | -0.02 (keep blue for cool) |
| Swamp | -0.06 to -0.11 | +0.02 to +0.04 | -0.05 to -0.09 |
| Sandy beach | +0.05 to +0.09 | +0.04 to +0.07 | +0.01 to +0.02 |
| Warm civilized | +0.03 to +0.05 | +0.02 to +0.04 | -0.02 to -0.04 |
| Urban (via density) | blend 55% toward (0.52, 0.50, 0.48) |
| Wasteland (desaturate) | blend 25-40% toward luminance |
| Blast zone (heavy desat) | blend 60-80% toward luminance, slight yellow cast |

## Contamination Spread (Multi-Source Dijkstra)

```python
import heapq

def spread_from_sources(sources, dem, sea_level, barrier_interp, max_dist=1200):
    """
    sources: [(lat, lon, weight), ...] — weight scales starting distance
    Returns: distance field (0 = source, higher = further)
    """
    h, w = dem.shape
    dist = np.full((h, w), np.inf)
    pq = []
    
    for lat, lon, weight in sources:
        r = int((lat_n - lat) / (lat_n - lat_s) * h)
        c = int((lon - lon_w) / (lon_e - lon_w) * w)
        if 0 <= r < h and 0 <= c < w:
            start_dist = weight * 100  # further from epicenter = delayed start
            dist[r, c] = start_dist
            heapq.heappush(pq, (start_dist, r, c))
    
    water = (dem <= sea_level) | np.isnan(dem)
    
    while pq:
        d, r, c = heapq.heappop(pq)
        if d > dist[r, c]:
            continue
        if d > max_dist:
            continue
        
        for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
            nr, nc = r+dr, c+dc
            if not (0 <= nr < h and 0 <= nc < w):
                continue
            if water[nr, nc]:
                continue
            # Block at barrier line
            lat_here = lat_n - nr / h * (lat_n - lat_s)
            lon_here = lon_w + nc / w * (lon_e - lon_w)
            barrier_lat = float(barrier_interp(lon_here))
            if lat_here > barrier_lat:  # north of barrier
                continue
            
            new_d = d + 1
            if new_d < dist[nr, nc]:
                dist[nr, nc] = new_d
                heapq.heappush(pq, (new_d, nr, nc))
    
    return dist
```

### Adding fractal edge noise
```python
np.random.seed(666)
noise = np.zeros((h, w), dtype=np.float32)
for scale, weight in [(2, 0.3), (5, 0.25), (11, 0.2), (23, 0.15), (47, 0.1)]:
    n = np.random.normal(0, 1, (h//scale+2, w//scale+2))
    noise += zoom(n, scale, order=1)[:h, :w] * weight
noise /= np.abs(noise).max() + 1e-8

# Threshold and apply
spread = np.clip(raw_dist / max_dist, 0, 1)
spread[raw_dist == np.inf] = 0
# Invert so 1 = epicenter, 0 = edge
plague = 1 - spread
plague[plague < 0.01] = 0
```

## Paper Texture & Vignette

```python
np.random.seed(42)  # FIXED SEED — critical for reproducible re-renders
image_255 = rgb_float * 255

# Fine grain
for c in range(3):
    image_255[:,:,c] += np.random.normal(0, 10, (h, w))

# Large-scale parchment variation
for c in range(3):
    image_255[:,:,c] += gaussian_filter(
        np.random.normal(0, 18, (h, w)).astype(np.float32), sigma=35
    )

# Vignette
vy, vx = np.mgrid[0:h, 0:w]
vd = np.sqrt(((vx - w/2) / (w * 0.6))**2 + ((vy - h/2) / (h * 0.6))**2)
for c in range(3):
    image_255[:,:,c] *= np.clip(1 - 0.35 * vd, 0.55, 1)

final = Image.fromarray(np.clip(image_255, 0, 255).astype(np.uint8), 'RGB')
```

## Parchment Canvas

```python
border = 80
canvas_w = terrain_w + border * 2
canvas_h = terrain_h + border * 2

canvas = Image.new("RGBA", (canvas_w, canvas_h), (210, 190, 150, 255))

# Add parchment noise to border area
np.random.seed(99)
pa = np.array(canvas).astype(np.float32)
pa[:,:,:3] += np.random.normal(0, 8, (canvas_h, canvas_w, 3))
pa[:,:,:3] += gaussian_filter(
    np.random.normal(0, 15, (canvas_h, canvas_w, 3)).astype(np.float32), sigma=20
)
canvas = Image.fromarray(np.clip(pa, 0, 255).astype(np.uint8), "RGBA")

# Paste terrain
canvas.paste(terrain_image.convert("RGBA"), (border, border))

# Triple border
D = ImageDraw.Draw(canvas)
D.rectangle([8, 8, canvas_w-9, canvas_h-9], outline=(50,40,30), width=3)
D.rectangle([15, 15, canvas_w-16, canvas_h-16], outline=(70,55,40), width=1)
D.rectangle([20, 20, canvas_w-21, canvas_h-21], outline=(90,72,50), width=2)
D.rectangle([border-4, border-4, border+terrain_w+3, border+terrain_h+3],
            outline=(60,48,35), width=2)
```

## A* Pathfinding for Barrier Lines

```python
import heapq

def pathfind_barrier(graph, nodes, start_node, end_node,
                     lat_min, lat_max, is_land_fn):
    """A* on road network with latitude band penalties."""
    
    def heuristic(nid):
        lat, lon = nodes[nid]
        elat, elon = nodes[end_node]
        return haversine(lat, lon, elat, elon)
    
    dist = {start_node: 0}
    prev = {}
    pq = [(heuristic(start_node), 0, start_node)]
    visited = set()
    
    while pq:
        _, d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        if u == end_node:
            break
        
        for v, edge_w in graph[u]:
            if v in visited:
                continue
            lat_v = nodes[v][0]
            
            # Latitude band penalties
            penalty = 0
            if lat_v > lat_max:
                penalty = (lat_v - lat_max) * 500000
            elif lat_v < lat_min:
                penalty = (lat_min - lat_v) * 500000
            
            new_d = d + edge_w + penalty
            if v not in dist or new_d < dist[v]:
                dist[v] = new_d
                prev[v] = u
                heapq.heappush(pq, (new_d + heuristic(v), new_d, v))
    
    # Reconstruct path
    path = []
    current = end_node
    while current in prev:
        path.append(nodes[current])
        current = prev[current]
    path.append(nodes[start_node])
    path.reverse()
    return path
```

## Scale Bar Calculation

```python
import numpy as np

km_per_deg_lon = 111.32 * np.cos(np.radians(mid_latitude))
total_km = (lon_e - lon_w) * km_per_deg_lon
total_miles = total_km * 0.621371
px_per_mile = terrain_w / total_miles

# Draw 10-mile bar with alternating segments
bar_px = int(10 * px_per_mile)
```

## Recommended Color Palette (Parchment Style)

```python
SHADOW          = (30, 25, 18)
TEXT_BRIGHT     = (235, 220, 185)
TEXT_WATER      = (200, 210, 215)
TEXT_SUBMERGED  = (170, 185, 195)
TEXT_DISTRICT   = (215, 200, 165)
WALL_COLOR      = (200, 150, 90)
PATROL_COLOR    = (180, 165, 135)
ROAD_MAJOR      = (185, 160, 115)
ROAD_SHADOW     = (45, 38, 28)
COMPASS_LINE    = (140, 115, 78)
PARCHMENT_BASE  = (210, 190, 150)
BORDER_DARK     = (50, 40, 30)
FLEUR_COLOR     = (85, 65, 45)
BIO_SYMBOL      = (190, 140, 50)
HATCH_RGBA      = (200, 150, 60, 120)
```
