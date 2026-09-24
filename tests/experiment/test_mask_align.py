"""T0 tests for mask-centre auto-alignment: the slit fit on synthetic traces, and the
driver op end to end against a fake chamber camera whose marker brightens only while
the (fake) mask's slit is over it."""

from __future__ import annotations

import asyncio
import re
import uuid

import numpy as np
import pytest

from lumi.contracts.payloads.experiment import (
    AutoAlignMaskCenter,
    CenterMaskPos,
    ConfirmCenterMask,
    ConfirmMaskCenter,
)
from lumi.experiment.handlers import ExperimentHandler
from lumi.contracts.payloads.fiducial import (
    MASK_CENTER_ROLE,
    CrossShape,
    FiducialMarker,
    MarkerList,
    MarkerStats,
    MarkerStatsSample,
    RoleMap,
)
from lumi.experiment.mask_align import SlitNotFound, locate_slit

from test_handlers import handler  # noqa: F401 -- the fixture (tests/ is not a package)


def triangle(positions, center, half_width=1.0, baseline=100.0, height=100.0, polarity=1):
    pos = np.asarray(positions, dtype=float)
    return baseline + polarity * height * np.clip(1 - np.abs(pos - center) / half_width, 0, None)


# --- the fit -----------------------------------------------------------------------


@pytest.mark.parametrize("center", [100.0, 100.3, 101.25, 98.9])
def test_a_bright_slit_is_centred_between_sweep_points(center):
    positions = np.arange(96.0, 104.01, 0.25)
    fit = locate_slit(positions, triangle(positions, center))
    assert fit.center == pytest.approx(center, abs=0.1)
    assert fit.polarity == 1
    assert fit.baseline == pytest.approx(100.0)


def test_a_slit_darker_than_the_plate_is_found_too():
    """A sample darker than the plate reads as a dip, not a peak."""
    positions = np.arange(96.0, 104.01, 0.25)
    fit = locate_slit(positions, triangle(positions, 99.4, polarity=-1, height=60))
    assert fit.center == pytest.approx(99.4, abs=0.1)
    assert fit.polarity == -1
    assert fit.contrast == pytest.approx(60, abs=10)


def test_a_flat_trace_is_no_slit():
    positions = np.arange(96.0, 104.01, 0.5)
    with pytest.raises(SlitNotFound, match="no slit seen"):
        locate_slit(positions, np.full(positions.shape, 100.0) + np.random.default_rng(0).normal(0, 0.5, positions.shape))


def test_a_slit_at_the_edge_of_the_window_is_not_trusted():
    positions = np.arange(96.0, 104.01, 0.5)
    with pytest.raises(SlitNotFound, match="off the edge"):
        locate_slit(positions, triangle(positions, 104.0))


# --- the driver op -----------------------------------------------------------------


class FakeFiducial:
    """Answers like the chamber's fiducial capability. The tagged marker reads bright
    only while the mask -- wherever the fake MI runner last moved it -- has its slit
    within 1 mm of `true_center`; a fresh frame (new uuid) on every call."""

    def __init__(self, mi, true_center: float, roles: dict[str, str] | None = None) -> None:
        self.mi = mi
        self.true_center = true_center
        self.roles = {MASK_CENTER_ROLE: "m41"} if roles is None else roles

    def _mask_position(self) -> float:
        for call in reversed(self.mi.calls):
            m = re.search(r"Set Mask Position M1=([\d.]+)", call)
            if m:
                return float(m.group(1))
        return 0.0

    async def list_roles(self) -> RoleMap:
        return RoleMap(roles=self.roles)

    async def list_markers(self) -> MarkerList:
        return MarkerList(markers=[FiducialMarker(marker_id="m41", shape=CrossShape(kind="cross", x=1, y=1))])

    async def marker_stats(self) -> MarkerStatsSample:
        reading = float(triangle([self._mask_position()], self.true_center)[0])
        return MarkerStatsSample(
            time=0.0, uuid=uuid.uuid4().hex, time_stamp="",
            stats={"m41": MarkerStats(n_pixels=9, mean=reading, center=reading)},
        )


async def run_task(handler, req: AutoAlignMaskCenter) -> dict:  # noqa: F811
    ack = await handler.auto_align_center_mask(req)
    assert handler.readout().current_task.id == ack.task_id
    for _ in range(500):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            return event.task_result
        await asyncio.sleep(0.01)
    pytest.fail("task never reported completion")


async def run_task_result(handler) -> dict:  # noqa: F811
    """The task_result of a task already started."""
    for _ in range(500):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            return event.task_result
        await asyncio.sleep(0.01)
    pytest.fail("task never reported completion")


def with_fiducial(handler, fiducial) -> None:  # noqa: F811
    handler.sources["chamber_fiducial"] = fiducial
    handler.build_manager()


async def test_auto_align_finds_the_slit_and_applies_it(handler):  # noqa: F811
    mi = handler.sources["chamber_mi"]
    with_fiducial(handler, FakeFiducial(mi, true_center=101.3))  # fixture's center_mask_pos is 100

    result = await run_task(handler, AutoAlignMaskCenter(half_window_mm=4.0, step_mm=0.25))

    assert result["ok"] is True, result
    assert result["marker_id"] == "m41"
    assert result["previous_center"] == 100.0
    assert result["center"] == pytest.approx(101.3, abs=0.1)
    assert handler.pld_config.center_mask_pos == pytest.approx(result["center"])
    assert sum(1 for s in result["samples"] if s["pass_index"] == 0) == 33
    # Set up like begin_check_mask_center, and leaves the mask at the found centre.
    assert "Select Target Clear" in "".join(mi.calls)
    assert "Sample Shutter ON" in "".join(mi.calls)
    assert mi.calls[-1].strip() == f"Set Mask Position M1={result['center']:.2f}"


def mask_moves(mi) -> list[float]:
    return [float(m) for m in re.findall(r"Set Mask Position M1=([\d.]+)", "".join(mi.calls))]


async def test_each_pass_is_one_increasing_scan_with_no_back_and_forth(handler):  # noqa: F811
    """The mask only ever moves backward to start a new pass or to park at the found
    centre -- never mid-scan."""
    mi = handler.sources["chamber_mi"]
    with_fiducial(handler, FakeFiducial(mi, true_center=101.3))

    result = await run_task(handler, AutoAlignMaskCenter(max_passes=3, points_per_pass=11))

    moves = mask_moves(mi)
    backward = sum(1 for a, b in zip(moves, moves[1:]) if b < a)
    assert backward <= len(result["passes"])  # each later pass's start, plus the final park
    wide = [s["position"] for s in result["samples"] if s["pass_index"] == 0]
    assert wide == pytest.approx([96.0 + 0.5 * i for i in range(17)])


async def test_later_passes_narrow_in_on_the_slit_with_a_finer_step(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=101.3))

    result = await run_task(handler, AutoAlignMaskCenter(max_passes=3, points_per_pass=15, tolerance_mm=1e-6))

    passes = result["passes"]
    assert len(passes) == 3
    assert result["converged"] is False  # tolerance too tight to ever be met
    assert passes[1]["step"] < passes[0]["step"]
    assert passes[1]["stop"] - passes[1]["start"] < passes[0]["stop"] - passes[0]["start"]
    for p in passes[1:]:
        assert p["start"] < 101.3 < p["stop"]
    assert result["center"] == pytest.approx(101.3, abs=0.05)


async def test_refining_stops_once_the_centre_settles(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=100.0))

    result = await run_task(handler, AutoAlignMaskCenter(max_passes=6, tolerance_mm=0.05))

    assert result["converged"] is True
    assert len(result["passes"]) < 6


async def test_apply_false_reports_but_leaves_center_mask_pos(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=99.0))

    result = await run_task(handler, AutoAlignMaskCenter(apply=False))

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["center"] == pytest.approx(99.0, abs=0.1)
    assert handler.pld_config.center_mask_pos == 100.0


async def test_without_a_tagged_marker_and_no_wait_the_task_fails_and_nothing_moves(handler):  # noqa: F811
    mi = handler.sources["chamber_mi"]
    with_fiducial(handler, FakeFiducial(mi, true_center=100.0, roles={}))

    result = await run_task(handler, AutoAlignMaskCenter(role_wait_timeout_s=0))

    assert result["ok"] is False
    assert MASK_CENTER_ROLE in result["error"]
    assert not any("Set Mask Position" in c for c in mi.calls)
    assert handler.pld_config.center_mask_pos == 100.0


async def test_a_slit_outside_the_window_fails_and_keeps_the_old_center(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=110.0))

    result = await run_task(handler, AutoAlignMaskCenter(half_window_mm=4.0))

    assert result["ok"] is False
    assert "no slit seen" in result["error"]
    assert handler.pld_config.center_mask_pos == 100.0


async def test_center_mm_moves_the_scan_but_previous_center_stays_the_calibration(handler):  # noqa: F811
    mi = handler.sources["chamber_mi"]
    with_fiducial(handler, FakeFiducial(mi, true_center=110.0))

    result = await run_task(handler, AutoAlignMaskCenter(center_mm=109.0, half_window_mm=4.0, apply=False))

    assert result["ok"] is True, result
    wide = [s["position"] for s in result["samples"] if s["pass_index"] == 0]
    assert wide[0] == pytest.approx(105.0) and wide[-1] == pytest.approx(113.0)
    assert result["center"] == pytest.approx(110.0, abs=0.1)
    assert result["previous_center"] == 100.0
    assert handler.pld_config.center_mask_pos == 100.0


async def test_the_result_event_names_its_task_even_when_it_fails_at_once(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=100.0, roles={}))

    ack = await handler.auto_align_center_mask(AutoAlignMaskCenter(role_wait_timeout_s=0))
    for _ in range(500):
        event = await handler.next_update()
        if event is not None and event.task_result is not None:
            break
        await asyncio.sleep(0.01)

    assert event.task_result["ok"] is False
    assert event.current_task is None
    assert event.finished_task.id == ack.task_id
    assert event.finished_task.kind == "auto_align_center_mask"


def test_the_contract_declares_what_auto_align_reports():
    from lumi.codegen.ts_client import emit
    from lumi.contracts.experiment import EXPERIMENT
    from lumi.contracts.payloads.experiment import MaskAlignResult

    (op,) = [o for cap in EXPERIMENT.capabilities for o in cap.ops if o.name == "auto_align_center_mask"]
    assert op.result is MaskAlignResult
    ts = emit()
    assert "export interface MaskAlignResult" in ts
    assert "export interface MaskSweepPoint" in ts


# --- the calibration itself: on the state, and settable without moving ------------


async def test_center_mask_pos_is_on_the_state(handler):  # noqa: F811
    assert handler.readout().center_mask_pos == 100.0
    handler.pld_config.center_mask_pos = 97.2
    assert handler.readout().center_mask_pos == 97.2


async def test_set_center_mask_pos_stores_it_and_moves_nothing(handler):  # noqa: F811
    mi = handler.sources["chamber_mi"]
    n_calls = len(mi.calls)

    await handler.set_center_mask_pos(CenterMaskPos(position=97.219))

    assert handler.pld_config.center_mask_pos == 97.219
    assert handler.readout().center_mask_pos == 97.219
    assert len(mi.calls) == n_calls


async def test_set_center_mask_pos_refuses_a_position_past_the_travel(handler):  # noqa: F811
    with pytest.raises(ValueError, match="travel"):
        await handler.set_center_mask_pos(CenterMaskPos(position=handler.bounds.mask_travel_max))
    assert handler.pld_config.center_mask_pos == 100.0


async def restarted(handler) -> ExperimentHandler:  # noqa: F811
    """A fresh handler on the same growth.db, starting from settings' value again --
    what a node restart looks like."""
    handler.pld_config.center_mask_pos = 100.0
    again = ExperimentHandler(
        sources=handler.sources, growth_db=handler.growth_db, pld_config=handler.pld_config,
        bounds=handler.bounds, target_mapper=handler.target_mapper,
    )
    await again.load_calibration()
    return again


async def test_with_nothing_saved_the_settings_value_stands(handler):  # noqa: F811
    assert (await restarted(handler)).readout().center_mask_pos == 100.0


async def test_set_center_mask_pos_survives_a_restart(handler):  # noqa: F811
    await handler.set_center_mask_pos(CenterMaskPos(position=97.219))
    assert (await restarted(handler)).readout().center_mask_pos == 97.219


async def test_an_applied_auto_align_survives_a_restart(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=99.0))
    result = await run_task(handler, AutoAlignMaskCenter())
    assert result["ok"] is True, result
    assert (await restarted(handler)).readout().center_mask_pos == pytest.approx(result["center"])


async def test_a_report_only_auto_align_saves_nothing(handler):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=99.0))
    await run_task(handler, AutoAlignMaskCenter(apply=False))
    assert await handler.growth_db.get_calibration("center_mask_pos") is None


async def test_the_manual_calibrations_are_saved_too(handler):  # noqa: F811
    await handler.begin_align_center_mask(None)
    await handler.confirm_center_mask(ConfirmCenterMask(position=98.5))
    assert await handler.growth_db.get_calibration("center_mask_pos") == 98.5

    await handler.begin_check_mask_center(None)
    await handler.confirm_mask_center(ConfirmMaskCenter(aligned=False, corrected_position=98.1))
    assert (await restarted(handler)).readout().center_mask_pos == 98.1


# --- the fiducial_role gate: ask for the marker rather than failing ------------------


async def wait_for_pending(handler, kind: str):  # noqa: F811
    for _ in range(300):
        pending = handler.readout().pending_confirmation
        if pending is not None and pending.kind == kind:
            return pending
        await asyncio.sleep(0.01)
    pytest.fail(f"no {kind!r} pending confirmation appeared")


@pytest.fixture
def fast_poll(monkeypatch, handler):  # noqa: F811
    """The gate polls the role every second; a test has no reason to wait that long."""
    original = handler._await_mask_center_role
    monkeypatch.setattr(handler, "_await_mask_center_role",
                        lambda timeout_s: original(timeout_s, poll=0.01))


async def test_an_untagged_marker_is_asked_for_and_the_alignment_continues_once_tagged(handler, fast_poll):  # noqa: F811
    mi = handler.sources["chamber_mi"]
    fiducial = FakeFiducial(mi, true_center=100.0, roles={})
    with_fiducial(handler, fiducial)

    ack = await handler.auto_align_center_mask(AutoAlignMaskCenter())
    pending = await wait_for_pending(handler, "fiducial_role")
    assert MASK_CENTER_ROLE in pending.message
    assert handler.readout().current_task.id == ack.task_id
    assert not any("Set Mask Position" in c for c in mi.calls)  # nothing moves while it waits

    # Every event is a full snapshot: once one shows the gate, none reports it cleared.
    events = []
    while (event := await handler.next_update()) is not None:
        events.append(event)
    gated = [e.pending_confirmation is not None for e in events]
    assert gated[-1] and all(gated[gated.index(True):])
    assert all(e.current_task is not None for e in events)

    fiducial.roles[MASK_CENTER_ROLE] = "m41"  # the operator tags it
    result = await run_task_result(handler)

    assert result["ok"] is True, result
    assert result["center"] == pytest.approx(100.0, abs=0.1)
    assert handler.readout().pending_confirmation is None


async def test_a_role_pointing_at_a_deleted_marker_is_asked_for_too(handler, fast_poll):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=100.0,
                                        roles={MASK_CENTER_ROLE: "gone"}))

    await handler.auto_align_center_mask(AutoAlignMaskCenter())
    pending = await wait_for_pending(handler, "fiducial_role")

    assert "'gone'" in pending.message


async def test_confirming_the_role_prompt_cancels_the_alignment(handler, fast_poll):  # noqa: F811
    from lumi.contracts.payloads.experiment import ConfirmProceed

    mi = handler.sources["chamber_mi"]
    with_fiducial(handler, FakeFiducial(mi, true_center=100.0, roles={}))

    await handler.auto_align_center_mask(AutoAlignMaskCenter())
    pending = await wait_for_pending(handler, "fiducial_role")
    await handler.confirm(ConfirmProceed(confirmation_id=pending.id))
    result = await run_task_result(handler)

    assert result["ok"] is False
    assert "cancelled" in result["error"]
    assert not any("Set Mask Position" in c for c in mi.calls)
    assert handler.pld_config.center_mask_pos == 100.0


async def test_the_role_prompt_gives_up_after_its_timeout_and_clears_itself(handler, fast_poll):  # noqa: F811
    with_fiducial(handler, FakeFiducial(handler.sources["chamber_mi"], true_center=100.0, roles={}))

    await handler.auto_align_center_mask(AutoAlignMaskCenter(role_wait_timeout_s=0.05))
    result = await run_task_result(handler)

    assert result["ok"] is False
    assert "gave up" in result["error"]
    assert handler.readout().pending_confirmation is None
