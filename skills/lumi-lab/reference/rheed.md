# Reading RHEED

## Where to look

- Live: `rheed.camera.image` (the picture), `rheed.integrator.cache` (intensity in each
  box, over time: these are the oscillations). Boxes 0-999 are the operator's (the
  Deck); 1000 and up are the ones you drew (`add_rheed_box`).
- Recorded: `storage.archive.recording_frame_jpeg` for a frame,
  `storage.archive.recording_integration` for the traces.
- Expected: `simulation.rheed_sim.simulate_rheed_jpeg` / `rheed_spots`.

## Patterns

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

### Which box is the specular spot

`rheed.integrator.bboxes` lists the boxes; `rheed.integrator.cache` gives each one's
intensity over time (the last 1000 frames: ask for `max_points`, or `since` a time, to
get less). The operator's boxes carry no role, so work it out (yours carry the
label you gave them, in `add_rheed_box`'s result):

1. **Look.** Take `rheed.camera.image` and find each box on it. The specular spot is
   the one on the mirror line of the pattern: on the zeroth Laue circle, centred
   between the symmetric side streaks, and usually the brightest.
2. **Check against the simulator.** `rheed_spots` for the substrate and azimuth (leave
   `screen` out) returns `specular_px`, where the specular spot lands on the lab camera.
   The box that contains it is the specular box. The real spot can sit 10–30 px off
   (no refraction in the model), and the sample's height moves the whole pattern, so
   match the pattern's spacing too, not that one point alone.
3. If the two disagree, or no box covers the specular spot, say so and ask the person
   rather than reading oscillations off the wrong box.

### Drawing your own boxes

Draw them with `experiment.driver.add_rheed_box` before `start_storage`, with the gun on
and the beam on the sample, so the whole growth is recorded:

1. **Find the spots** as above: `rheed.camera.image` and `rheed_spots`'s `specular_px`
   (and the first-order rods' positions).
2. **Specular** (`label: "specular"`, `stft: true`): centred on the specular spot, a
   little larger than the spot itself (about 1.5x its visible size; 20-40 px on the lab
   camera), so a small drift of the pattern keeps it inside. This is the oscillation the
   growth is read by.
3. **A first-order rod** (`label: "(01) rod"`, optional): the same way, on one side streak.
4. **Background** (`label: "background"`): a dark patch away from every spot and streak,
   above the shadow edge's glow, to tell a beam or gain change from growth.
5. **Check**: take `rheed.camera.image` again and `rheed.integrator.cache` for each box;
   the specular box must read well above the background. Look at the trace for a few
   seconds: a steady value, not one at full scale.

Never cover or replace the operator's boxes: add yours beside them. A box that missed:
`remove_rheed_box` and draw it again, before `start_storage` (removal is refused while
recording, since it cuts the trace). Say which boxes you drew, with their labels.

## Using the simulator

- Leave `screen` out: you get the lab camera's own geometry, so the result lines up with
  `rheed.camera.image` and recorded frames.
- Compare **positions**, using `rheed_spots` (the labelled spot list). Do not compare
  brightness: the model is kinematic, so intensities are only qualitative.
- It has no Kikuchi lines, no refraction (real spots near the shadow edge sit a little
  lower than predicted; 10–30 px off is normal), no inelastic background.
  Details: [RHEED simulation §13](https://github.com/AuroraLHT/Lumi-Lab/blob/main/docs/RHEED_SIMULATION.md#13-what-it-leaves-out).
- Useful for: which azimuth you are looking along (rotate the sample and match the
  spot spacing), whether an extra streak is a reconstruction, and what spacing a
  film with a different lattice should give.
