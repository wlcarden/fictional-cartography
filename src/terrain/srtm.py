"""SRTM tile loading: fetch, mosaic, crop to bounds, enforce aspect ratio, downsample.

Tiles come from Mapzen's mirror at elevation-tiles-prod (no auth required).
Cached as ungzipped .hgt files in cache/srtm/.
"""
from __future__ import annotations

import gzip
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
from scipy.ndimage import zoom

from src.pipeline import MapBounds, cache_dir


SRTM_TILE_URL_TEMPLATE = (
    "https://elevation-tiles-prod.s3.amazonaws.com/skadi/"
    "{lat_dir}{lat:02d}/{lat_dir}{lat:02d}{lon_dir}{lon:03d}.hgt.gz"
)
TILE_SIZE = 3601  # SRTMGL1 tiles are 3601 x 3601 pixels per 1° x 1° cell


def tile_name(lat_floor: int, lon_floor: int) -> str:
    lat_dir = "N" if lat_floor >= 0 else "S"
    lon_dir = "E" if lon_floor >= 0 else "W"
    return f"{lat_dir}{abs(lat_floor):02d}{lon_dir}{abs(lon_floor):03d}"


def tile_url(lat_floor: int, lon_floor: int) -> str:
    return SRTM_TILE_URL_TEMPLATE.format(
        lat_dir="N" if lat_floor >= 0 else "S",
        lat=abs(lat_floor),
        lon_dir="E" if lon_floor >= 0 else "W",
        lon=abs(lon_floor),
    )


def required_tiles(bounds: MapBounds) -> list[tuple[int, int]]:
    """Return SW-corner (lat_floor, lon_floor) of every tile covering bounds."""
    lat_lo = math.floor(bounds.lat_s)
    lat_hi = math.floor(bounds.lat_n - 1e-9)
    lon_lo = math.floor(bounds.lon_w)
    lon_hi = math.floor(bounds.lon_e - 1e-9)
    return [
        (la, lo)
        for la in range(lat_lo, lat_hi + 1)
        for lo in range(lon_lo, lon_hi + 1)
    ]


def load_tile(path: Path) -> np.ndarray:
    """Read a single .hgt file as a (3601, 3601) float32 array, with NaN for voids."""
    data = np.fromfile(str(path), dtype=">i2").reshape(TILE_SIZE, TILE_SIZE).astype(np.float32)
    data[data < -100] = np.nan
    return data


def download_tile(lat_floor: int, lon_floor: int, dest: Path) -> bool:
    """Download a tile from Mapzen S3, ungzip, write to dest. Returns False on 404."""
    url = tile_url(lat_floor, lon_floor)
    req = Request(url, headers={
        "User-Agent": "fictional-cartography/0.1 (+https://github.com/wlcarden/fictional-cartography)",
    })
    try:
        with urlopen(req, timeout=180) as resp:
            gz_bytes = resp.read()
    except HTTPError as e:
        if e.code == 404:
            return False
        raise RuntimeError(f"SRTM download {tile_name(lat_floor, lon_floor)}: HTTP {e.code}") from e
    except URLError as e:
        raise RuntimeError(f"SRTM download {tile_name(lat_floor, lon_floor)}: {e}") from e
    raw = gzip.decompress(gz_bytes)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return True


def get_tile(lat_floor: int, lon_floor: int, allow_download: bool = True) -> np.ndarray | None:
    """Return tile array, downloading if missing. None if tile unavailable (ocean-only)."""
    name = tile_name(lat_floor, lon_floor)
    cache_path = cache_dir("srtm") / f"{name}.hgt"
    missing_marker = cache_dir("srtm") / f"{name}.missing"
    if missing_marker.exists():
        return None
    if not cache_path.exists():
        if not allow_download:
            return None
        print(f"  fetching {name}...")
        ok = download_tile(lat_floor, lon_floor, cache_path)
        if not ok:
            missing_marker.touch()
            return None
    return load_tile(cache_path)


def mosaic(tiles: dict[tuple[int, int], np.ndarray]) -> tuple[np.ndarray, MapBounds]:
    """Combine tiles into one array. Missing tiles fill with NaN (treated as ocean)."""
    if not tiles:
        raise ValueError("No tiles to mosaic")
    lats = sorted({k[0] for k in tiles}, reverse=True)
    lons = sorted({k[1] for k in tiles})
    rows = []
    for la in lats:
        row_arrays = []
        for lo in lons:
            tile = tiles.get((la, lo))
            if tile is None:
                tile = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
            row_arrays.append(tile)
        rows.append(np.hstack(row_arrays))
    arr = np.vstack(rows)
    bounds = MapBounds(
        lat_n=max(lats) + 1,
        lat_s=min(lats),
        lon_w=min(lons),
        lon_e=max(lons) + 1,
    )
    return arr, bounds


def crop_to_bounds(
    dem: np.ndarray, mosaic_bounds: MapBounds, target: MapBounds
) -> tuple[np.ndarray, MapBounds]:
    """Crop a mosaic to the intersection of target and mosaic bounds."""
    h, w = dem.shape
    eff = MapBounds(
        lat_n=min(target.lat_n, mosaic_bounds.lat_n),
        lat_s=max(target.lat_s, mosaic_bounds.lat_s),
        lon_w=max(target.lon_w, mosaic_bounds.lon_w),
        lon_e=min(target.lon_e, mosaic_bounds.lon_e),
    )
    rs = int((mosaic_bounds.lat_n - eff.lat_n) / mosaic_bounds.lat_span * h)
    re = int((mosaic_bounds.lat_n - eff.lat_s) / mosaic_bounds.lat_span * h)
    cs = int((eff.lon_w - mosaic_bounds.lon_w) / mosaic_bounds.lon_span * w)
    ce = int((eff.lon_e - mosaic_bounds.lon_w) / mosaic_bounds.lon_span * w)
    return dem[rs:re, cs:ce], eff


def enforce_aspect_ratio(
    dem: np.ndarray, bounds: MapBounds, aspect: tuple[int, int]
) -> tuple[np.ndarray, MapBounds]:
    """Center-crop DEM so width:height matches aspect (w_units, h_units)."""
    aw, ah = aspect
    h, w = dem.shape
    target_ratio = aw / ah
    current_ratio = w / h
    if abs(current_ratio - target_ratio) < 1e-6:
        return dem, bounds
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        crop = (w - new_w) // 2
        new_dem = dem[:, crop:crop + new_w]
        span = bounds.lon_span
        new_lon_w = bounds.lon_w + (crop / w) * span
        new_lon_e = new_lon_w + (new_w / w) * span
        return new_dem, MapBounds(bounds.lat_n, bounds.lat_s, new_lon_w, new_lon_e)
    new_h = int(w / target_ratio)
    crop = (h - new_h) // 2
    new_dem = dem[crop:crop + new_h, :]
    span = bounds.lat_span
    new_lat_n = bounds.lat_n - (crop / h) * span
    new_lat_s = new_lat_n - (new_h / h) * span
    return new_dem, MapBounds(new_lat_n, new_lat_s, bounds.lon_w, bounds.lon_e)


def downsample(dem: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return dem
    return zoom(dem, 1.0 / factor, order=3)


def load_terrain(
    bounds: MapBounds,
    aspect: tuple[int, int] | None = None,
    downsample_factor: int = 1,
    allow_download: bool = True,
) -> tuple[np.ndarray, MapBounds]:
    """Full SRTM pipeline.

    Returns (dem_array, adjusted_bounds). adjusted_bounds reflect
    intersection-with-available-tiles AND aspect-ratio cropping. All subsequent
    pipeline steps must use these bounds, not the originally requested ones.
    """
    print(f"Loading terrain for bounds {bounds.lat_s:.3f}-{bounds.lat_n:.3f}N, "
          f"{bounds.lon_w:.3f}-{bounds.lon_e:.3f}E")
    needed = required_tiles(bounds)
    print(f"  {len(needed)} tile(s) required")
    tiles: dict[tuple[int, int], np.ndarray] = {}
    for la, lo in needed:
        tile = get_tile(la, lo, allow_download=allow_download)
        if tile is not None:
            tiles[(la, lo)] = tile
        else:
            print(f"  {tile_name(la, lo)}: unavailable (ocean-only)")
    if not tiles:
        raise RuntimeError("No SRTM tiles available for the requested bounds")

    arr, mb = mosaic(tiles)
    dem, eff = crop_to_bounds(arr, mb, bounds)
    print(f"  cropped to {dem.shape}")

    if aspect:
        dem, eff = enforce_aspect_ratio(dem, eff, aspect)
        print(f"  aspect {aspect[0]}:{aspect[1]} -> {dem.shape}")

    if downsample_factor > 1:
        dem = downsample(dem, downsample_factor)
        print(f"  downsampled by {downsample_factor}x -> {dem.shape}")

    return dem, eff
