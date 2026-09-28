"""Structures in: CIFs, manual cells, built-ins, and the saved ones.

T0: the library and the node's handler called directly, no broker.
"""

from __future__ import annotations

import math

import pydantic
import pytest

pytest.importorskip("gemmi")

from lumi.contracts.payloads.common import Empty  # noqa: E402
from lumi.contracts.payloads.simulation import (  # noqa: E402
    AtomSite,
    Lattice,
    ManualStructure,
    Reconstruction,
    RheedJpegRequest,
    RheedSimRequest,
    SaveStructure,
    StructureName,
    StructureSpec,
)
from lumi.rheedsim import StructureStore, from_cif, from_manual, simulate  # noqa: E402
from lumi.rheedsim.handlers import RheedSimHandler  # noqa: E402

STO_CIF = """data_SrTiO3
_cell_length_a 3.905
_cell_length_b 3.905
_cell_length_c 3.905
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_symmetry_space_group_name_H-M 'P m -3 m'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_U_iso_or_equiv
Sr1 Sr2+ 0 0 0 0.008
Ti1 Ti4+ 0.5 0.5 0.5 0.005
O1 O2- 0.5 0.5 0 0.009
"""

#: Rock salt MgO written out atom by atom in P1: nothing says it is face-centred.
MGO_P1 = ManualStructure(lattice=Lattice(a=4.212, b=4.212, c=4.212), sites=[
    AtomSite(element="Mg", x=x, y=y, z=z) for x, y, z in
    [(0, 0, 0), (0.5, 0.5, 0), (0.5, 0, 0.5), (0, 0.5, 0.5)]
] + [
    AtomSite(element="O", x=x, y=y, z=z) for x, y, z in
    [(0.5, 0.5, 0.5), (0, 0, 0.5), (0, 0.5, 0), (0.5, 0, 0)]
])


def test_a_cif_is_expanded_by_its_space_group():
    c = from_cif(STO_CIF)
    assert c.formula == "SrTiO3" and len(c.sites) == 5 and c.space_group == "P m -3 m"
    ti = next(s for s in c.sites if s.element == "Ti")
    assert ti.b_iso == pytest.approx(8 * math.pi ** 2 * 0.005)


def test_a_cif_and_the_builtin_put_every_spot_in_the_same_place():
    a = simulate(RheedSimRequest(structure=StructureSpec(cif=STO_CIF)), image=False)[0]
    b = simulate(RheedSimRequest(structure=StructureSpec(name="SrTiO3")), image=False)[0]
    # Rod crossings are pure geometry. (Streak maxima are not compared: the CIF carries
    # its own Debye-Waller factors, which reshape the profiles they are found on.)
    def crossings(m):
        return sorted((s.label, s.x_px, s.y_px) for s in m.spots if s.kind == "rod")
    assert crossings(a) == crossings(b)


def test_a_p1_cell_is_found_to_be_centred_from_its_atoms():
    c = from_manual("MgO-P1", MGO_P1)
    assert len(c.translations) == 4
    a = simulate(RheedSimRequest(structure=StructureSpec(manual=MGO_P1)), image=False)[0]
    b = simulate(RheedSimRequest(structure=StructureSpec(name="MgO")), image=False)[0]
    assert a.mesh.a1_length == pytest.approx(b.mesh.a1_length)
    assert {s.label for s in a.spots} == {s.label for s in b.spots}


def test_mixed_occupancy_sites_keep_both_elements():
    c = StructureStore(None).get("La0.7Sr0.3MnO3-pc")
    assert c.formula == "La0.7Sr0.3MnO3"
    assert {s.element for s in c.sites} == {"La", "Sr", "Mn", "O"}


@pytest.mark.parametrize("text, match", [
    ("not a cif at all", "could not read the CIF|no data block"),
    ("data_x\n_cell_length_a 4\n", "no data block with a cell|no atom sites"),
])
def test_a_bad_cif_says_why(text, match):
    with pytest.raises(ValueError, match=match):
        from_cif(text)


def test_an_unknown_element_or_space_group_is_refused():
    with pytest.raises(ValueError, match="unknown element"):
        from_manual("x", ManualStructure(lattice=Lattice(a=4, b=4, c=4),
                                         sites=[AtomSite(element="Qq", x=0, y=0, z=0)]))
    with pytest.raises(ValueError, match="unknown space group"):
        from_manual("x", ManualStructure(lattice=Lattice(a=4, b=4, c=4), space_group="Xyz",
                                         sites=[AtomSite(element="Si", x=0, y=0, z=0)]))


def test_request_validation():
    with pytest.raises(pydantic.ValidationError, match="exactly one"):
        StructureSpec()
    with pytest.raises(pydantic.ValidationError, match="exactly one"):
        StructureSpec(name="SrTiO3", cif=STO_CIF)
    with pytest.raises(pydantic.ValidationError, match="singular"):
        Reconstruction(matrix=[[2, 4], [1, 2]])
    with pytest.raises(pydantic.ValidationError, match="name"):
        SaveStructure(name="../evil", cif=STO_CIF)


# --- the store and the handler -------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    return RheedSimHandler(StructureStore(tmp_path / "structures"))


async def test_saved_structures_are_listed_after_the_builtins(handler):
    await handler.save_structure(SaveStructure(name="my-STO", cif=STO_CIF, description="from a CIF"))
    await handler.save_structure(SaveStructure(name="MgO-by-hand", manual=MGO_P1))
    listed = (await handler.list_structures(Empty())).structures
    assert listed[0].source == "builtin"
    assert [s.name for s in listed if s.source == "saved"] == ["MgO-by-hand", "my-STO"]
    info = await handler.get_structure(StructureName(name="my-STO"))
    assert info.description == "from a CIF" and len(info.sites) == 5
    assert handler.readout().n_saved == 2


async def test_a_saved_structure_can_be_simulated_by_name(handler):
    await handler.save_structure(SaveStructure(name="my-STO", cif=STO_CIF))
    meta = await handler.rheed_spots(RheedSimRequest(structure=StructureSpec(name="my-STO")))
    assert meta.structure.source == "saved" and meta.spots


async def test_saving_refuses_builtin_names_duplicates_and_bad_input(handler, tmp_path):
    with pytest.raises(ValueError, match="built-in"):
        await handler.save_structure(SaveStructure(name="SrTiO3", cif=STO_CIF))
    await handler.save_structure(SaveStructure(name="mine", cif=STO_CIF))
    with pytest.raises(ValueError, match="overwrite"):
        await handler.save_structure(SaveStructure(name="mine", cif=STO_CIF))
    await handler.save_structure(SaveStructure(name="mine", manual=MGO_P1, overwrite=True))
    assert (await handler.get_structure(StructureName(name="mine"))).formula == "MgO"
    with pytest.raises(ValueError):
        await handler.save_structure(SaveStructure(name="broken", cif="garbage"))
    assert not (tmp_path / "structures" / "broken.json").exists()


async def test_deleting(handler):
    await handler.save_structure(SaveStructure(name="gone", cif=STO_CIF))
    await handler.delete_structure(StructureName(name="gone"))
    with pytest.raises(KeyError):
        await handler.get_structure(StructureName(name="gone"))
    with pytest.raises(ValueError, match="built in"):
        await handler.delete_structure(StructureName(name="SrTiO3"))


async def test_a_store_without_a_folder_serves_only_the_builtins():
    h = RheedSimHandler(StructureStore(None))
    assert len((await h.list_structures(Empty())).structures) == h.readout().n_builtin
    with pytest.raises(ValueError, match="no structures folder"):
        await h.save_structure(SaveStructure(name="x", cif=STO_CIF))


async def test_jpeg_and_npy_carry_the_same_spots(handler):
    r = RheedJpegRequest(structure=StructureSpec(name="SrTiO3"), scale="log")
    meta, jpeg = await handler.simulate_rheed_jpeg(r)
    assert jpeg[:2] == b"\xff\xd8" and meta.scale == "log"
    meta2, image = await handler.simulate_rheed(RheedSimRequest(structure=StructureSpec(name="SrTiO3")))
    assert image.shape == (meta2.screen.height_px, meta2.screen.width_px)
    assert [s.label for s in meta.spots] == [s.label for s in meta2.spots]


async def test_a_request_without_a_screen_gets_the_nodes(tmp_path):
    from lumi.contracts.payloads.simulation import ScreenSpec

    lab = ScreenSpec(width_px=320, height_px=240, flip_y=True, origin_x_px=100, origin_y_px=30)
    h = RheedSimHandler(StructureStore(None), screen=lab)
    meta = await h.rheed_spots(RheedSimRequest(structure=StructureSpec(name="SrTiO3")))
    assert (meta.screen.width_px, meta.screen.flip_y, meta.origin_px) == (320, True, [100, 30])
