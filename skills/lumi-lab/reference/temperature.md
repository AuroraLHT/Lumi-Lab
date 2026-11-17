# Substrate temperature

## What is measured

- The only temperature the driver reads, sets and records (`get_current_temperature`,
  `to_temperature`, the log, every record) comes from the **back pyrometer**. It is
  fixed to the chamber and looks at the **backside of the SiC heater block**. The
  substrate sits on the front face of the block, so the pyrometer never sees the
  substrate: what substrate is mounted, or its emissivity, does not change the reading.
- The emissivity set on the pyrometer is an **effective emissivity**: the SiC block's own
  together with the absorption of the viewport it looks through. It is not a material
  constant. It has to come from a calibration, and it drifts as the viewport coats.
- A second, portable pyrometer is used **only to calibrate** the back one, never to read
  a mounted sample.

## Emissivity history

| from | back-pyrometer emissivity | growths |
| --- | --- | --- |
| before Oct 2026 | 0.65 | experiments before 23, recordings before 2026-10-22 |
| week of 2026-10-19 | 0.262 | experiment 23 (HZO-YSZ-04) and later |

Every growth record (`pyrometer_emissivity`) and every recording (root attribute
`pyrometer_emissivity`) carries the emissivity it was read at. New ones take it from the
pyrometer; older ones were filled in from this table when growth.db was migrated or the
recording converted to format 2.

The old setting reads far low. Block temperature (reference pyrometer) for an old
reading, from the 2026-10-19 calibration (100 mTorr O2):

| old reading (0.65) | 350 | 400 | 450 | 500 | 550 | 600 | 650 | 700 | 750 | 800 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| real, °C | 420 | 480 | 541 | 603 | 665 | 731 | 794 | 858 | 925 | 990 |

On the new setting the back pyrometer reads 3–7 °C below the reference over 350–998 °C,
so "real" ≈ a new reading. Interpolate between columns.

- **[hard]** Before comparing a temperature across the change, or reusing an old
  growth's temperature as a setpoint, convert it and give both numbers. Old readings
  above ~790 °C map past the driver's 1000 °C limit.
- **[hard]** Do not say the substrate's emissivity affects the reading. It does not.

## Recalibrating

A person does this; the agent cannot change the emissivity setting. See
`procedures/pyrometer-calibration.md`.

Many things change how much heater power a temperature takes, and some also change what
the back pyrometer reads: a dirty viewport, a different block surface, a dying heating
laser, a different clamping method, high pressure (> 1 Torr), and others. No single sign
calls for a recalibration, so:

- **[thumb]** Suggest the two-pyrometer check (`procedures/pyrometer-calibration.md`,
  "Routine check") to the person about twice a month. Before a growth, if the last check
  is more than about two weeks old (or not known), say so and offer it.
- If the two disagree, the person runs the full calibration to find out what changed.
  Do not adjust setpoints to make up for a disagreement in the meantime.
