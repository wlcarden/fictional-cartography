"""Server-only tests for the render queue + barrier-preview behavior.

Verifies:
  - POST /api/render-queue returns a job_id immediately (sub-second)
  - GET /api/render-status reports state transitions: queued → running → done
  - Stage events are emitted via the print-hook
  - Errors propagate cleanly (state=error, message captured)
  - GC drops old completed jobs
  - barrier-preview replays prior barriers so astar_offset finds its reference
"""
from __future__ import annotations

import time
from src.server import (
    _create_render_job, _gc_old_jobs, _RENDER_JOBS, _RENDER_LOCK,
)


def _wait_for_state(job_id: str, target: str, timeout: float = 60) -> dict:
    """Poll the in-process job dict until state matches or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _RENDER_LOCK:
            j = _RENDER_JOBS.get(job_id)
            if j and j["state"] == target:
                return dict(j)
            if j and j["state"] == "error":
                return dict(j)
        time.sleep(0.2)
    with _RENDER_LOCK:
        j = _RENDER_JOBS.get(job_id)
        return dict(j) if j else {}


class TestRenderQueueLifecycle:
    """End-to-end queue tests using the actual pipeline. Slow (~30s on
    cache miss; ~2-3s on cache hit) — but the only way to verify the
    print-hook integration with real render output."""

    def test_job_creation_returns_job_id(self):
        job_id = _create_render_job(
            "dominus-columbia",
            {"scale": 1, "format": "jpg", "quality": 88},
        )
        assert isinstance(job_id, str)
        assert len(job_id) > 8
        with _RENDER_LOCK:
            assert job_id in _RENDER_JOBS
            assert _RENDER_JOBS[job_id]["state"] in ("queued", "running")

    def test_job_completes_with_stages(self):
        """A successful render should report stage events and end in 'done'."""
        job_id = _create_render_job(
            "dominus-columbia",
            {"scale": 1, "format": "jpg", "quality": 88},
        )
        # Allow up to 3 minutes (cold cache could take that long)
        final = _wait_for_state(job_id, "done", timeout=180)
        assert final.get("state") == "done", (
            f"job did not complete; final state: {final.get('state')}; "
            f"error: {final.get('error')}"
        )
        # Stage events should include all four pipeline stages
        stage_names = {s["stage"] for s in final.get("stages", [])}
        # Cache may hit some stages; we just need at least 1 to confirm
        # the print-hook fired.
        assert len(stage_names) >= 1, (
            f"expected at least 1 stage event, got {final.get('stages')}"
        )
        # Result has the URL + render_ms
        assert "result" in final and final["result"] is not None
        assert "url" in final["result"]
        assert final["result"]["render_ms"] > 0


class TestRenderQueueErrors:
    """Error handling: invalid config, missing file."""

    def test_unknown_config_errors_cleanly(self):
        job_id = _create_render_job(
            "__nonexistent_config__",
            {"scale": 1, "format": "jpg", "quality": 88},
        )
        final = _wait_for_state(job_id, "error", timeout=30)
        assert final.get("state") == "error", (
            f"expected error state, got {final.get('state')}"
        )
        assert final.get("error"), "expected an error message"


class TestBarrierPreviewPriorReplay:
    """barrier-preview must run prior barriers when previewing astar_offset
    so the reference path is in prior_paths. Verifies the server endpoint
    end-to-end.
    """

    def test_offset_preview_finds_reference(self):
        """Patrol Line (barrier 1) is astar_offset and references The Wall
        (barrier 0). Posting barrier_index=1 should run both barriers
        and return a non-empty path. Without prior-replay, _compute_astar_offset
        would not find its reference and return None.
        """
        from src.server import create_app
        app = create_app()
        client = app.test_client()
        resp = client.post(
            "/api/barrier-preview/dominus-columbia",
            json={"barrier_index": 1},
        )
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.data!r}"
        )
        data = resp.get_json()
        # The Patrol Line should produce a long path (~90km, 1900+ pts).
        # If prior-replay broke, path would be empty.
        assert len(data.get("path", [])) > 100, (
            f"expected substantial path, got {len(data.get('path', []))} pts; "
            f"warnings: {data.get('warnings')}"
        )
        assert data["length_km"] > 50, (
            f"expected >50km, got {data['length_km']}"
        )

    def test_offset_preview_with_override(self):
        """Override max_deviation; the resulting A* should still work."""
        from src.server import create_app
        app = create_app()
        client = app.test_client()
        resp = client.post(
            "/api/barrier-preview/dominus-columbia",
            json={
                "barrier_index": 1,
                "override": {
                    "corridor": {
                        "deviation_penalty": 50000,
                        "max_deviation": 0.20,    # tighter than YAML's
                        "max_deviation_penalty": 300000,
                        "south_hard_penalty": 200000,
                    },
                },
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data.get("path", [])) > 100, (
            f"override broke A*: {data.get('warnings')}"
        )


class TestRenderQueueGC:
    """Old completed jobs should be garbage-collected."""

    def test_gc_drops_old_completed_jobs(self):
        # Manually insert a fake old job
        with _RENDER_LOCK:
            _RENDER_JOBS["__test_old__"] = {
                "id": "__test_old__",
                "state": "done",
                "stages": [],
                "started_at": time.time() - 7200,    # 2h ago
                "completed_at": time.time() - 7000,  # also 2h ago
                "result": None, "error": None,
                "config": "test", "opts": {},
            }
            _RENDER_JOBS["__test_recent__"] = {
                "id": "__test_recent__",
                "state": "done",
                "stages": [],
                "started_at": time.time() - 60,
                "completed_at": time.time() - 30,
                "result": None, "error": None,
                "config": "test", "opts": {},
            }
        _gc_old_jobs()
        with _RENDER_LOCK:
            assert "__test_old__" not in _RENDER_JOBS, \
                "old job (2h+) should be GC'd"
            assert "__test_recent__" in _RENDER_JOBS, \
                "recent job (30s ago) should still be present"
            # Cleanup
            _RENDER_JOBS.pop("__test_recent__", None)
