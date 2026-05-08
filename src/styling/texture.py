"""Texture and atmosphere effects: noise, paper grain, vignette, coastline,
wasteland wash, urbanization tint.

All functions take a float32 (h, w, 3) array in [0, 1] and mutate it in-place
unless noted. Random seeds are fixed to keep output deterministic.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import binary_dilation, gaussian_filter


# Fixed seeds preserve byte-identical output across runs, matching the
# reference render's noise patterns.
DRIFT_SEED = 777
SCORCH_SEED = 888
PAPER_SEED = 42


def _per_channel(value, default: float) -> list[float]:
    """Coerce a noise param into a 3-list (R/G/B). Accepts:
        - None             → [default] * 3
        - scalar (number)  → [value]   * 3      (back-compat with old configs)
        - [r, g, b]        → list      [r,g,b]
        - {r, g, b}        → list      [r,g,b]   (any missing channel → default)
    """
    if value is None:
        return [float(default)] * 3
    if isinstance(value, dict):
        return [float(value.get(k, default)) for k in ('r', 'g', 'b')]
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError(f"per-channel noise list needs exactly 3 entries, got {len(value)}")
        return [float(x) for x in value]
    return [float(value)] * 3


def apply_drift_noise(
    rgb: np.ndarray,
    land_mask: np.ndarray,
    sigma,
    strength,
    seed: int = DRIFT_SEED,
) -> None:
    """Large-scale per-channel color drift over land — mimics soil/rock variation.

    `sigma` and `strength` may be scalars (uniform across channels — original
    behavior) or 3-lists/dicts to drift each channel independently. Use
    different sigmas to get color-separated texture (e.g. coarse red, fine
    blue) for a more painterly look.
    """
    sigmas    = _per_channel(sigma,    0)
    strengths = _per_channel(strength, 0)
    h, w = rgb.shape[:2]
    rng = np.random.RandomState(seed)
    for c in range(3):
        n = rng.normal(0, 1, (h, w)).astype(np.float32)
        drift = gaussian_filter(n, sigma=sigmas[c]) * strengths[c]
        rgb[..., c] = np.clip(rgb[..., c] + drift * land_mask, 0, 1)


def apply_scorch(
    rgb: np.ndarray,
    land_mask: np.ndarray,
    sigma,
    strength,
    seed: int = SCORCH_SEED,
) -> None:
    """Mid-scale patches of desaturated, slightly warmed earth.

    `sigma` controls the patch SIZE — single value (uniform per-channel
    structure) or 3-list/dict to make red/green/blue patches at different
    scales. `strength` controls how strongly each channel desaturates;
    again scalar or per-channel.

    The scalar path is byte-identical to the original implementation: a
    single noise array is generated and shared across the three channel
    blends. The per-channel path generates three independent noise arrays.
    Old configs render the same as before; new configs that opt into a
    per-channel sigma/strength get independent patches per channel.
    """
    sigmas    = _per_channel(sigma,    0)
    strengths = _per_channel(strength, 0)
    h, w = rgb.shape[:2]
    rng = np.random.RandomState(seed)
    grey = 0.3 * rgb[..., 0] + 0.59 * rgb[..., 1] + 0.11 * rgb[..., 2]
    coefs   = (0.5, 0.6, 0.7)
    offsets = (0.03, -0.01, -0.03)

    uniform = (sigmas[0] == sigmas[1] == sigmas[2]
               and strengths[0] == strengths[1] == strengths[2])
    if uniform:
        # Back-compat: single shared noise array, original RNG sequence.
        n = rng.normal(0, 1, (h, w)).astype(np.float32)
        scorch = np.clip(gaussian_filter(n, sigma=sigmas[0]), 0, 2) * strengths[0] * land_mask
        for c in range(3):
            rgb[..., c] = rgb[..., c] * (1 - scorch * coefs[c]) + (grey + offsets[c]) * (scorch * coefs[c])
    else:
        # Per-channel: independent noise arrays, each at its own sigma.
        for c in range(3):
            n = rng.normal(0, 1, (h, w)).astype(np.float32)
            scorch_c = np.clip(gaussian_filter(n, sigma=sigmas[c]), 0, 2) * strengths[c] * land_mask
            rgb[..., c] = rgb[..., c] * (1 - scorch_c * coefs[c]) + (grey + offsets[c]) * (scorch_c * coefs[c])


def apply_urbanization(
    rgb: np.ndarray,
    density: np.ndarray,
    land_mask: np.ndarray,
    color: tuple[float, float, float],
    blend_strength: float,
) -> None:
    """Tint dense urban areas toward `color` (a cold concrete grey-brown by default)."""
    mask = density * land_mask * blend_strength
    inv = 1 - mask
    rgb[..., 0] = rgb[..., 0] * inv + color[0] * mask
    rgb[..., 1] = rgb[..., 1] * inv + color[1] * mask
    rgb[..., 2] = rgb[..., 2] * inv + color[2] * mask


def apply_wasteland_wash(
    rgb: np.ndarray,
    land_mask: np.ndarray,
    desaturate: float,
    warm_push_r: float,
    cool_push_b: float,
) -> None:
    """Subtle global desaturation + warm-cool shift to sell the post-apocalyptic feel."""
    grey = 0.3 * rgb[..., 0] + 0.59 * rgb[..., 1] + 0.11 * rgb[..., 2]
    inv = 1 - desaturate
    rgb[..., 0] = rgb[..., 0] * inv + grey * desaturate
    rgb[..., 1] = rgb[..., 1] * inv + grey * desaturate
    rgb[..., 2] = rgb[..., 2] * inv + grey * desaturate
    rgb[..., 0] = np.clip(rgb[..., 0] + warm_push_r * land_mask, 0, 1)
    rgb[..., 2] = np.clip(rgb[..., 2] + cool_push_b * land_mask, 0, 1)


def composite_land_water(
    land_rgb: np.ndarray, water_rgb: np.ndarray, land_mask: np.ndarray
) -> np.ndarray:
    """np.where over the per-pixel mask. Returns a fresh (h, w, 3) array."""
    out = np.empty_like(land_rgb)
    m3 = land_mask[..., None]
    out[:] = np.where(m3, np.clip(land_rgb, 0, 1), water_rgb)
    return out


def darken_coastline(
    rgb: np.ndarray, water_mask: np.ndarray, dilation: int, factor: float
) -> None:
    """Multiply land cells within `dilation` steps of water by `factor`."""
    coast = binary_dilation(water_mask, iterations=dilation) & (~water_mask)
    rgb[coast] *= factor


def apply_paper_grain(
    rgb: np.ndarray,
    fine_sigma: float = 12.0,
    smooth_strength_sigma: float = 20.0,
    smooth_blur_sigma: float | None = None,
    seed: int = PAPER_SEED,
) -> None:
    """Two-layer paper noise. Operates on rgb in [0, 255] (mutates in-place).

    `smooth_blur_sigma` defaults to a resolution-dependent value
    (`min(h, w) / 50`, clamped to [22, 80]) so the smooth grain reads as
    "aged parchment" at every render scale rather than turning blotchy
    at small resolutions or invisible at large ones. Pass an explicit
    value to override.
    """
    h, w = rgb.shape[:2]
    if smooth_blur_sigma is None:
        smooth_blur_sigma = max(22.0, min(80.0, min(h, w) / 50.0))
    rng = np.random.RandomState(seed)
    for c in range(3):
        rgb[..., c] += rng.normal(0, fine_sigma, (h, w)).astype(np.float32)
    for c in range(3):
        smooth = gaussian_filter(
            rng.normal(0, smooth_strength_sigma, (h, w)).astype(np.float32),
            sigma=smooth_blur_sigma,
        )
        rgb[..., c] += smooth


def apply_vignette(rgb: np.ndarray, strength: float, floor: float) -> None:
    """Radial vignette around the image center. rgb in [0, 255], in-place."""
    h, w = rgb.shape[:2]
    vy, vx = np.mgrid[0:h, 0:w]
    vd = np.sqrt(((vx - w / 2) / (w * 0.6)) ** 2 + ((vy - h / 2) / (h * 0.6)) ** 2)
    factor = np.clip(1 - strength * vd, floor, 1.0).astype(np.float32)
    for c in range(3):
        rgb[..., c] *= factor
