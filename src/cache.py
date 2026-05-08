"""Stage cache for the rendering pipeline.

Each pipeline stage produces an asset (a numpy array, a PIL image, etc.) plus
a small metadata sidecar. The asset is keyed by a SHA-1 over the config
sections that feed it; if the key matches what's already on disk, we skip
recomputing and load from the cache instead.

Layout under cache/stages/<config_name>/:
    terrain_dem.npy           DEM array (float32, h x w)
    terrain_dem.meta.json     {key, bounds, h, w}
    terrain_styled.png        terrain image (RGB)
    terrain_styled.npy        water_mask (bool, h x w)
    terrain_styled.meta.json  {key}
    canvas_roads.png          canvas image (RGB; border + roads)
    canvas_roads.meta.json    {key}
    final.png                 final composed image
    final.meta.json           {key}
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from src.pipeline import cache_dir


def stage_key(*parts: Any, **named: Any) -> str:
    """SHA-1 hex digest over canonical JSON of all inputs.

    Accepts both positional and keyword inputs. Keyword args sort by name so
    insertion order doesn't change the hash.
    """
    payload = json.dumps(
        {"parts": parts, "named": named},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(payload.encode()).hexdigest()


@dataclass
class StageCache:
    """Per-config cache directory."""

    name: str

    @property
    def dir(self) -> Path:
        d = cache_dir("stages") / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- key bookkeeping ----------------------------------------------------
    def _meta(self, stage: str) -> Path:
        return self.dir / f"{stage}.meta.json"

    def get_meta(self, stage: str) -> dict | None:
        p = self._meta(stage)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def hit(self, stage: str, key: str) -> bool:
        meta = self.get_meta(stage)
        return bool(meta and meta.get("key") == key)

    def write_meta(self, stage: str, key: str, **extra: Any) -> None:
        self._meta(stage).write_text(json.dumps({"key": key, **extra}))

    # ---- asset I/O ----------------------------------------------------------
    def save_npy(self, stage: str, key: str, arr: np.ndarray, **meta: Any) -> None:
        np.save(self.dir / f"{stage}.npy", arr, allow_pickle=False)
        self.write_meta(stage, key, **meta)

    def load_npy(self, stage: str) -> np.ndarray:
        return np.load(self.dir / f"{stage}.npy", allow_pickle=False)

    def save_image(self, stage: str, key: str, img: Image.Image, **meta: Any) -> None:
        img.save(self.dir / f"{stage}.png", format="PNG", optimize=False)
        self.write_meta(stage, key, **meta)

    def load_image(self, stage: str) -> Image.Image:
        # Preserve original mode (RGBA matters for the canvas stage so subsequent
        # alpha-composite operations work).
        return Image.open(self.dir / f"{stage}.png")

    def save_image_and_mask(
        self, stage: str, key: str, img: Image.Image, mask: np.ndarray, **meta: Any
    ) -> None:
        img.save(self.dir / f"{stage}.png", format="PNG", optimize=False)
        np.save(self.dir / f"{stage}.npy", mask, allow_pickle=False)
        self.write_meta(stage, key, **meta)

    def load_image_and_mask(self, stage: str) -> tuple[Image.Image, np.ndarray]:
        img = Image.open(self.dir / f"{stage}.png")
        mask = np.load(self.dir / f"{stage}.npy", allow_pickle=False)
        return img, mask
