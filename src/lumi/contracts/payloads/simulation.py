"""RHEED simulation payloads: a crystal, how it is cut and turned, and the optics.

These models are the simulator's own input, not a wire copy of it: `lumi.rheedsim`
takes a `RheedSimRequest` from a notebook exactly as the node takes one off the bus.

Conventions, once:

- Lengths in the crystal are angstrom, on the screen millimetre, on the image pixel.
- `normal` is the surface plane as Miller indices (hkl); `azimuth` is the direction
  the beam travels along the surface, as a direct-lattice [uvw] lying in that plane.
  Both are in the structure's own (conventional) cell.
- Lab frame: z is the outward surface normal, x the beam's in-plane direction. The
  beam comes in `incidence_deg` below the surface plane and the screen stands `camera
  length` down the x axis, square to it.
- The image origin is the point on the screen level with the surface, straight down
  the beam: the middle of the shadow edge. The specular spot sits above it by
  L*tan(incidence), the direct beam the same distance below.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import ServerStateBase

Index3 = Annotated[list[int], Field(min_length=3, max_length=3)]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+()-]{0,63}$")


# --- structures ---------------------------------------------------------------------


class Lattice(BaseModel):
    a: float = Field(gt=0, le=200)
    b: float = Field(gt=0, le=200)
    c: float = Field(gt=0, le=200)
    alpha: float = Field(default=90.0, gt=0, lt=180)
    beta: float = Field(default=90.0, gt=0, lt=180)
    gamma: float = Field(default=90.0, gt=0, lt=180)


class AtomSite(BaseModel):
    element: str = Field(min_length=1, max_length=3)
    #: Fractional coordinates in the cell.
    x: float
    y: float
    z: float
    occupancy: float = Field(default=1.0, ge=0, le=1)
    #: Isotropic displacement B = 8 pi^2 U_iso, in A^2. None takes the morphology's
    #: `debye_waller_b`.
    b_iso: float | None = Field(default=None, ge=0, le=50)
    label: str | None = None


class ManualStructure(BaseModel):
    """A cell typed in by hand: the lattice, a space group and its asymmetric unit.

    Give the sites of the asymmetric unit and the space group expands them; with P1
    (the default) the sites are taken as the whole cell -- which is how to describe a
    strained or hypothetical cell no space group fits.
    """

    lattice: Lattice
    space_group: str = "P1"
    sites: list[AtomSite] = Field(min_length=1, max_length=2000)


class StructureSpec(BaseModel):
    """Exactly one of: a structure by name (built in, or saved with save_structure),
    a CIF's text, or a manual cell."""

    name: str | None = None
    cif: str | None = Field(default=None, max_length=4_000_000)
    manual: ManualStructure | None = None

    @model_validator(mode="after")
    def _one(self) -> StructureSpec:
        given = [k for k in ("name", "cif", "manual") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(f"give exactly one of name, cif or manual (got {given or 'none'})")
        return self


class StructureSummary(BaseModel):
    name: str
    formula: str
    source: Literal["builtin", "saved", "inline"]
    space_group: str
    lattice: Lattice
    #: Atoms in the conventional cell, after symmetry expansion.
    n_atoms: int
    description: str = ""


class StructureList(BaseModel):
    structures: list[StructureSummary] = []


class StructureName(BaseModel):
    name: str


class StructureInfo(StructureSummary):
    #: Every site of the conventional cell (symmetry already applied).
    sites: list[AtomSite] = []


class SaveStructure(BaseModel):
    """Keep a structure under a name, so a simulation can refer to it by that name."""

    name: str
    description: str = Field(default="", max_length=2000)
    cif: str | None = Field(default=None, max_length=4_000_000)
    manual: ManualStructure | None = None
    overwrite: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("name: 1-64 of letters, digits and _ . + - ( ), not starting "
                             "with punctuation")
        return v

    @model_validator(mode="after")
    def _one(self) -> SaveStructure:
        if (self.cif is None) == (self.manual is None):
            raise ValueError("give exactly one of cif or manual")
        return self


# --- the scene ----------------------------------------------------------------------


class Reconstruction(BaseModel):
    """A surface superstructure, as its mesh in units of the bulk surface mesh.

    `matrix` rows are the supercell's vectors in terms of the 1x1 mesh (a1, a2):
    [[2,0],[0,1]] is 2x1, [[1,1],[-1,1]] is c(2x2) (its primitive cell, sqrt2 x sqrt2
    R45). Two rotational domains are two entries. The reconstructed atoms are not
    known to the simulator, so the fractional-order rods it adds take the specular
    rod's profile scaled by `strength` -- positions are right, intensities are a knob.
    """

    matrix: Annotated[list[Annotated[list[int], Field(min_length=2, max_length=2)]],
                      Field(min_length=2, max_length=2)]
    strength: float = Field(default=0.3, ge=0, le=10)
    label: str | None = None

    @model_validator(mode="after")
    def _det(self) -> Reconstruction:
        (a, b), (c, d) = self.matrix
        if a * d - b * c == 0:
            raise ValueError("the reconstruction matrix is singular")
        if abs(a * d - b * c) > 64:
            raise ValueError("the reconstruction is larger than 64 surface cells")
        return self


class SurfaceSpec(BaseModel):
    #: The surface plane (hkl) of the structure's cell.
    normal: Index3 = [0, 0, 1]
    #: The beam's in-plane direction [uvw]; must satisfy h*u + k*v + l*w = 0.
    azimuth: Index3 = [1, 0, 0]
    #: Turns the sample about the normal from `azimuth`, degrees -- off a zone axis.
    azimuth_offset_deg: float = Field(default=0.0, ge=-360, le=360)
    #: Which atomic plane is on top: an index into the result's `mesh.terminations`
    #: (0 = the first listed), or that plane's composition ("TiO2"). None = the plane
    #: the cell puts highest.
    termination: int | str | None = None
    reconstructions: list[Reconstruction] = Field(default=[], max_length=8)

    @model_validator(mode="after")
    def _indices(self) -> SurfaceSpec:
        if not any(self.normal):
            raise ValueError("normal (hkl) cannot be 0 0 0")
        if not any(self.azimuth):
            raise ValueError("azimuth [uvw] cannot be 0 0 0")
        return self


class BeamSpec(BaseModel):
    energy_kev: float = Field(default=20.0, ge=1, le=100)
    #: Glancing angle between the beam and the surface plane.
    incidence_deg: float = Field(default=3.0, gt=0, le=20)
    #: Angular spread of the beam; blurs every feature by about k * divergence.
    divergence_mrad: float = Field(default=0.3, ge=0, le=20)


class ScreenSpec(BaseModel):
    """Where the phosphor screen is and how the camera sees it.

    `pixel_size_mm` is the size of one image pixel *on the screen*, not on the
    sensor -- the camera's lens and distance are folded into it. The lab camera
    sees the screen upside down, which is `flip_y`.
    """

    camera_length_mm: float = Field(default=300.0, gt=0, le=5000)
    pixel_size_mm: float = Field(default=0.25, gt=0, le=10)
    width_px: int = Field(default=720, ge=16, le=4096)
    height_px: int = Field(default=540, ge=16, le=4096)
    #: The shadow-edge centre on the image. None puts it mid-width, and 10 % of the
    #: height in from whichever edge the screen's horizon is at.
    origin_x_px: float | None = None
    origin_y_px: float | None = None
    #: Camera rotation about the beam axis, degrees, counter-clockwise in the image.
    roll_deg: float = Field(default=0.0, ge=-360, le=360)
    flip_x: bool = False
    flip_y: bool = False


class Morphology(BaseModel):
    """How far the surface is from an ideal infinite flat crystal."""

    #: Lateral coherence length (terrace size). Rods are 2 pi / this wide, which is
    #: what turns spots on a Laue circle into streaks.
    terrace_nm: float = Field(default=50.0, ge=0.5, le=100_000)
    #: Share of the pattern from 3D islands (transmission spots), 0 = flat, 1 = rough.
    islands: float = Field(default=0.0, ge=0, le=1)
    #: Island size; transmission spots are 2 pi / this wide.
    island_nm: float = Field(default=5.0, ge=0.5, le=1000)
    #: Inelastic mean free path; with the grazing angles it sets how deep the beam
    #: sees. ~10 nm is typical of oxides at 10-30 keV.
    mean_free_path_nm: float = Field(default=10.0, ge=0.1, le=1000)
    #: B (A^2) for sites that do not carry their own.
    debye_waller_b: float = Field(default=0.5, ge=0, le=50)
    #: Unit cells below the surface summed; None = deep enough for the mean free path.
    layers: int | None = Field(default=None, ge=1, le=500)


class RenderSpec(BaseModel):
    #: Diffuse glow around the specular beam, relative to the strongest feature.
    background: float = Field(default=0.03, ge=0, le=1)
    #: Point-spread of the screen and camera, in pixels.
    blur_px: float = Field(default=1.0, ge=0, le=50)
    #: Draw the direct beam (under the shadow edge), as a sample holder often lets
    #: through part of it.
    direct_beam: bool = True
    #: Shot-noise level, 0 = none. Noise is Poisson with this many counts at 1.0.
    noise_counts: float = Field(default=0.0, ge=0, le=1e6)
    seed: int | None = None


class RheedSimRequest(BaseModel):
    structure: StructureSpec
    surface: SurfaceSpec = SurfaceSpec()
    beam: BeamSpec = BeamSpec()
    #: None = the lab camera's screen, from settings ([simulation.rheed.screen]).
    screen: ScreenSpec | None = None
    morphology: Morphology = Morphology()
    render: RenderSpec = RenderSpec()


class RheedJpegRequest(RheedSimRequest):
    #: How intensity maps to grey. RHEED spans decades, so sqrt or log read better.
    scale: Literal["linear", "sqrt", "log"] = "sqrt"
    quality: int = Field(default=90, ge=10, le=100)


# --- results ------------------------------------------------------------------------


class RheedSpot(BaseModel):
    """One diffraction feature where the Ewald sphere meets it."""

    #: rod = where the Ewald sphere crosses an integer-order surface rod (on a Laue
    #: circle); fractional = the same for a reconstruction's rod; streak_max = a bright
    #: point along a wide rod's streak, at a bulk Bragg point and off the Laue circle;
    #: bulk = a transmission spot through a 3D island.
    kind: Literal["rod", "fractional", "streak_max", "bulk"]
    #: "1 0", "1/2 0" (surface mesh), or "0 1 5" (bulk hkl: streak_max and bulk).
    label: str
    #: Surface-mesh (m, n) for a rod, bulk (h, k, l) for streak_max and bulk.
    indices: list[float]
    #: The in-plane momentum in the structure's reciprocal cell (h, k, l): what a
    #: rod is usually called, e.g. "10" of a perovskite (001) is (1, 0, 0).
    hkl: list[float]
    x_px: float
    y_px: float
    #: Relative to the strongest listed spot of the same kind.
    intensity: float
    #: Rows of rods across the beam, 0 = the one through the specular spot.
    laue_zone: int | None = None
    in_view: bool = True
    #: Momentum transfer in the lab frame, 1/A.
    q: list[float]


class SurfaceMesh(BaseModel):
    """The surface cell the simulator cut: the rods are its reciprocal lattice."""

    #: In-plane mesh vectors and the stacking vector, as [uvw] of the structure's cell --
    #: fractional in a centred cell, e.g. [1/2, -1/2, 0] for fcc (111).
    a1: list[float]
    a2: list[float]
    a3: list[float]
    a1_length: float
    a2_length: float
    gamma_deg: float
    #: Height of one a3 repeat along the normal, angstrom.
    layer_spacing: float
    #: The distinct atomic planes in one repeat, top-first, by composition.
    terminations: list[str]
    termination: str
    n_layers: int


class RheedSimMeta(BaseModel):
    structure: StructureSummary
    mesh: SurfaceMesh
    #: The screen used, with its defaults filled in.
    screen: ScreenSpec
    wavelength_a: float
    k_inv_a: float
    origin_px: list[float]
    #: Two points on the shadow edge (the horizon), across the image.
    shadow_edge_px: list[list[float]]
    specular_px: list[float]
    direct_beam_px: list[float]
    spots: list[RheedSpot] = []
    warnings: list[str] = []
    elapsed_ms: float = 0.0


class RheedJpegMeta(RheedSimMeta):
    scale: str = "sqrt"


class RheedSimReadout(BaseModel):
    n_builtin: int = 0
    n_saved: int = 0
    structures_path: str | None = None


class RheedSimState(RheedSimReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the simulator's readout.
    pass
