"""The chamber node's marker file: a simulated camera keeps its own, so a marker drawn
on the twin's image never reaches a production start on the webcam (DRY-FULL-02)."""

from __future__ import annotations

import queue

from lumi.pascal.hardware import build_fiducials


def test_a_simulated_camera_keeps_its_markers_apart_from_the_webcams():
    real, _ = build_fiducials(queue.Queue())
    sim, _ = build_fiducials(queue.Queue(), simulated=True)
    assert real.path.name == "chamber_fiducials.json"
    assert sim.path.name == "chamber_fiducials.sim.json"
    assert real.path.parent == sim.path.parent


def test_an_explicit_path_wins(tmp_path):
    store, _ = build_fiducials(queue.Queue(), store_path=str(tmp_path / "m.json"), simulated=True)
    assert store.path == tmp_path / "m.json"
