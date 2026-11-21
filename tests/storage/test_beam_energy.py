"""The RHEED beam energy a recording is made at: asked for, stored, read back.

With pld-service's gun on, the recording keeps its HV readback; otherwise whatever it
was told -- `start_recording`'s `rheed_energy_kev`, else the lab's `rheed.energy_kev` --
and `rheed_energy_source` says which. Files from before this existed have none, and
read back as the default, flagged as such.

T0: no broker; the real Recorder writes every file.
"""

from __future__ import annotations

import h5py
import numpy as np
import pytest

from lumi.contracts.payloads.experiment import StartStorage
from lumi.contracts.payloads.storage import ListRecordings, RecordingName, StorageRequest
from lumi.storage.archive import RecordingArchive
from lumi.contracts.payloads.pld import PldChamberState, PldRheed
from lumi.storage.handlers import ArchiveHandler, StorageHandler, beam_energy, default_energy_kev
from lumi.storage.record import Recorder, RecorderConfig


def write(root, name, attrs=None) -> None:
    rec = Recorder(RecorderConfig(
        project_name=name, root_folder=str(root), frame_dim=(8, 8),
        frame_meta_columns=["time_stamp", "time"], log_columns=None,
        pattern_dim=None, detection_meta_columns=None, detector_classes=None,
        classifier_classes=None, initial_size=10,
        save_frame=True, save_log=False, save_ai=False, save_integration=False,
        attrs=attrs or {},
    ))
    assert rec.create_datasets()["succ"]
    rec.save_frame(np.zeros((8, 8), dtype=np.uint16), {"time_stamp": "t", "time": 1.0})
    rec.close_h5()


def test_the_lab_default_is_25_kev():
    assert default_energy_kev() == 25.0


async def test_a_recording_keeps_the_energy_it_was_made_at(tmp_path):
    write(tmp_path, "new", attrs={"rheed_energy_kev": 30.0})
    with h5py.File(tmp_path / "new.hdf5", "r") as f:
        assert f.attrs["rheed_energy_kev"] == 30.0
    info = await ArchiveHandler(RecordingArchive(tmp_path)).recording_info(RecordingName(name="new"))
    assert (info.rheed_energy_kev, info.rheed_energy_recorded) == (30.0, True)


async def test_an_older_recording_reads_back_as_the_default_and_says_so(tmp_path):
    write(tmp_path, "old")  # no attrs: how every file before this was written
    archive = RecordingArchive(tmp_path, default_energy_kev=22.0)
    info = await ArchiveHandler(archive).recording_info(RecordingName(name="old"))
    assert (info.rheed_energy_kev, info.rheed_energy_recorded) == (22.0, False)
    listed = await ArchiveHandler(archive).list_recordings(ListRecordings())
    assert listed.recordings[0].rheed_energy_kev == 22.0


@pytest.mark.parametrize("asked, stored, source", [(None, 25.0, "default"), (18.5, 18.5, "request")])
async def test_start_recording_stores_what_it_is_asked_or_the_default(tmp_path, asked, stored, source):
    handler = StorageHandler(sources={}, root_folder=str(tmp_path))
    # Nothing saved and no pld source, so this is only the file's own metadata.
    config = await handler._build_config(StorageRequest(project_name="x", rheed_energy_kev=asked))
    assert config["attrs"] == {"rheed_energy_kev": stored, "rheed_energy_source": source}


def gun(**rheed):
    return PldChamberState(rheed=PldRheed(present=True, **rheed))


@pytest.mark.parametrize("asked, state, expect", [
    # The gun on: its HV readback, whatever was asked; a different ask is noted.
    (None, gun(hv_on=True, hv_readback_kv=24.9), {"rheed_energy_kev": 24.9, "rheed_energy_source": "gun"}),
    (25.0, gun(hv_on=True, hv_readback_kv=24.9), {"rheed_energy_kev": 24.9, "rheed_energy_source": "gun"}),
    (15.0, gun(hv_on=True, hv_readback_kv=24.9), {"rheed_energy_kev": 24.9, "rheed_energy_source": "gun",
                                                  "rheed_energy_warning": "asked for 15 keV but the gun's HV "
                                                  "read 24.9 kV at the start; the gun's is kept"}),
    # The gun off: what was asked or the default, with the gun noted as off.
    (None, gun(hv_on=False, hv_readback_kv=0.0), {"rheed_energy_kev": 25.0, "rheed_energy_source": "default",
                                                  "rheed_energy_warning": "the RHEED gun's HV was off at the start"}),
    (20.0, gun(hv_on=False), {"rheed_energy_kev": 20.0, "rheed_energy_source": "request",
                              "rheed_energy_warning": "the RHEED gun's HV was off at the start"}),
    # No RHEED link (the legacy driver's chamber, a pld-service without one): no note.
    (None, PldChamberState(), {"rheed_energy_kev": 25.0, "rheed_energy_source": "default"}),
])
def test_the_beam_energy_comes_from_the_gun_when_it_is_on(asked, state, expect):
    assert beam_energy(asked, state) == expect


def test_the_experiment_node_passes_the_energy_through():
    assert StartStorage(project_name="p").rheed_energy_kev is None
    assert StartStorage(project_name="p", rheed_energy_kev=25).rheed_energy_kev == 25.0
    with pytest.raises(ValueError):
        StorageRequest(project_name="p", rheed_energy_kev=0)
