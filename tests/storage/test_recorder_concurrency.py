"""The recorder's index allocation must be atomic across the writer pool.

`RecorderServer` submits every save into a `ThreadPoolExecutor` sized by
`storage.hdf5_recorder.max_workers` (8), so `Recorder.save_*` runs on eight threads at
once. Each save allocated its index with an unguarded read-modify-write:

    idx = self._idx_frame      # LOAD_ATTR
    self._idx_frame += 1       # BINARY_OP + STORE_ATTR

The GIL can switch between those bytecodes, so two threads could be handed the same
index and the second `ds_frame[idx] = frame` silently overwrote the first. That is
frame loss, not just a bad counter -- reported as §8 in the frontend's BACKEND-NOTES.

T0: no broker, no equipment. Everything here is a real `Recorder` writing a real HDF5
file into tmp_path.
"""

from __future__ import annotations

import sys
import threading

import numpy as np
import pytest

from lumi.storage.record import Recorder, RecorderConfig

FRAME_DIM = (8, 8)


@pytest.fixture
def recorder(tmp_path) -> Recorder:
    rec = Recorder(
        config=RecorderConfig(
            project_name="concurrency",
            root_folder=str(tmp_path),
            frame_dim=FRAME_DIM,
            frame_meta_columns=["time_stamp", "time"],
            log_columns=["Time", "Pressure"],
            pattern_dim=None,
            detection_meta_columns=None,
            detector_classes=None,
            classifier_classes=None,
            # Deliberately smaller than the number of frames written below, so the
            # tests also exercise resize_if_over racing between threads.
            initial_size=16,
            save_frame=True,
            save_log=True,
            save_ai=False,
            save_integration=False,
            # No throttle: the speed limiter would drop most of the writes and hide
            # exactly the contention being tested.
            frame_speed_limit=None,
            force_rewrite=True,
        )
    )
    created = rec.create_datasets()
    assert created["succ"], created
    yield rec
    rec.close_h5()


@pytest.fixture
def hair_trigger_switching():
    """Force the interpreter to switch threads as often as it can.

    The race is a two-bytecode window, so at the default 5ms switch interval it is
    rare enough that a test would pass by luck. This makes an unguarded counter fail
    essentially every run.
    """
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


def _run_concurrently(fn, n_threads: int, per_thread: int) -> None:
    """Start n_threads that each call fn(i) per_thread times, released together."""
    start = threading.Barrier(n_threads)

    def worker() -> None:
        start.wait()
        for i in range(per_thread):
            fn(i)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a writer thread hung"


# --- the allocator itself -------------------------------------------------------


def test_no_two_threads_get_the_same_index(recorder, hair_trigger_switching):
    handed_out: list[int] = []
    guard = threading.Lock()

    def take(_i: int) -> None:
        idx = recorder.next_frame_idx
        with guard:  # only to make the list append itself safe
            handed_out.append(idx)

    _run_concurrently(take, n_threads=8, per_thread=250)

    assert len(handed_out) == 2000
    assert len(set(handed_out)) == 2000, "the same index was handed to two threads"
    assert sorted(handed_out) == list(range(2000)), "the sequence has holes"
    assert recorder._idx_frame == 2000


def test_the_counter_does_not_fall_behind_the_writes(recorder, hair_trigger_switching):
    """A lost increment leaves _idx_frame below the number of allocations, which is
    what made the old counter unusable as a progress signal even ignoring the loss."""
    _run_concurrently(lambda _i: recorder.next_log_idx, n_threads=8, per_thread=250)

    assert recorder._idx_log == 2000


def test_block_allocations_do_not_overlap(recorder, hair_trigger_switching):
    """The integration path writes one contiguous slice per call, so a run of indices
    has to be exclusively its own."""
    runs: list[range] = []
    guard = threading.Lock()

    def take_block(_i: int) -> None:
        base = recorder._next_idx("_idx_integration_bbox", 4)
        with guard:
            runs.append(range(base, base + 4))

    _run_concurrently(take_block, n_threads=8, per_thread=100)

    assert len(runs) == 800
    covered = [i for run in runs for i in run]
    assert len(set(covered)) == len(covered), "two runs overlapped"
    assert sorted(covered) == list(range(3200))


def test_allocation_waits_while_a_write_is_in_progress(recorder):
    """Deterministic proof of mutual exclusion, rather than inferring it from a race
    that happens not to have fired."""
    took_one = threading.Event()

    def take() -> None:
        recorder.next_frame_idx
        took_one.set()

    with recorder._lock_write:
        thread = threading.Thread(target=take)
        thread.start()
        assert not took_one.wait(timeout=0.5), "allocated an index mid-write"

    thread.join(timeout=5)
    assert took_one.is_set(), "never allocated after the lock was released"


# --- the read-only counters ------------------------------------------------------


def test_reading_a_progress_count_does_not_consume_an_index(recorder):
    """`next_frame_idx` is a property with a side effect; anything that reads it to
    display a count burns an index nobody writes to. That is what frames_written is
    for."""
    recorder.next_frame_idx
    recorder.next_frame_idx

    assert recorder.frames_written == 2
    assert recorder.frames_written == 2  # still 2 -- reading it changed nothing
    assert recorder._idx_frame == 2


# --- end to end, against a real HDF5 file ----------------------------------------


def test_concurrent_frames_all_survive(recorder, hair_trigger_switching):
    """The bug that actually costs data: two threads on one index means the second
    write lands on top of the first and a frame is gone from the file."""
    n_threads, per_thread = 8, 40
    total = n_threads * per_thread

    def save(_i: int) -> None:
        # Each frame is filled with its own index, so an overwrite is identifiable:
        # the row's value will not match any missing index.
        idx = recorder.frames_written
        recorder.save_frame(
            np.full(FRAME_DIM, 0, dtype=np.uint16),
            {"time_stamp": str(idx), "time": "0"},
        )

    _run_concurrently(save, n_threads=n_threads, per_thread=per_thread)

    assert recorder.ds_frame.attrs["size"] == total, "a size increment was lost"
    assert recorder.ds_frame_meta.attrs["size"] == total
    assert recorder._idx_frame == total

    # Every row up to `total` must have been written by exactly one call. The meta
    # dataset is written in the same locked section as the frame, so a hole here is a
    # frame that was overwritten before it was ever read back.
    written = recorder.ds_frame_meta[:total]
    assert all(row[0] != b"" for row in written), "an allocated row was never written"


def test_concurrent_logs_all_survive(recorder, hair_trigger_switching):
    n_threads, per_thread = 8, 40
    total = n_threads * per_thread

    def save(_i: int) -> None:
        recorder.save_log({"Time": "0", "Pressure": "1e-6"})

    _run_concurrently(save, n_threads=n_threads, per_thread=total // n_threads)

    assert recorder.ds_log.attrs["size"] == total
    assert recorder._idx_log == total
    assert all(row[0] != b"" for row in recorder.ds_log[:total])
