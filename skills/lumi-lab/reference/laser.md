# Laser pulse energy

What matters for a growth is the pulse energy that reaches the target. The energy meter
reads it at the exit port, and only with the carousel at Clear; the excimer reports its
own (source) energy all the time. The ratio between the two changes slowly (optics,
windows), so it is measured once a day and the excimer is then set from it.

## The daily laser test

Once a day, before the first deposition (`list_laser_tests`: is one marked `today`?):

1. A person sets the excimer to ON.
2. `begin_laser_test`: the driver closes the sample shutter, turns the carousel to Clear
   and runs the excimer in HV mode at **21 kV**. It raises a `laser_test` confirmation.
3. The person either says yes, and you call `fire_laser_test` (100 pulses at 1 Hz, about
   100 s; wait for its task, whose result carries `source_mj`), or fires the laser
   themselves.
   `fire_laser_test` is refused if the excimer is no longer at 21 kV in HV mode, and
   fails if the train fires fewer than 100 pulses (fire it again).
4. The person reads the meter's average, after the train has ended (`confirm_laser_test`
   is refused while it fires). `confirm_laser_test` with `meter_mj`. Pass `source_mj`,
   read off the laser's display, when the task result has a `source_note`, or when the
   person fired by hand and the confirm asks for it. The driver does not take the
   laser's readings as the source energy when they never move or equal its energy
   setpoint: that is the setpoint echoed, not a measurement. A meter reading above 1.1
   times the source is refused as a wrong number.

The driver records the test (source, meter, ratio = meter / source, who fired, who
confirmed) in growth.db, in the step journal and in the recording if one is running.
**[hard]** Do not move the carousel between `begin_laser_test` and `confirm_laser_test`.

## Setting the energy for a deposition

`begin_set_laser_power` with the energy wanted at the meter, mJ. The driver switches the
excimer to energy mode at `wanted / ratio` from today's test; nobody reads the meter. It
is refused when there was no test today, and by pld-service when that source energy is
outside the laser's range (report it; do not pick another energy yourself). Changing the
excimer by hand (`set_laser_energy` / `set_laser_hv`) forgets the setting, so call
`begin_set_laser_power` again before depositing.

Every growth record keeps the energy at the meter (`laser_power`), the excimer energy it
was set to (`laser_source_energy`) and the ratio (`laser_meter_ratio`).

Under `--use-legacy-mi-mode` the excimer cannot be reached: the old check applies there
(`begin_set_laser_power` → the person reads the meter → `confirm_laser_power`).
