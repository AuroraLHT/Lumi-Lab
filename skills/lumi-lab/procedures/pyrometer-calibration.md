# Calibrating the back pyrometer

Two stages: first make a portable reference pyrometer read true against a thermocouple,
then use it to set the back pyrometer's effective emissivity in the chamber. A person
does this; the agent can run the heater and write the readings down
(`reference/temperature.md` for why it matters).

## Routine check: the two pyrometers against each other

About twice a month. The reference pyrometer, on its stage-1 settings, at the front
viewport, reads the SiC block against the back pyrometer at a few temperatures, as in
step 8. If they agree, nothing has changed. If they disagree, something has (dirty
viewport, block surface, heating laser, clamping, pressure, ...): run the full
calibration below to find out what.

## Stage 1: reference pyrometer against a thermocouple (outside the chamber)

1. Clamp a p-type thermocouple to one edge of the SiC heater block. Focus the reference
   pyrometer on the block next to the clamp.
2. Put the holder on a hot plate and heat it in air. The frame runs slightly hotter than
   the block, so the block is not losing heat to it and the thermocouple and pyrometer
   see the same temperature. The hot plate reaches about 460 °C in air, so this stage
   covers up to there.
3. Start the reference pyrometer at emissivity and transmission both at maximum (1.1).
   Record pyrometer against thermocouple up to the hot plate's limit, twice at each
   point: window **off** (straight at the block) and window **on** (the viewport window
   it will look through on the chamber held in its line of sight).
4. Window off, set the emissivity until the reading matches the thermocouple: that is
   the block alone. Then window on, set the transmission until it matches again: that
   takes out the window. 2026-10-19: emissivity 0.91, transmission 0.87 (±8 °C).

## Stage 2: back pyrometer against the reference (in the chamber)

5. Mount the heater block with **no sample**, the reference pyrometer at the front
   viewport on the block's front face with the settings from step 4. Gas as in a growth
   (2026-10-19: 100 mTorr O2).
6. With the back pyrometer still on its old emissivity, step the heater over the range
   and record back against reference (2026-10-19: 350–800 °C in 50 °C steps). Keep this
   table: it converts old records to the new scale.
7. Go to the highest temperature growths will use (2026-10-19: 24.09 A heater current,
   reference 998 °C) and set the back pyrometer's emissivity until it reads the same
   (→ 0.262). Match at the top: an emissivity error shows most at high temperature.
8. Step down through the range recording both (998 → 350 °C). They should agree within
   a few degrees over the whole range (2026-10-19: 0–7 °C).
9. Sanity check: the block starts to glow on the chamber camera around 535–550 °C.
10. Write down the date, the new emissivity and from which experiment it applies, and
    add the change to `src/lumi/experiment/pyrometer.py` and the history table in
    `reference/temperature.md`, so old and new records are not mixed.
