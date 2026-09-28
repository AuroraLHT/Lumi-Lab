"""Structures the simulator knows without being given a file.

The lab's substrates and films, plus a few textbook references to check the
simulator against. Perovskite films are pseudocubic: that is how their RHEED is
indexed, and it gets the integer-order rods right. The half-order rods of an
orthorhombic film's octahedral tilts need its real cell -- upload the CIF for that.
"""

from __future__ import annotations

from lumi.contracts.payloads.simulation import AtomSite, Lattice, ManualStructure

from .crystal import Crystal, from_manual


def _cubic(a: float) -> Lattice:
    return Lattice(a=a, b=a, c=a)


def _perovskite(a_sites: dict[str, float], b_sites: dict[str, float], a: float) -> ManualStructure:
    """ABO3, Pm-3m: A at the corner, B at the body centre, O on the face centres."""
    sites = [AtomSite(element=el, x=0, y=0, z=0, occupancy=occ) for el, occ in a_sites.items()]
    sites += [AtomSite(element=el, x=0.5, y=0.5, z=0.5, occupancy=occ) for el, occ in b_sites.items()]
    sites.append(AtomSite(element="O", x=0.5, y=0.5, z=0))
    return ManualStructure(lattice=_cubic(a), space_group="Pm-3m", sites=sites)


#: name -> (structure, description)
BUILTINS: dict[str, tuple[ManualStructure, str]] = {
    "SrTiO3": (_perovskite({"Sr": 1}, {"Ti": 1}, 3.905),
               "Strontium titanate, cubic perovskite (Pm-3m), a = 3.905 A."),
    "LaAlO3-pc": (_perovskite({"La": 1}, {"Al": 1}, 3.79),
                  "Lanthanum aluminate, pseudocubic a = 3.79 A (the real cell is R-3c)."),
    "LSAT-pc": (_perovskite({"La": 0.3, "Sr": 0.7}, {"Al": 0.65, "Ta": 0.35}, 3.868),
                "(LaAlO3)0.3(Sr2TaAlO6)0.35, pseudocubic a = 3.868 A."),
    "La0.7Sr0.3MnO3-pc": (_perovskite({"La": 0.7, "Sr": 0.3}, {"Mn": 1}, 3.876),
                          "LSMO, pseudocubic a = 3.876 A (the real cell is R-3c)."),
    "LaFeO3-pc": (_perovskite({"La": 1}, {"Fe": 1}, 3.93),
                  "Lanthanum orthoferrite, pseudocubic a = 3.93 A. The real cell is "
                  "orthorhombic (Pnma); its tilt half-order rods need that CIF."),
    "YSZ": (ManualStructure(lattice=_cubic(5.14), space_group="Fm-3m", sites=[
        AtomSite(element="Zr", x=0, y=0, z=0, occupancy=0.85),
        AtomSite(element="Y", x=0, y=0, z=0, occupancy=0.15),
        AtomSite(element="O", x=0.25, y=0.25, z=0.25, occupancy=0.963),
    ]), "Yttria-stabilised zirconia, fluorite (Fm-3m), a = 5.14 A, ~8 mol% Y2O3."),
    "MgO": (ManualStructure(lattice=_cubic(4.212), space_group="Fm-3m", sites=[
        AtomSite(element="Mg", x=0, y=0, z=0),
        AtomSite(element="O", x=0.5, y=0.5, z=0.5),
    ]), "Magnesium oxide, rock salt (Fm-3m), a = 4.212 A."),
    "Al2O3": (ManualStructure(
        lattice=Lattice(a=4.7589, b=4.7589, c=12.991, gamma=120), space_group="R-3c", sites=[
            AtomSite(element="Al", x=0, y=0, z=0.35216),
            AtomSite(element="O", x=0.30624, y=0, z=0.25),
        ]), "Sapphire, corundum (R-3c, hexagonal axes). c-plane is (0 0 1)."),
    "TiO2-rutile": (ManualStructure(
        lattice=Lattice(a=4.5937, b=4.5937, c=2.9587), space_group="P42/mnm", sites=[
            AtomSite(element="Ti", x=0, y=0, z=0),
            AtomSite(element="O", x=0.3048, y=0.3048, z=0),
        ]), "Rutile (P4_2/mnm), a = 4.594, c = 2.959 A."),
    "Si": (ManualStructure(lattice=_cubic(5.431), space_group="F-43m", sites=[
        AtomSite(element="Si", x=0, y=0, z=0),
        AtomSite(element="Si", x=0.25, y=0.25, z=0.25),
    ]), "Silicon, diamond structure, a = 5.431 A."),
    "GaAs": (ManualStructure(lattice=_cubic(5.6533), space_group="F-43m", sites=[
        AtomSite(element="Ga", x=0, y=0, z=0),
        AtomSite(element="As", x=0.25, y=0.25, z=0.25),
    ]), "Gallium arsenide, zinc blende (F-43m), a = 5.653 A."),
}

_cache: dict[str, Crystal] = {}


def builtin(name: str) -> Crystal | None:
    if name not in BUILTINS:
        return None
    if name not in _cache:
        spec, doc = BUILTINS[name]
        _cache[name] = from_manual(name, spec, source="builtin", description=doc)
    return _cache[name]
