# Reading RHEED

## Where to look

- Live: `rheed.camera.image` (the picture), `rheed.integrator.cache` (intensity in each
  box the operator drew, over time: these are the oscillations).
- Recorded: `storage.archive.recording_frame_jpeg` for a frame,
  `storage.archive.recording_integration` for the traces.
- Expected: `simulation.rheed_sim.simulate_rheed_jpeg` / `rheed_spots`.

## Patterns (general physics; `TODO(lab)` add examples from this chamber)

| what you see | what it usually means |
| --- | --- |
| Sharp spots on a semicircle (the Laue circle), faint diagonal lines (Kikuchi lines) | Flat, well-ordered crystalline surface. A good substrate. |
| Streaks, perpendicular to the shadow edge | Smooth surface with small terraces or some disorder. Normal during 2D growth. |
| Spots *not* on the Laue circle, arranged on a grid (transmission spots) | 3D islands: the beam passes through them. Growth has gone rough. |
| Rings, or arcs | Polycrystalline or textured film. |
| Extra, fainter streaks between the main ones | A surface reconstruction. They look like the main features and sit at a simple fraction (½, ⅓, …) of the main spacing; if not, suspect a second phase instead. |
| Diffuse, dim, no clear features | Amorphous, contaminated, or the gun/gain is off. Check the gain before concluding anything. |

## Oscillations

- One oscillation of the specular spot is usually one unit cell (or one layer) of
  layer-by-layer growth. Its period in pulses is the pulses per unit cell.
- Oscillations that damp out steadily: moving towards step-flow or roughening. Check the
  pattern to tell which (streaks kept = step-flow, spots appearing = rough).
- A sharp intensity drop over the first pulses that then recovers is normal: the surface
  coverage is changing as the new material arrives.
- Intensity that falls and never recovers: roughening. §4 of SKILL.md: stop and ask.
- `TODO(lab)`: which box the operator usually draws on the specular spot, and how to tell
  from `rheed.integrator.cache` which box is which.

## Using the simulator

- Leave `screen` out: you get the lab camera's own geometry, so the result lines up with
  `rheed.camera.image` and recorded frames.
- Compare **positions**, using `rheed_spots` (the labelled spot list). Do not compare
  brightness: the model is kinematic, so intensities are only qualitative.
- It has no Kikuchi lines, no refraction (real spots near the shadow edge sit a little
  lower than predicted; 10–30 px off is normal), no inelastic background.
  See `docs/RHEED_SIMULATION.md` §13.
- Useful for: which azimuth you are looking along (rotate the sample and match the
  spot spacing), whether an extra streak is a reconstruction, and what spacing a
  film with a different lattice should give.
