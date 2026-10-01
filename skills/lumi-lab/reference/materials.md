# Materials

The carousel slot → material map changes when targets are swapped. Always resolve the
material with `get_target_name_by_id` / `show_available_targets`; never assume a slot
holds a given material.

## Laser

- **Energy** is measured at the exit view port (the `laser_power` confirmation, SKILL.md §2).
- **Fluence** is energy ÷ spot area, in J/cm². The spot area is calibrated from the spot
  shape and rarely changes, so fluence is varied through the energy. Ask the operator for
  the current spot size (or the energy and fluence) to know the setting.
- Higher fluence gives the plasma more kinetic energy and also raises the deposition rate.
- **Repetition rate** is limited by the laser source. The deposition rate scales roughly
  with it.

## Substrates

- Single crystals with a known out-of-plane orientation. Usually 5×5, 10×5 or
  10×10 mm, 0.5 mm thick.
- As loaded, the in-plane direction along the beam is usually known:

  | substrate | beam along |
  | --- | --- |
  | SrTiO3 (001) | [100] |
  | YSZ (111) | [1-10] |
  | Al2O3 (0001) | [10-10] |

- A good as-loaded pattern is mostly spots lying on the zeroth Laue circle
  (`rheed.md`).
- Preparation varies by sample: a high-temperature anneal, or a chemical etch followed by
  one, to improve the surface.
