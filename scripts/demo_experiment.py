#!/usr/bin/env python
"""Drive a whole growth through the **experiment node**, the way a notebook would.

    scripts/start_simulation.sh --chamber-speed 30 --with-experiment    # terminal 1
    uv run scripts/demo_experiment.py --speed 30                        # terminal 2

The sibling of scripts/demo_growth.py, one layer up. demo_growth talks to the chamber
node directly and sends raw MI commands -- useful for watching the simulated chamber
move, but it bypasses everything the experiment node exists for. This drives the same
growth through `lumi.experiment.client.ExperimentSession` and
`lumi.experiment.recipes.perform_single_deposition`: project and substrate bookkeeping,
the long-running-task state machine (`current_task` -> `task_result`), the human-gated
steps, storage records, and the provenance row written at the end.

So this is the script that exercises the production client path. What it prints on the
left is the experiment node's own state; what it prints on the right is the chamber log
the frontend is reading at the same moment.

The `[Manual]` steps in the real recipe wait on a human at a terminal (set the pressure,
read the laser power meter). Here they are answered automatically so the run is
unattended -- pass --interactive to answer them yourself instead.

Needs `nodes/experiment.py` running and `nodes/pascal.py --src sim`. It checks both and
says so if either is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid

from lumi.contracts.payloads.experiment import (
    RegisterProject,
    RegisterSubstrate,
)
from aio_pika import ExchangeType

from lumi.config import settings
from lumi.contracts.system import SYSTEM
from lumi.experiment import recipes
from lumi.experiment.client import ExperimentSession
from lumi.generated.clients.system import SystemRegistryClient

# Chamber-log columns worth watching, and how to print them. The names are PASCAL's.
WATCH = [
    ("HT Temp set", "Tset", "{:>5.0f}"),
    ("HT Temp moni", "Tmoni", "{:>5.0f}"),
    ("Vac Pres Main", "P/Torr", "{:>9.2e}"),
    ("TG No.", "TG", "{:>3.0f}"),
    ("Mask1", "mask", "{:>6.1f}"),
    ("LaserPuls", "pulses", "{:>7.0f}"),
    ("LaserHz", "Hz", "{:>4.0f}"),
]


def phase(title: str) -> None:
    print(f"\n\033[1m=== {title} \033[0m".ljust(88, "="), flush=True)


def note(message: str) -> None:
    print(f"  {message}", flush=True)


def warn(message: str) -> None:
    print(f"  \033[33m! {message}\033[0m", flush=True)


def as_float(values: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(values.get(key, default))
    except (TypeError, ValueError):
        return default


class Watcher:
    """Prints the node's task transitions and the chamber log side by side.

    The task half comes from the driver's *update channel* -- the whole point of the
    experiment node being PUBSUB -- rather than from polling, so a `to_temperature`
    that takes half an hour reports the moment it finishes.
    """

    def __init__(self, exp: ExperimentSession, interval: float) -> None:
        self.exp = exp
        self.interval = interval
        self.latest: dict = {}
        self.failed: list[dict] = []
        self._task: asyncio.Task | None = None
        self._seen_task: str | None = None
        self._seen_pending: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    #: Transitions are checked far more often than the table is printed. A task can
    #: start and finish inside one --interval (a to_temperature that is already at
    #: temperature takes a second), and missing it entirely makes the run look like
    #: nothing happened.
    TRANSITION_POLL_S = 0.25

    async def _run(self) -> None:
        header = "  " + " ".join(f"{label:>9}" for _, label, _ in WATCH)
        print(f"\033[2m{header}\033[0m", flush=True)
        next_row = 0.0
        while True:
            self._report_transitions()
            if time.monotonic() >= next_row:
                await self._sample_log()
                if self.latest:
                    cells = " ".join(fmt.format(as_float(self.latest, key)).rjust(9)
                                     for key, _, fmt in WATCH)
                    print(f"  \033[2m{cells}\033[0m", flush=True)
                next_row = time.monotonic() + self.interval
            await asyncio.sleep(self.TRANSITION_POLL_S)

    async def _sample_log(self) -> None:
        try:
            batch = await self.exp.chamber_log.log()
        except Exception:
            return  # the chamber may be busy; the next tick will catch up
        if batch.entries:
            self.latest = batch.entries[-1].values

    def _report_transitions(self) -> None:
        """`exp.current_task` / `exp.pending` are kept current by the session's own
        update subscription -- this only notices when they change."""
        task = self.exp.current_task
        task_id = task.id if task else None
        if task_id != self._seen_task:
            if task is not None:
                print(f"  \033[36m> task {task.kind} started\033[0m", flush=True)
            elif self._seen_task is not None:
                self._report_result()
            self._seen_task = task_id

    def _report_result(self) -> None:
        """A finished task reports its outcome once, and the recipe does not act on it
        -- `_wait_for_task` returns as soon as `current_task` clears, whether the op
        raised or not. So a failed ramp would otherwise scroll past unremarked and the
        deposition would go ahead at the wrong temperature."""
        result = self.exp.last_task_result
        if result is None:
            return
        if result.get("ok"):
            print("  \033[32m> task finished ok\033[0m", flush=True)
        else:
            self.failed.append(result)
            print(f"  \033[31m> TASK FAILED: {result.get('error', result)}\033[0m", flush=True)

        pending = self.exp.pending
        pending_id = pending.id if pending else None
        if pending_id != self._seen_pending:
            if pending is not None:
                print(f"  \033[35m? gate {pending.kind}: {pending.message}\033[0m", flush=True)
            self._seen_pending = pending_id


async def detection_is_up(exp: ExperimentSession) -> bool:
    """Whether a detection node is on the bus, from the SYSTEM registry.

    The session holds no registry client, so borrow its channel. If the monitor is
    unreachable, say True rather than crying wolf -- the storage node makes the same
    call and reaches the same conclusion, so a false alarm here would be a guess about
    a guess.
    """
    try:
        exchange = await exp._channel.declare_exchange(
            SYSTEM.exchange, ExchangeType(SYSTEM.exchange_type), durable=True
        )
        registry = SystemRegistryClient(exp._channel, exchange, timeout=10.0)
        await registry.start()
        try:
            listing = await registry.list_nodes()
        finally:
            await registry.stop()
    except Exception:
        return True
    return any(n.equipment == "detection" and n.status == "up" for n in listing.nodes)


async def preflight(exp: ExperimentSession, args: argparse.Namespace) -> bool:
    """Fail with a diagnosis rather than mid-growth. Every check here corresponds to a
    way this has actually gone wrong."""
    phase("preflight")
    state = await exp.driver.get_state()

    deps = state.deps_available or {}
    missing = [name for name, up in deps.items() if not up]
    note(f"experiment node up, deps: {deps or '(none reported yet)'}")
    if missing:
        warn(f"these dependencies are down: {', '.join(missing)} -- the growth will fail on the first op that needs one")

    # The storage node refuses to record unless every source the request names is on the
    # bus, and `manager.start_storage` always asks for save_ai, which maps to the
    # detection node (storage/handlers.py DEPENDENCIES). A stack started without
    # --with-detection therefore fails at start_storage, several minutes into a growth
    # and well after the ramp -- so check it here instead.
    #
    # Ask the registry, not the experiment node: `deps_available` covers only what the
    # experiment node itself consumes (chamber, rheed, storage), so `.get("detection")`
    # against it is False whether detection is down or simply not its dependency.
    if not args.dryrun and not await detection_is_up(exp):
        warn("the detection node is not on the bus, and start_storage always requests save_ai,")
        warn("so recording will be refused: 'cannot record: detection not available on the bus'.")
        warn("Start the stack with --with-detection, or use --dryrun to skip recording.")

    batch = await exp.chamber_log.log()
    if not batch.entries:
        warn("the chamber log has no rows yet")
        return False
    values = batch.entries[-1].values

    # `--src test` replays a recording of an idle chamber: Motor Stat is 0x0000 for
    # every row, so is_motor_free() never returns true and set_target blocks forever.
    if str(values.get("Motor Stat", "")).strip() in {"0x0000", "0"}:
        warn("the chamber looks like `--src test` (Motor Stat 0x0000 -- the motors never report free).")
        warn("restart it with `--src sim` or this will hang on the first target select.")
        return False

    temperature = as_float(values, "HT Temp moni")
    note(f"chamber at {temperature:.0f} degC, {as_float(values, 'Vac Pres Main'):.2e} Torr")

    # How long the warm-up will sit there, in wall time. These sleeps are on the
    # experiment node and `--speed` cannot reach them -- only the node's own bounds can,
    # which start_simulation.sh --experiment-speed overrides. Read from this process's
    # settings, so it is an estimate: the node may have been started with different ones.
    if temperature < 220.0 <= args.temperature and not args.dryrun:
        b = settings.experiment.bounds
        step_seconds = b.warm_up_step / b.warm_up_current_ramp_rate
        ramp_seconds = ((8.5 - 7.0) / b.warm_up_step) * step_seconds
        note(f"warm-up will take ~{ramp_seconds:.0f}s to ramp plus up to "
             f"{b.warm_up_max_waittime:.0f}s waiting, in wall time")
        if ramp_seconds + b.warm_up_max_waittime > 60.0:
            warn("that is minutes of real time, and --speed does not compress it -- those sleeps")
            warn("are the experiment node's. Restart the stack with --experiment-speed N to scale")
            warn("them (start_simulation.sh does it for you when you pass --chamber-speed).")

    return True


def providers(args: argparse.Namespace):
    """The recipe's two human hooks. Unattended by default: every `[Manual]` step is
    announced and auto-answered, so the run does not stop on a prompt nobody is at."""
    if args.interactive:
        return recipes.default_input_provider, recipes.default_value_provider

    async def auto_input(message: str) -> None:
        print(f"  \033[2m[auto] {message}\033[0m", flush=True)

    async def auto_value(prompt: str) -> str:
        # The two questions the recipe asks: a measured laser power, and yes/no
        # alignment checks. Answer power with what was asked for, everything else yes.
        answer = str(args.laser_power) if "measured" in prompt.lower() else "y"
        print(f"  \033[2m[auto] {prompt} -> {answer}\033[0m", flush=True)
        return answer

    return auto_input, auto_value


async def growth(exp: ExperimentSession, watcher: "Watcher", args: argparse.Namespace) -> int:
    phase("bookkeeping")
    project = await exp.driver.register_project(RegisterProject(
        project_name=args.project, description="scripts/demo_experiment.py",
    ))
    note(f"project {project.project_name} (id {project.project_id})")

    substrate = await exp.driver.register_substrate(RegisterSubstrate(
        materials=args.substrate, orientation="001",
        width=5.0, height=5.0, thickness=0.5,
        substrate_name=f"demo-{uuid.uuid4().hex[:6]}",
    ))
    note(f"substrate {substrate.substrate_uuid} (id {substrate.substrate_id}), "
         f"{len(substrate.positions)} position(s)")

    targets = await exp.driver.show_available_targets()
    note(f"targets: {targets.targets}")

    # Neither `to_temperature` nor `perform_single_deposition` turns the heating laser
    # on -- the recipe's `[Manual] Set excimer laser to ON` is the *ablation* laser, a
    # different device. With the heating diode off, `Set Heating Current` moves `HT set`
    # and nothing else, so the warm-up ramp stalls at the pyrometer floor and
    # to_temperature raises. Driving a growth means turning the heater on first.
    if not args.dryrun and not args.no_heater_init:
        phase("heater")
        await exp.driver.initiate_heating_laser()
        note("heating laser on, PID engaged (initiate_heating_laser)")

    phase(f"growth: {args.pulses} pulses of {args.target_material} at "
          f"{args.temperature:.0f} degC, {args.pressure:.2e} Torr")
    input_provider, value_provider = providers(args)
    started = time.monotonic()

    ok, (storage_name, conditions) = await recipes.perform_single_deposition(
        exp,
        project_name=args.project,
        pressure=args.pressure,
        temperature=args.temperature,
        laser_power=args.laser_power,
        laser_repetition_rate=args.rate,
        target_id=args.target,
        target_material=args.target_material,
        num_pulse=args.pulses,
        do_preablation=args.preablation_pulses > 0,
        preablation_pulse=args.preablation_pulses,
        preablation_frequency=args.rate,
        ramp_rate=args.ramp_rate,
        is_dryrun=args.dryrun,
        input_provider=input_provider,
        value_provider=value_provider,
    )

    phase("result")
    elapsed = time.monotonic() - started

    # The recipe's return value only covers the steps it checks. A long-running op that
    # raised is not one of them, so ask the watcher what it saw.
    if watcher.failed:
        warn(f"{len(watcher.failed)} long-running task(s) failed and the recipe continued anyway:")
        for result in watcher.failed:
            warn(f"  {result.get('error', result)}")
        warn("the growth that follows a failed ramp is not the growth you asked for")
        return 1

    if not ok:
        warn(f"the recipe reported failure after {elapsed:.0f}s")
        # The recipe logs its reason (start_storage refused: ..., no pixel left, ...)
        # from *this* process, so it is one of the warnings above -- not in the node's
        # log, which is where this used to send people.
        warn("the reason is the warning logged just above")
        return 1

    note(f"finished in {elapsed:.0f}s wall clock")
    note(f"storage record: {storage_name}")
    for key, value in (conditions or {}).items():
        note(f"  {key}: {value}")
    note("the provenance row is in the growth database (experiment.growth_db_path)")
    return 0


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo_experiment",
        description="Drive a growth through the experiment node, the way a notebook would.",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="the --chamber-speed the chamber node was started with. Sizes this "
                             "script's RPC timeouts only -- it cannot speed the run up. The "
                             "chamber's clock is set on nodes/pascal.py and the experiment node's "
                             "wall-clock pacing by --experiment-speed on start_simulation.sh")
    parser.add_argument("--project", default="demo_experiment")
    parser.add_argument("--substrate", default="SrTiO3")
    parser.add_argument("--target", default="C", help="carousel slot A-F, Clear, Monitor")
    parser.add_argument("--target-material", default="LaAlO3")
    parser.add_argument("--temperature", type=float, default=700.0, help="degC")
    parser.add_argument("--ramp-rate", type=float, default=20.0, help="degC/min")
    parser.add_argument("--pressure", type=float, default=2.0e-2, help="Torr")
    parser.add_argument("--laser-power", type=float, default=1.2, help="W, reported back to the gate")
    parser.add_argument("--pulses", type=int, default=600)
    parser.add_argument("--rate", type=float, default=10.0, help="laser Hz")
    parser.add_argument("--preablation-pulses", type=int, default=300, help="0 to skip")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between printed samples")
    parser.add_argument("--dryrun", action="store_true",
                        help="run the bookkeeping and gates but skip the ramp and the laser")
    parser.add_argument("--no-heater-init", action="store_true",
                        help="skip initiate_heating_laser (the heater is already on and PID engaged)")
    parser.add_argument("--interactive", action="store_true",
                        help="answer the [Manual] steps yourself instead of automatically")
    parser.add_argument("--quick", action="store_true",
                        help="short preset: 300 degC, fast ramp, few pulses")
    args = parser.parse_args()
    if args.quick:
        args.temperature, args.ramp_rate = 300.0, 120.0
        args.pulses, args.preablation_pulses = 120, 60
    return args


async def main() -> int:
    args = cli()

    # The recipe runs in *this* process, and it reports why it gave up through the
    # logging module -- `start_storage refused: ...` and friends. Without a handler
    # those vanish and all you get is "the recipe reported failure".
    logging.basicConfig(level=logging.WARNING, format="  \033[33m! %(message)s\033[0m")

    # The driver's RPC deadline. The long ops return a TaskAck immediately, but the
    # inline ones (set_target, move_mask_to_position) do their MI waiting inside the
    # handler, so this has to cover a real mask traverse -- more of it at --speed 1.
    timeout = max(120.0, 600.0 / max(args.speed, 1.0))

    try:
        session = await ExperimentSession.open(
            host=args.host, user=args.user, password=args.password, timeout=timeout,
        )
    except Exception as exc:
        print(f"cannot reach the broker at {args.host}:5672 ({type(exc).__name__}: {exc})\n"
              f"start the stack first: scripts/start_simulation.sh --with-experiment", file=sys.stderr)
        return 1

    watcher = Watcher(session, args.interval)
    try:
        if not await preflight(session, args):
            return 1
        await watcher.start()
        return await growth(session, watcher, args)
    except TimeoutError as exc:
        print(f"\nthe experiment node did not answer: {exc}", file=sys.stderr)
        print("is nodes/experiment.py running? start_simulation.sh needs --with-experiment.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted -- note that this does not stop the chamber; a script already "
              "loaded runs to completion (docs/TODO.md).", file=sys.stderr)
        return 130
    finally:
        await watcher.stop()
        await session.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
