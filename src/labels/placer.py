"""Collision-aware label placement.

The placer holds a list of reserved bounding boxes and, for each call to
`place()`, evaluates 48 candidate positions around an anchor (8 angle offsets x
6 distance rings). The chosen candidate minimizes
    score = overlap_area * 50 + distance * 1.0
subject to staying inside the placer's margin bounds. If a candidate has zero
overlap and is at one of the two closest distances, it short-circuits as
optimal — preserving the reference's "prefer close, exit early when free."
"""
from __future__ import annotations

import math
from typing import Iterable

from PIL import ImageDraw, ImageFont


# Score weights and candidate-position grid match the reference render.
OVERLAP_WEIGHT = 50.0
DISTANCE_WEIGHT = 1.0
CANDIDATE_DISTANCES = (22, 35, 50, 70, 95, 125)
CANDIDATE_ANGLE_OFFSETS = (0, 45, -45, 90, -90, 135, -135, 180)


class LabelPlacer:
    """Tracks reserved boxes; finds non-overlapping positions for new labels."""

    def __init__(
        self,
        draw: ImageDraw.ImageDraw,
        margin_left: int,
        margin_top: int,
        margin_right: int,
        margin_bottom: int,
    ):
        self._draw = draw
        self.boxes: list[tuple[int, int, int, int]] = []
        self.ml = margin_left
        self.mt = margin_top
        self.mr = margin_right
        self.mb = margin_bottom

    def text_size(self, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
        bb = self._draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]

    def _overlap(self, box: tuple[int, int, int, int]) -> float:
        total = 0.0
        for pb in self.boxes:
            dx = max(0, min(box[2], pb[2]) - max(box[0], pb[0]))
            dy = max(0, min(box[3], pb[3]) - max(box[1], pb[1]))
            total += dx * dy
        return total

    def _in_bounds(self, box: tuple[int, int, int, int]) -> bool:
        return (
            box[0] >= self.ml
            and box[2] <= self.mr
            and box[1] >= self.mt
            and box[3] <= self.mb
        )

    def place(
        self,
        anchor_x: int,
        anchor_y: int,
        texts_fonts: Iterable[tuple[str, ImageFont.FreeTypeFont]],
        pad: int = 8,
        preferred_angle: int = 0,
    ) -> tuple[int, int]:
        """Pick the best label position near (anchor_x, anchor_y).

        `texts_fonts` is an iterable of (text, font) pairs stacked vertically.
        Returns (label_x, label_y) — the top-left of the text block.
        """
        texts_fonts = list(texts_fonts)
        bw, bh = 0, 0
        for t, f in texts_fonts:
            tw, th = self.text_size(t, f)
            bw = max(bw, tw)
            bh += th + 4
        bh = max(0, bh - 4)

        candidates: list[tuple[int, int, int]] = []
        for dist in CANDIDATE_DISTANCES:
            for ao in CANDIDATE_ANGLE_OFFSETS:
                angle = math.radians(preferred_angle + ao)
                cx = anchor_x + int(dist * math.cos(angle))
                cy = anchor_y - int(dist * math.sin(angle))
                aa = (preferred_angle + ao) % 360
                # Anchor the label box so its corner-on-the-marker side
                # touches (cx, cy) — keeps text from overlapping the marker.
                if aa < 45 or aa >= 315:
                    lx, ly = cx, cy - bh // 2
                elif aa < 135:
                    lx, ly = cx - bw // 2, cy - bh
                elif aa < 225:
                    lx, ly = cx - bw, cy - bh // 2
                else:
                    lx, ly = cx - bw // 2, cy
                candidates.append((lx, ly, dist))

        best: tuple[int, int, tuple[int, int, int, int]] | None = None
        best_score = float("inf")
        for lx, ly, dist in candidates:
            box = (lx - pad, ly - pad, lx + bw + pad, ly + bh + pad)
            if not self._in_bounds(box):
                continue
            ov = self._overlap(box)
            score = ov * OVERLAP_WEIGHT + dist * DISTANCE_WEIGHT
            if score < best_score:
                best_score = score
                best = (lx, ly, box)
            if ov == 0 and dist <= 35:
                break  # close-and-clear is optimal; short-circuit

        if best is None:
            # Fallback: clamp inside bounds at the preferred-side default
            lx = max(self.ml + pad, min(anchor_x + 22, self.mr - bw - pad))
            ly = max(self.mt + pad, min(anchor_y - bh // 2, self.mb - bh - pad))
            best = (lx, ly, (lx - pad, ly - pad, lx + bw + pad, ly + bh + pad))

        self.boxes.append(best[2])
        return best[0], best[1]

    def reserve(self, box: tuple[int, int, int, int]) -> None:
        self.boxes.append(box)
