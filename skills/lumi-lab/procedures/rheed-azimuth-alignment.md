# Aligning the substrate azimuth to the RHEED beam

Turn the sample in-plane until the beam runs along a chosen crystal direction [uvw] of
the (hkl) surface and the pattern is symmetric. Done before the growth, after the
substrate is in and heated. Works for any surface: the simulator gives the rod spacing
to expect for each in-plane direction.

## Who does what

- Agent: `rheed_on` (the gun, pld driver), `rotate_sample_to` / `rotate_sample_by` (the
  stage's in-plane angle, the log's `Substrate` column), `rheed.camera.image`, the
  simulator, the decision.
- Person, by hand: beam position, incidence angle, gain (and the gun itself under
  `--use-legacy-mi-mode`). Stage tilt is not
  motorised. If no specular spot shows, only the person can fix it.

## Steps

1. Substrate registered (`register_substrate` / `current_substrate`) **before** heating,
   so the `start` snapshot really is the start.
2. Ask the person: heat in gas or vacuum (SKILL.md §1). `initiate_heating_laser`, then
   `to_temperature` **420 °C** at the substrate's ramp rate unless the person says
   otherwise (on the current pyrometer setting, `reference/temperature.md`). Poll until
   within 10 °C. **Snapshot: chamber `heated`.**
3. `rheed_on` (SKILL.md §2; under legacy MI mode ask the person), then ask the person
   to bring the beam onto the sample.
   **First, clear the clips.** The sample is held by clips, and at some stage angles one
   sits in the beam path. If `rheed.camera.image` shows no diffraction (only the band
   near the top: shadow edge, holder), rotate the stage (`rotate_sample_by`, about 15°
   steps, one image after each move) until diffraction appears. **Snapshot: RHEED
   `other`** at the first angle where it shows, note = stage angle and what is visible.
   If a full 360° shows nothing, it is not a clip: record that (snapshot `other`, the
   angles tried) and ask the person to set the RHEED gun screws back to the marks they
   made for a well-aligned beam; wait.
   **[hard] Then compare θ and θ+180° before any scan.** Estimate the incidence from how
   far the specular sits below the shadow edge (in the lab geometry, 3° puts it about
   224 px below, roughly linear in the angle; about 166 px ≈ 2.2° was a good beam). A
   sample not quite normal to the rotation axis tilts the other way at +180°.
   `rotate_sample_by(180)`, compare the specular's distance from the shadow edge at both
   angles, and do the scan from the steeper one. The in-plane rods repeat with the
   surface's rotational symmetry, so +180° is an equivalent azimuth whenever the surface
   has 2-, 4- or 6-fold symmetry (for 3-fold, e.g. fcc(111), the ±1 rods swap sides).
4. Predict the pattern with the simulator: `rheed_spots` (leave `screen` out) with the
   substrate's structure, `normal` = [hkl], and `azimuth` = each in-plane high-symmetry
   direction [uvw] (perpendicular to [hkl]) you might be looking along. Note each one's
   first- and second-order rod offsets from the specular, in px, and how often it
   repeats in angle (360° / the surface's rotational symmetry). The observed rod spacing
   then tells which direction you are on. Examples on the lab camera at 25 keV:

   | surface | beam along | first-order rods | second-order rods | repeats every |
   |---|---|---|---|---|
   | YSZ(111) | ⟨1-10⟩ | ±105 px | ±210 px | 60° |
   | YSZ(111) | ⟨11-2⟩ | ±181 px | — | 60° |
   | SrTiO3(001) | ⟨100⟩ | ±84 px | — | 90° |
   | SrTiO3(001) | ⟨110⟩ | ±119 px | — | 90° |

5. Coarse scan: `rotate_sample_to` in 10° steps over at least one full repeat period
   (and at least 120°), one image per step. Record per angle: specular present and how
   bright, side spots and their x offset from the specular. **[thumb]** Keep scanning
   through blank angles (see Wobble): scan the full range, do not stop at the first
   blank.
6. Fine scan around the angle whose rod spacing matches the target direction: 2° steps,
   then 1°, then 0.5°. Approach the final angle from the same side every time
   (backlash). The stage moves in about 0.1–0.2° steps; moves of 0.05° do nothing.
7. Judge symmetry by **rod positions**, not brightness (see Asymmetry). Off-axis, the lit
   spot on each first-order rod slides along the rod (y) as the angle changes, in
   opposite directions on the two sides (about 40–80 px per degree). Fit y against angle
   for left and right; where they cross is the azimuth. On axis the lit first-order spot
   sits about level with the specular (refraction pulls real spots 10–30 px below the
   simulator's).
8. Park there. **Snapshot: RHEED `other`**, note = stage angle (from the log), the
   direction, spot positions, anything odd. Read temperature and pressure back. Show the
   person and let them accept, nudge (±0.25°) or re-tune the beam.

## Pitfalls

- **[hard] Rotate, then image, one call after the other.** `rotate_sample_to` blocks
  until the move ends, but an image fetched in the same parallel batch can be taken
  mid-move.
- **Read the log's `Substrate` after every move** and use that angle in notes and
  snapshots. Larger moves sometimes land 1° past the request (15 → 16); small moves land
  right. Give `rotate_sample_to` angles in 0–360: a request for −0.5° landed at 0.4°.
- **Wobble.** On an irregular piece the specular may only show over part of the turn,
  while the log confirms the stage moved. That is the surface not normal to the rotation
  axis (or the beam walking off the piece), not an azimuth effect. Report it; the
  mounting is the person's call. The equivalent azimuth one period away may then not be
  checkable.
- **Asymmetry is physics when the beam is not in a mirror plane of the surface.** Then
  the ±1 rods can differ several-fold in brightness (YSZ(111) along ⟨1-10⟩: about 4×,
  from the ABC stacking). Do not chase equal brightness by rotating: it trades away
  position symmetry. Check with the simulator whether the direction you want is
  symmetric in brightness by crystal symmetry. A too-shallow incidence also makes the
  sides unequal: on HZO-YSZ-05 the +180° turn (≈1.5° → 2.0°) made both rods bright.
- **The specular can saturate** (4091 on 12 bit). Fine for positions; ask the person to
  lower the gain if brightness comparisons matter.
- Kikuchi-like arcs appear near the axis; they are not rods. Use the predicted x offsets
  to pick the rods out.

## Measuring spots from the frames

`rheed.camera.image` saves each frame as a PNG (path in the tool output). A short script
is enough: subtract a heavy blur (σ ≈ 40 px) as background, a light blur (σ ≈ 2 px) as
signal, then for each predicted rod column (specular x ± first- and second-order offset,
±8 px wide) take the maximum and its y between the shadow edge and the bottom. Compare
left and right y and intensity per angle.

The stage angle that comes out is specific to how that piece was mounted: redo the scan
for every new mounting.
