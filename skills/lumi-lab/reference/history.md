# Answering questions from the history

## Which tool for which question

| question | start with |
| --- | --- |
| What have we grown on substrate X / in project Y? | `list_samples`, `list_experiments` (filter by substrate or project) |
| What is on this sample, bottom to top? | `get_sample` (the derived layer stack) |
| What exactly happened to it, in order, and who did it? | `sample_history` |
| What conditions gave the best result for metric M? | `list_measurements` (measurements joined with growth conditions) |
| What did RHEED look like during that growth? | `get_record`, then `storage.archive.recording_frame_jpeg` / `recording_integration` |
| What did the sample and chamber look like at each stage? | `list_snapshots` (by sample), then `snapshot_jpeg` |
| What was the chamber doing at the time? | `storage.archive.recording_log`, or `chamber.log.log_window` outside a recording |

## Which data to trust

- Retired rows are hidden from listings because someone marked them wrong. Do not bring
  them back into an answer.
- A dry-run growth is journaled as steps but deposited nothing; the step says
  `is_dryrun`. Its recorded temperature and pressure are the chamber's idle state
  (~160 °C, ~1e-8 Torr), not the proposed conditions, so never compare it with a real
  growth.
- Older growths are less reliable than recent ones. The setup drifts over time: laser
  spot size, heater block, how the substrate is clamped, who operated. When comparing
  growths far apart in time, say so.

## Comparing growths

- Each metric measures a different aspect of a growth. Compare metric by metric rather
  than folding them into one score.
- For most epitaxial growths, a better film shows in RHEED as:
  - a single periodicity (no second set of streaks or spots from another phase or
    domain),
  - bright features,
  - sharp features: narrow both across (horizontal) and along (vertical) the streak.
- Always look at the raw RHEED frames as well (`recording_frame_jpeg`), not only the
  numbers, to see the difference for yourself.
- Give the sample and experiment ids you used, so a person can check the answer.
