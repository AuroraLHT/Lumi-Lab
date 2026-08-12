#!/usr/bin/env python
"""Drive the simulated chamber through a whole growth, so you can watch the UI move.

Run the stack first, then this alongside it:

    scripts/start_simulation.sh --chamber-speed 30
    uv run scripts/demo_growth.py --speed 30

It talks to the chamber node directly over AMQP -- the same bus the API bridge is
reading -- and walks through heat-up, gas, target select, preablation, deposition and
cool-down, printing the chamber log as it goes. Every number it prints is a number the
frontend is being pushed at the same moment, so the console is the ground truth to
check the panels against.

`--speed` must match the `--chamber-speed` the node was started with. It does not
change the node's behaviour; it is how this script sizes its timeouts and predicts how
long the run will take. It also measures the real speed-up during the deposition and
tells you if the two disagree.

Needs `nodes/pascal.py --src sim`. Against `--src test` the log is a recording of an
idle chamber and none of this will move; the script checks and says so.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid

from aio_pika import ExchangeType, connect_robust

import lumi.pascal.command as pcmd
from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.payloads.chamber import MICommands
from lumi.experiment.manager import _truthy
from lumi.generated.clients.chamber import ChamberClient

# Chamber-log columns worth watching, and how to print them. The names are PASCAL's.
WATCH = [
    ("HT Temp set", "Tset", "{:>5.0f}"),
    ("HT Temp moni", "Tmoni", "{:>5.0f}"),
    ("HT set", "amps", "{:>5.1f}"),
    ("Vac Pres Main", "P/Torr", "{:>9.2e}"),
    ("MFC1 moni", "sccm", "{:>5.2f}"),
    ("TG No.", "TG", "{:>3.0f}"),
    ("Mask1", "mask", "{:>6.1f}"),
    ("LaserPuls", "pulses", "{:>7.0f}"),
    ("LaserHz", "Hz", "{:>4.0f}"),
]
FLAGS = [("Sample Shutter", "shut"), ("Motor free", "motor"), ("Gate shutter", "gate")]


def phase(title: str) -> None:
    print(f"\n\033[1m=== {title} \033[0m".ljust(88, "="), flush=True)


def note(message: str) -> None:
    print(f"    {message}", flush=True)


class LogPrinter:
    """Polls the chamber log and prints a line per sample, in the background."""

    def __init__(self, chamber: ChamberClient, interval: float) -> None:
        self.chamber = chamber
        self.interval = interval
        self.latest: dict = {}
        self._task: asyncio.Task | None = None
        self._header_every = 20
        self._lines = 0

    async def _run(self) -> None:
        while True:
            try:
                batch = await self.chamber.log.log()
                if batch.entries:
                    self.latest = batch.entries[-1].values
                    self._print(self.latest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a dropped sample must not kill the demo
                print(f"    (log read failed: {type(exc).__name__}: {exc})", flush=True)
            await asyncio.sleep(self.interval)

    def _print(self, values: dict) -> None:
        if self._lines % self._header_every == 0:
            head = "    " + " ".join(f"{label:>{max(len(label), 5)}}" for _, label, _ in WATCH)
            head += "  " + " ".join(f"{label:>5}" for _, label in FLAGS)
            print(f"\033[2m{head}\033[0m", flush=True)
        cells = []
        for column, label, fmt in WATCH:
            try:
                cells.append(fmt.format(float(values[column])))
            except (KeyError, TypeError, ValueError):
                cells.append("    ?")
        for column, _label in FLAGS:
            cells.append(f"{'ON' if _truthy(values.get(column)) else '.':>5}")
        print("    " + " ".join(cells), flush=True)
        self._lines += 1

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def sample(self) -> dict:
        batch = await self.chamber.log.log()
        if batch.entries:
            self.latest = batch.entries[-1].values
        return self.latest


class Chamber:
    """Submits MI scripts and waits for the *chamber log* to show the effect.

    Waiting on the log rather than on `lumi.experiment.mi.MiCommandRunner`'s completion
    update, for two reasons. It is the same signal the frontend panels are reading, so
    what this prints is what they should be showing. And it is the only thing that works
    uniformly: `Temperature Set` returns as soon as the setpoint is accepted -- a
    160->750 ramp is half an hour, far past the 30s MI timeout -- so the substrate
    arriving has to be polled for anyway.
    """

    def __init__(self, chamber: ChamberClient, printer: LogPrinter) -> None:
        self.chamber = chamber
        self.printer = printer

    async def send(self, *commands) -> None:
        script = "".join(c.to_text() for c in commands)
        await self.chamber.mi_mode.register_commands(
            MICommands(commands=script, commands_uuid=uuid.uuid4().hex)
        )

    async def until(self, predicate, timeout: float, what: str) -> dict:
        """Poll the log until `predicate(values)` holds."""
        deadline = time.monotonic() + timeout
        values: dict = {}
        while time.monotonic() < deadline:
            values = await self.printer.sample()
            try:
                if values and predicate(values):
                    return values
            except (KeyError, TypeError, ValueError):
                pass
            await asyncio.sleep(0.2)
        raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


def target_number(slot: str) -> int:
    """Carousel slot letter -> the `TG No.` the log will show.

    `settings.experiment.target_mapper` maps the slot to a `TG<n>name` field in
    PLDconfig.ini, which is the same mapping the chamber uses.
    """
    field = settings.experiment.target_mapper.get(slot)
    if field is None:
        raise SystemExit(f"unknown target slot {slot!r}; "
                         f"have {sorted(settings.experiment.target_mapper)}")
    return int(str(field).removeprefix("TG").removesuffix("name"))


def as_float(values: dict, column: str, default: float = 0.0) -> float:
    try:
        return float(values[column])
    except (KeyError, TypeError, ValueError):
        return default


async def check_source(chamber: ChamberClient) -> None:
    state = await chamber.log.get_state()
    batch = await chamber.log.log()
    values = batch.entries[-1].values if batch.entries else {}
    if values and not _truthy(values.get("Motor free")):
        print(
            "\n\033[33mwarning:\033[0m 'Motor free' is not set in the chamber log.\n"
            f"    log source: {state.log_file}\n"
            "    That is what the recorded log looks like -- the node is probably on\n"
            "    --src test. Restart it with --src sim or none of this will move.\n",
            flush=True,
        )


async def growth(chamber: ChamberClient, mi: Chamber, printer: LogPrinter, args) -> None:
    scale = max(args.speed, 1e-6)

    def budget(sim_seconds: float) -> float:
        """Wall-clock allowance for something that takes `sim_seconds` of chamber time."""
        return sim_seconds / scale * 4.0 + 20.0

    phase("1. baseline")
    note("watch: everything idle -- motor ON (free), shut off, no pulses")
    await printer.sample()
    printer._print(printer.latest)

    phase("2. heater on, PID engaged, ramp to temperature")
    ramp_seconds = abs(args.temperature - 160.0) / args.ramp_rate * 60.0
    note(f"watch: Tset climbing at {args.ramp_rate:g} degC/min, Tmoni following it, amps rising")
    note(f"about {ramp_seconds:.0f}s of chamber time -> ~{ramp_seconds / scale:.0f}s here")
    await mi.send(
        pcmd.HeatingLaserLock(locked=False, nowait=False),
        pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False),
        pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID),
        pcmd.TemperatureRamp(args.ramp_rate, state=pcmd.PascalState("ON")),
        pcmd.TemperatureSet(args.temperature, nowait=False),
    )
    values = await mi.until(
        lambda v: abs(as_float(v, "HT Temp moni") - args.temperature) <= 5.0,
        budget(ramp_seconds), f"the substrate to reach {args.temperature:.0f} degC",
    )
    note(f"arrived at {as_float(values, 'HT Temp moni'):.0f} degC")

    phase("3. process gas")
    note("watch: sccm rising, then P/Torr following it up")
    await mi.send(
        pcmd.PressureControl(state=pcmd.PascalState("ON")),
        pcmd.SetPressure(args.pressure * 1e-3),
    )
    target_torr = args.pressure * 1e-3
    await mi.until(lambda v: as_float(v, "Vac Pres Main") >= target_torr * 0.9,
                   budget(60), f"the chamber to reach {args.pressure:g} mTorr")
    note(f"settled at {as_float(await printer.sample(), 'Vac Pres Main'):.2e} Torr")

    phase(f"4. select target {args.target}")
    note("watch: motor drops to '.' while the carousel turns, then TG changes")
    note("      (TG only changes on arrival -- it is the target under the plume)")
    # Wait for `TG No.` to actually become the requested slot. "Motor free" alone is
    # true again the instant before the command lands, so it returns immediately and
    # reports whatever target was already there.
    wanted = target_number(args.target)
    await mi.send(pcmd.SelectTarget(args.target, nowait=False), pcmd.TargetRotationMode(mode="AUTO"))
    await mi.until(lambda v: as_float(v, "TG No.") == wanted and _truthy(v.get("Motor free")),
                   budget(30), f"the carousel to arrive at TG {wanted}")
    note(f"TG No. {as_float(printer.latest, 'TG No.'):.0f} "
         f"at {as_float(printer.latest, 'TG Rotate'):.1f} deg")

    if args.preablation_pulses:
        phase("5. preablation (shutter closed, mask blocking)")
        pre_seconds = args.preablation_pulses / args.rate
        note("watch: pulses climbing with shut '.' -- the sample is not being coated")
        note(f"about {pre_seconds:.0f}s of chamber time -> ~{pre_seconds / scale:.0f}s here")
        before = as_float(await printer.sample(), "LaserPuls")
        await mi.send(
            pcmd.SampleShutter(pcmd.PascalState("OFF")),
            pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=args.mask_block, sync=False, nowait=False),
            pcmd.TriggerLaser(num_pulse=args.preablation_pulses, frequency=args.rate, sync=False, nowait=False),
        )
        await mi.until(lambda v: as_float(v, "LaserPuls") >= before + args.preablation_pulses,
                       budget(pre_seconds + args.mask_block / 10.0), "the preablation train")

    phase("6. deposition (shutter open)")
    depo_seconds = args.pulses / args.rate
    note("watch: shut ON, gate ON, Hz showing the rep rate, pulses climbing")
    note(f"about {depo_seconds:.0f}s of chamber time -> ~{depo_seconds / scale:.0f}s here")
    before = as_float(await printer.sample(), "LaserPuls")
    started = time.monotonic()
    await mi.send(
        pcmd.SampleShutter(pcmd.PascalState("ON")),
        pcmd.TriggerLaser(num_pulse=args.pulses, frequency=args.rate, sync=False, nowait=False),
    )
    values = await mi.until(lambda v: as_float(v, "LaserPuls") >= before + args.pulses,
                            budget(depo_seconds), "the deposition train")
    wall = time.monotonic() - started
    await mi.send(pcmd.SampleShutter(pcmd.PascalState("OFF")))

    delivered = as_float(values, "LaserPuls") - before
    measured = depo_seconds / wall if wall > 0 else float("inf")
    note(f"delivered {delivered:.0f} pulses in {wall:.1f}s wall = {measured:.0f}x real time")
    if abs(measured - scale) > max(0.5 * scale, 3.0):
        note(f"note: the node looks like it is running at ~{measured:.0f}x, not the "
             f"--speed {scale:g} you passed here -- pass the same value you gave --chamber-speed.")

    if not args.skip_cooldown:
        phase("7. cool down")
        note("watch: Tset falling back to 160, amps dropping, sccm going to zero")
        await mi.send(
            pcmd.TemperatureSet(160.0, nowait=False),
            pcmd.PressureControl(state=pcmd.PascalState("OFF")),
            pcmd.SetMFC1Flow(0.0),
        )
        cool_seconds = abs(args.temperature - 160.0) / args.ramp_rate * 60.0
        await mi.until(lambda v: as_float(v, "HT Temp set") <= 165.0,
                       budget(cool_seconds), "the setpoint to come back down")
        await mi.send(pcmd.HeatingLaser(state=pcmd.PascalState("OFF"), nowait=False))


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo_growth",
        description="Drive the simulated chamber through a growth so the UI has something to show.",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="the --chamber-speed the node was started with (sizes timeouts)")
    parser.add_argument("--temperature", type=float, default=700.0, help="degC")
    parser.add_argument("--ramp-rate", type=float, default=20.0, help="degC/min")
    parser.add_argument("--pressure", type=float, default=20.0, help="mTorr")
    parser.add_argument("--target", default="C", help="carousel slot A-F, Clear, Monitor")
    parser.add_argument("--pulses", type=int, default=600)
    parser.add_argument("--rate", type=float, default=10.0, help="laser Hz")
    parser.add_argument("--preablation-pulses", type=int, default=300, help="0 to skip")
    parser.add_argument("--mask-block", type=float, default=75.0, help="mm")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between printed samples")
    parser.add_argument("--skip-cooldown", action="store_true")
    parser.add_argument("--quick", action="store_true",
                        help="short preset: 300 degC, fast ramp, few pulses")
    args = parser.parse_args()
    if args.quick:
        args.temperature, args.ramp_rate = 300.0, 120.0
        args.pulses, args.preablation_pulses = 120, 60
    return args


async def main() -> int:
    args = cli()
    url = f"amqp://{args.user}:{args.password}@{args.host}:5672/"
    try:
        connection = await connect_robust(url, timeout=5.0)
    except Exception as exc:
        print(f"cannot reach the broker at {args.host}:5672 ({type(exc).__name__}: {exc})\n"
              f"start the stack first: scripts/start_simulation.sh", file=sys.stderr)
        return 1

    # The connection is closed in this outer `finally`, *after* the ChamberClient
    # context manager has unsubscribed. Closing it from inside leaves __aexit__
    # unsubscribing over a dead connection, which hangs instead of exiting.
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            CHAMBER.exchange, ExchangeType(CHAMBER.exchange_type), durable=True
        )

        async with ChamberClient.connect(channel, exchange, timeout=15.0) as chamber:
            try:
                await chamber.log.log()
            except Exception as exc:
                print(f"the chamber node is not answering ({type(exc).__name__}: {exc})\n"
                      f"is nodes/pascal.py running?", file=sys.stderr)
                return 1

            await check_source(chamber)
            printer = LogPrinter(chamber, args.interval)
            await printer.start()
            mi = Chamber(chamber, printer)
            started = time.monotonic()
            try:
                await growth(chamber, mi, printer, args)
                phase(f"done in {time.monotonic() - started:.0f}s")
                note("the frontend should have tracked every one of those columns live")
                return 0
            except (TimeoutError, asyncio.TimeoutError) as exc:
                phase("timed out")
                note(str(exc))
                note("if the node is running slower than --speed says, pass the matching value")
                return 1
            finally:
                await printer.stop()
    finally:
        await connection.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
