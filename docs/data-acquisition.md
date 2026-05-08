# Data Acquisition Reference

## SRTM Elevation Tiles

### Finding tiles
Tile naming: `N{floor_lat}W{abs_floor_lon}.hgt` for the SW corner of each 1°×1° cell.
For a region spanning lat 38.2–39.5, lon -77.6 to -76.0, you need:
`N38W077.hgt`, `N38W078.hgt`, `N39W077.hgt`, `N39W078.hgt`

### Sources (free, no login for most)
- OpenTopography: https://opentopography.org
- USGS EarthExplorer: https://earthexplorer.usgs.gov (requires free account)
- Direct: `https://e4ftl01.cr.usgs.gov/MEASURES/SRTMGL1.003/2000.02.11/`

### Parsing

```python
import numpy as np

def load_srtm_tile(path):
    """Load a single SRTM .hgt tile."""
    data = np.fromfile(path, dtype=">i2").reshape(3601, 3601)
    data = data.astype(np.float32)
    data[data < -100] = np.nan  # voids
    return data

def mosaic_tiles(tiles_dict):
    """
    tiles_dict: {(lat, lon): ndarray} where (lat, lon) is SW corner
    Returns: (mosaic_array, lat_n, lat_s, lon_w, lon_e)
    """
    lats = sorted(set(k[0] for k in tiles_dict), reverse=True)
    lons = sorted(set(k[1] for k in tiles_dict))
    rows = [np.hstack([tiles_dict[(la, lo)] for lo in lons]) for la in lats]
    mosaic = np.vstack(rows)
    return mosaic, max(lats)+1, min(lats), min(lons), max(lons)+1
```

### Downsampling
```python
from scipy.ndimage import zoom
dem_work = zoom(dem_full, 0.25, order=3)  # 4× downsample, cubic
```

Keep `dem_full` for the final hi-res render. Use `dem_work` for all iterative development.

### Cropping to aspect ratio
```python
# For 3:4 portrait (height > width):
target_w = int(h * 3 / 4)  # width = 75% of height
excess = current_w - target_w
trim = excess // 2
dem_crop = dem_work[:, trim:trim+target_w]
```

Store the geographic bounds of the cropped array — every coordinate conversion depends on them.

---

## Overpass API Queries

Base URL: `https://overpass-api.de/api/interpreter`
Method: POST with `data=` parameter (URL-encoded)

### Named route relations (interstates, highways)
```
[out:json][timeout:120];
relation["type"="route"]["route"="road"]
  ["ref"~"^(I |US |MD )"]
  ({LAT_S},{LON_W},{LAT_N},{LON_E});
out body;>;out skel qt;
```

Replace `MD` with your state prefix. The `ref` tag format includes spaces: "I 95", "US 301", "MD 4".

### All roads (for pathfinding graph)
```
[out:json][timeout:60];
way["highway"~"motorway|trunk|primary|secondary|tertiary|residential"]
  ({LAT_S},{LON_W},{LAT_N},{LON_E});
out body;>;out skel qt;
```

### Point features (transit stations, landmarks)
```
[out:json][timeout:30];
node["railway"="station"]["network"="{NETWORK_NAME}"]
  ({LAT_S},{LON_W},{LAT_N},{LON_E});
out body;
```

Replace `railway`/`station`/`network` with whatever POI type your fiction uses as epicenters.

### Land use polygons (urban density)
```
[out:json][timeout:120];
way["landuse"~"residential|commercial|industrial|retail"]
  ({LAT_S},{LON_W},{LAT_N},{LON_E});
out body;>;out skel qt;
```

### Parsing Overpass results

```python
import json

def parse_overpass_roads(filepath):
    """Parse Overpass JSON into adjacency graph for pathfinding."""
    with open(filepath) as f:
        data = json.load(f)
    
    nodes = {}
    ways = []
    for e in data['elements']:
        if e['type'] == 'node':
            nodes[e['id']] = (e['lat'], e['lon'])
        elif e['type'] == 'way':
            ways.append(e)
    
    from collections import defaultdict
    graph = defaultdict(list)
    
    for w in ways:
        nids = w.get('nodes', [])
        for i in range(len(nids) - 1):
            n1, n2 = nids[i], nids[i+1]
            if n1 in nodes and n2 in nodes:
                d = haversine(*nodes[n1], *nodes[n2])
                graph[n1].append((n2, d))
                graph[n2].append((n1, d))
    
    return nodes, graph

def parse_overpass_routes(filepath):
    """Parse route relations into {ref: [[lat,lon], ...]} dict."""
    with open(filepath) as f:
        data = json.load(f)
    
    nodes = {}
    relations = []
    ways_by_id = {}
    
    for e in data['elements']:
        if e['type'] == 'node':
            nodes[e['id']] = (e['lat'], e['lon'])
        elif e['type'] == 'way':
            ways_by_id[e['id']] = e.get('nodes', [])
        elif e['type'] == 'relation':
            relations.append(e)
    
    routes = {}
    for rel in relations:
        ref = rel.get('tags', {}).get('ref', '')
        if not ref:
            continue
        segments = []
        for member in rel.get('members', []):
            if member['type'] == 'way' and member['ref'] in ways_by_id:
                seg = [(nodes[nid][0], nodes[nid][1])
                       for nid in ways_by_id[member['ref']]
                       if nid in nodes]
                if seg:
                    segments.append(seg)
        if segments:
            routes[ref] = segments
    
    return routes

def haversine(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lon points."""
    import numpy as np
    R = 6371000
    p = np.pi / 180
    a = (np.sin((lat2-lat1)*p/2)**2 +
         np.cos(lat1*p) * np.cos(lat2*p) * np.sin((lon2-lon1)*p/2)**2)
    return 2 * R * np.arcsin(np.sqrt(a))
```

### Rasterizing land use for urban density

```python
def rasterize_landuse(polygons, lat_n, lat_s, lon_w, lon_e, h, w):
    """Convert OSM land use polygons to density array."""
    from matplotlib.path import Path
    density = np.zeros((h, w), dtype=np.float32)
    
    for poly_coords in polygons:
        # Convert to pixel coords
        px_coords = []
        for lat, lon in poly_coords:
            r = int((lat_n - lat) / (lat_n - lat_s) * h)
            c = int((lon - lon_w) / (lon_e - lon_w) * w)
            px_coords.append((c, r))
        
        if len(px_coords) < 3:
            continue
        
        path = Path(px_coords)
        # Get bounding box
        rs = [p[1] for p in px_coords]
        cs = [p[0] for p in px_coords]
        r_min, r_max = max(0, min(rs)), min(h-1, max(rs))
        c_min, c_max = max(0, min(cs)), min(w-1, max(cs))
        
        for r in range(r_min, r_max+1):
            for c in range(c_min, c_max+1):
                if path.contains_point((c, r)):
                    density[r, c] = 1.0
    
    from scipy.ndimage import gaussian_filter
    return gaussian_filter(density, sigma=8)
```

### Caching

Always save parsed results to `.json` or `.npy` files. Never re-query Overpass on every render — it's slow and rate-limited (~1 heavy request per 10 seconds).

```python
# Save
np.save("urban_density.npy", density)
with open("routes.json", "w") as f:
    json.dump(routes, f)

# Load
density = np.load("urban_density.npy")
with open("routes.json") as f:
    routes = json.load(f)
```
