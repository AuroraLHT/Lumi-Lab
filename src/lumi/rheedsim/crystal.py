"""A crystal as the simulator needs it: a lattice and every atom of its cell.

Whatever the structure came from -- a CIF, a cell typed in by hand, a built-in -- it
ends here as one conventional cell with symmetry already applied, so nothing
downstream knows or cares about space groups.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from functools import cached_property, lru_cache

import gemmi
import numpy as np

from lumi.contracts.payloads.simulation import AtomSite, Lattice, ManualStructure

#: Anions, which a formula writes last.
_ANIONS = ("C", "N", "O", "F", "P", "S", "Cl", "Se", "Br", "Te", "I", "H")


@dataclass(frozen=True, eq=False)
class Crystal:
    name: str
    lattice: Lattice
    space_group: str
    #: The whole conventional cell, fractional coordinates wrapped into [0, 1).
    sites: tuple[AtomSite, ...]
    source: str = "inline"
    description: str = ""
    #: Identifies the structure's content, for caches.
    key: str = field(default="")

    @cached_property
    def orth(self) -> np.ndarray:
        """Columns are a, b, c in Cartesian angstrom (a along x, b in the xy plane)."""
        L = self.lattice
        al, be, ga = (math.radians(v) for v in (L.alpha, L.beta, L.gamma))
        cz = math.sqrt(max(1 - math.cos(be) ** 2
                           - ((math.cos(al) - math.cos(be) * math.cos(ga)) / math.sin(ga)) ** 2, 0))
        return np.array([
            [L.a, L.b * math.cos(ga), L.c * math.cos(be)],
            [0.0, L.b * math.sin(ga), L.c * (math.cos(al) - math.cos(be) * math.cos(ga)) / math.sin(ga)],
            [0.0, 0.0, L.c * cz],
        ])

    @cached_property
    def reciprocal(self) -> np.ndarray:
        """Columns are a*, b*, c* (with the 2 pi): G = reciprocal @ hkl."""
        return 2 * math.pi * np.linalg.inv(self.orth).T

    @cached_property
    def frac(self) -> np.ndarray:
        return np.array([[s.x, s.y, s.z] for s in self.sites], dtype=float)

    @property
    def formula(self) -> str:
        return formula(self.sites, cells=len(self.translations))

    @cached_property
    def translations(self) -> np.ndarray:
        """Lattice translations within the cell, fractional: (0, 0, 0) and whatever
        centring the atoms have (fcc's three face centres, a supercell's repeats).

        Found from the atoms rather than the space group's name, so a P1 CIF of a
        centred cell still gets its true lattice -- which is what the surface mesh,
        and so every rod's label, is built from.
        """
        return centring(self.orth, self.frac, self.sites)

    def is_lattice_vector(self, v) -> bool:
        v = np.asarray(v, dtype=float)
        return any(np.allclose(v - t, np.rint(v - t), atol=1e-6) for t in self.translations)


#: Cells with more atoms than this are taken as primitive rather than searched.
_CENTRING_MAX_ATOMS = 600


def centring(orth: np.ndarray, frac: np.ndarray, sites) -> np.ndarray:
    zero = np.zeros((1, 3))
    n = len(sites)
    if n < 2 or n > _CENTRING_MAX_ATOMS:
        return zero
    f = np.mod(frac, 1.0)
    kinds = [(s.element, round(s.occupancy, 4), s.b_iso) for s in sites]
    ids: dict = {}
    kind_ids = np.array([ids.setdefault(k, len(ids)) for k in kinds])
    rarest = min(set(kind_ids.tolist()), key=lambda k: (kind_ids == k).sum())
    i0 = int(np.flatnonzero(kind_ids == rarest)[0])
    same = kind_ids[:, None] == kind_ids[None, :]
    found = [np.zeros(3)]
    for j in np.flatnonzero(kind_ids == rarest):
        if j == i0:
            continue
        t = np.mod(f[j] - f[i0], 1.0)
        t[np.isclose(t, 1.0, atol=1e-6)] = 0.0
        if any(np.allclose(t, g, atol=1e-4) for g in found):
            continue
        d = (f + t)[:, None, :] - f[None, :, :]
        d -= np.rint(d)
        dist = np.linalg.norm(d @ orth.T, axis=2)
        if np.all(np.min(np.where(same, dist, np.inf), axis=1) < 0.01):
            found.append(t)
    return np.array(found)


def formula(sites, *, reduced: bool = True, cells: int = 1) -> str:
    """Composition from occupancies: SrTiO3, La0.7Sr0.3MnO3. `cells` divides the
    counts first (a centred cell holds that many primitive ones); `reduced=False`
    keeps them as given (a plane of sapphire is O3, not O)."""
    counts: dict[str, float] = {}
    for s in sites:
        counts[s.element] = counts.get(s.element, 0.0) + s.occupancy / cells
    whole = [round(v) for v in counts.values() if abs(v - round(v)) < 1e-6 and round(v) > 0]
    # Only a formula of whole numbers is reduced: La0.3Sr0.7Al0.65Ta0.35O3 must not
    # become La0.1...O because O3 alone divides by 3.
    div = math.gcd(*whole) if reduced and whole and len(whole) == len(counts) else 1
    order = sorted(counts, key=lambda e: (e in _ANIONS, list(counts).index(e)))
    return "".join(e + _count(counts[e] / div) for e in order if counts[e] > 1e-9)


def _count(v: float) -> str:
    if abs(v - 1) < 1e-6:
        return ""
    if abs(v - round(v)) < 1e-6:
        return str(round(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def element(symbol: str) -> str:
    """The element's canonical symbol, or ValueError: 'sr' -> 'Sr', 'Sr2+' -> 'Sr'."""
    bare = "".join(ch for ch in symbol.strip() if ch.isalpha())[:2]
    el = gemmi.Element(bare)
    if el.atomic_number == 0:
        el = gemmi.Element(bare[:1])
    if el.atomic_number == 0:
        raise ValueError(f"unknown element {symbol!r}")
    return el.name


def space_group(name: str) -> gemmi.SpaceGroup:
    sg = gemmi.find_spacegroup_by_name(name.strip())
    if sg is None:
        raise ValueError(f"unknown space group {name!r}")
    return sg


def expand(sg: gemmi.SpaceGroup, lattice: Lattice, asym: list[AtomSite]) -> tuple[AtomSite, ...]:
    """Apply every operation of the space group to the asymmetric unit.

    Images closer than 0.01 A to one already placed are the same atom (a site on a
    special position), so each lands once whatever the multiplicity.
    """
    orth = Crystal("", lattice, "", ()).orth
    placed: list[AtomSite] = []
    for site in asym:
        el = element(site.element)
        mine: list[np.ndarray] = []
        for op in sg.operations():
            f = np.mod(np.array(op.apply_to_xyz([site.x, site.y, site.z])), 1.0)
            f[np.isclose(f, 1.0, atol=1e-9)] = 0.0
            if any(_close(orth, f, g) for g in mine):
                continue
            mine.append(f)
            placed.append(site.model_copy(update={"element": el, "x": float(f[0]),
                                                  "y": float(f[1]), "z": float(f[2])}))
    return tuple(placed)


def _close(orth: np.ndarray, f: np.ndarray, g: np.ndarray) -> bool:
    d = f - g
    d -= np.round(d)
    return float(np.linalg.norm(orth @ d)) < 0.01


def from_manual(name: str, spec: ManualStructure, *, source: str = "inline",
                description: str = "") -> Crystal:
    sg = space_group(spec.space_group)
    sites = expand(sg, spec.lattice, list(spec.sites))
    return Crystal(name=name, lattice=spec.lattice, space_group=sg.hm, sites=sites,
                   source=source, description=description,
                   key="manual:" + _digest(spec.model_dump_json()))


def from_cif(text: str, *, name: str | None = None, source: str = "inline",
             description: str = "") -> Crystal:
    """The first data block of a CIF with a cell and atom sites."""
    return _from_cif(text, name, source, description)


@lru_cache(maxsize=32)
def _from_cif(text: str, name: str | None, source: str, description: str) -> Crystal:
    try:
        doc = gemmi.cif.read_string(text)
    except Exception as exc:  # gemmi raises plain RuntimeError/ValueError with a line
        raise ValueError(f"could not read the CIF: {exc}") from None
    block = next((b for b in doc if b.find_value("_cell_length_a")), None)
    if block is None:
        raise ValueError("the CIF has no data block with a cell (_cell_length_a)")
    ss = gemmi.make_small_structure_from_block(block)
    if not ss.sites:
        raise ValueError(f"CIF block {block.name!r} has no atom sites")
    cell = ss.cell
    lattice = Lattice(a=cell.a, b=cell.b, c=cell.c, alpha=cell.alpha, beta=cell.beta, gamma=cell.gamma)
    sg = ss.spacegroup or space_group("P1")
    asym = [
        AtomSite(
            element=s.element.name if s.element.atomic_number else element(s.type_symbol or s.label),
            x=s.fract.x, y=s.fract.y, z=s.fract.z,
            occupancy=min(max(s.occ, 0.0), 1.0),
            b_iso=(8 * math.pi ** 2 * s.u_iso) if s.u_iso > 0 else None,
            label=s.label or None,
        )
        for s in ss.sites
    ]
    return Crystal(name=name or block.name, lattice=lattice, space_group=sg.hm,
                   sites=expand(sg, lattice, asym), source=source, description=description,
                   key="cif:" + _digest(text))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
