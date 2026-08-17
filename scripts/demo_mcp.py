#!/usr/bin/env python
"""Drive the simulated chamber through the **MCP server**, the way an LLM agent would.

    scripts/start_simulation.sh --chamber-speed 30 --with-experiment   # terminal 1
    uv run scripts/demo_mcp.py                                        # terminal 2

The third sibling of scripts/demo_growth.py and scripts/demo_experiment.py, one layer
up again. demo_growth sends raw MI commands to the chamber node; demo_experiment drives
`lumi.experiment.recipes` through the generated client; this one goes through
`lumi.mcp` -- it spawns the server over stdio exactly as Claude Code does, and every
chamber action below is a real MCP `tools/call`. Nothing here imports the experiment
client, so what passes here is what an agent can actually do.

It is a check, not a growth: each phase asserts something specific and prints PASS or
FAIL, and the exit status is the number of failures. The gas phases are the interesting
ones -- a flow setpoint is inert until the master gate is opened, and that gate is a
separate tool on purpose (see contracts/experiment.py), so this proves both halves are
reachable and that the tool descriptions say so.

With --http URL it drives the streamable-HTTP transport instead, which additionally
exercises the bearer-token auth in lumi.mcp.auth. That transport always requires an
operator-or-admin token, so getting one is most of the work -- scripts/start_mcp_http.sh
does all of it (account, login, token file, server).

Note this is the *script's* way in, not a real client's. A proper MCP host (Claude
Code, Codex) is handed no token at all: it gets a 401, follows it to the server's own
login page and logs in with a username and password, then renews itself -- see
lumi.mcp.oauth. This script deliberately keeps the flat --token path, because a
browser round-trip is not something a PASS/FAIL check can walk on its own, and because
the token it presents is the same JWT either route ends at. What it proves is that the
transport accepts one; what an operator should actually do is log in.

    scripts/start_simulation.sh --chamber-speed 30 --with-experiment --with-auth
    scripts/start_mcp_http.sh                                         # terminal 2
    uv run scripts/demo_mcp.py --http http://127.0.0.1:8100/mcp \
        --token "$(cat run/simulation/mcp_token.txt)"                 # terminal 3

By hand, and why each step is the way it is:

    # 1. An operator in the store the MCP server will read. start_simulation.sh points
    #    the stack at run/simulation/users.db, so use the same one -- the `manage` CLI
    #    otherwise writes to cfg/users.db and the server never sees the account.
    export DYNACONF_AUTH__DATABASE_PATH=run/simulation/users.db
    uv run python -m lumi.api.manage create-user agent --role operator --password ...

    # 2. A token, from the same login the browser uses. This needs a stack started
    #    --with-auth: with auth off, /auth/login skips the password check and hands
    #    back the *anonymous* identity, whose user row is deliberately inactive, and
    #    lumi.mcp.auth rejects inactive users -- so that token 401s here.
    #    (--with-auth's admin/simulation bootstrap only fires on an empty database,
    #    which is why step 1 creates the account explicitly.)
    TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
              -d 'username=agent&password=...' | jq -r .access_token)

    # 3. Serve and drive. Same DYNACONF_AUTH__DATABASE_PATH as step 1.
    uv run python -m lumi.mcp --transport http --port 8100            # terminal 2
    uv run scripts/demo_mcp.py --http http://127.0.0.1:8100/mcp --token "$TOKEN"

The server serves plain HTTP; TLS is a reverse proxy's job (see lumi.mcp.__main__), so
--http takes an http:// URL even in production behind one.

Needs `nodes/experiment.py` running and `nodes/pascal.py --src sim`; it checks and says
so if either is missing. Against `--src test` the log is a recording of an idle chamber
and nothing will move.

The default budgets assume a compressed chamber clock (--chamber-speed 30 above). On a
chamber running at 1x, the gas line's time constant is ~26s and the ramp is minutes, so
raise --gas-budget and --ramp-budget or the waits will time out on a chamber that is
behaving perfectly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

# Deliberately no `lumi.config` import: this script drives the MCP server as a client
# and reads none of the backend's settings. Where it needs a broker or a user store,
# the server (or the stack's run/simulation/env.sh) decides, not this.

#: Tools this script needs. Listed rather than discovered so a contract that loses one
#: fails here loudly instead of silently skipping the phase that used it.
REQUIRED = [
    "experiment.driver.get_current_log",
    "experiment.driver.get_current_pressure",
    "experiment.driver.get_current_temperature",
    "experiment.driver.get_mfc_status",
    "experiment.driver.set_mfc_flow",
    "experiment.driver.set_mfc_control",
    "experiment.driver.set_pressure",
    "experiment.driver.set_pressure_control",
    "experiment.driver.initiate_heating_laser",
    "experiment.driver.turn_off_heating_laser",
    "experiment.driver.to_temperature",
]


def phase(title: str) -> None:
    print(f"\n\033[1m=== {title} \033[0m".ljust(88, "="), flush=True)


def note(message: str) -> None:
    print(f"  {message}", flush=True)


def warn(message: str) -> None:
    print(f"  \033[33m! {message}\033[0m", flush=True)


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def ok(self, condition: bool, description: str, detail: str = "") -> bool:
        if condition:
            print(f"  \033[32mPASS\033[0m {description}", flush=True)
        else:
            self.failed.append(description)
            print(f"  \033[31mFAIL\033[0m {description}"
                  f"{chr(10) + '       ' + detail if detail else ''}", flush=True)
        return condition


class Mcp:
    """A thin call/expect wrapper over ClientSession.

    The server returns one TextContent holding the response model's JSON, and signals a
    refusal with is_error rather than a protocol error (server.py `_on_call_tool`), so
    both outcomes come back through the same path and `call` never raises for a refusal
    -- which is exactly the distinction the heater phase is testing.
    """

    def __init__(self, session: ClientSession, verbose: bool = False) -> None:
        self.session = session
        self.verbose = verbose

    async def call(self, name: str, **arguments) -> tuple[bool, dict | str]:
        result = await self.session.call_tool(name, arguments or None)
        text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
        if self.verbose:
            args = json.dumps(arguments) if arguments else ""
            note(f"\033[2m-> {name}({args}) {'ERR ' if result.is_error else ''}{text[:120]}\033[0m")
        if result.is_error:
            return False, text
        try:
            return True, json.loads(text)
        except json.JSONDecodeError:
            return True, text

    async def expect(self, name: str, **arguments) -> dict:
        """For calls whose failure means the rest of the run is meaningless."""
        ok, payload = await self.call(name, **arguments)
        if not ok:
            raise RuntimeError(f"{name} failed: {payload}")
        return payload

    async def pressure(self) -> float:
        return float((await self.expect("experiment.driver.get_current_pressure"))["pressure"])

    async def flow(self, mfc_id: str = "1") -> tuple[float, float]:
        status = await self.expect("experiment.driver.get_mfc_status", mfc_id=mfc_id)
        return float(status["set"]), float(status["monitor"])

    async def hold(self, predicate, budget: float, what: str,
                   samples: int = 3, interval: float = 1.0) -> tuple[bool, tuple[float, float, float]]:
        """Poll until `predicate(pressure, set_flow, monitor)` holds `samples` polls in
        a row, and return the last sample either way.

        Consecutive, because the chamber passes *through* the right answer on its way
        somewhere else: a pressure rising towards 31 mTorr crosses 20 mTorr en route,
        and a single sample taken there reads as a controller that has settled on 20
        when it has not even been asked yet. That is not hypothetical -- it is what
        this script did on its first run, and only the flow readback gave it away.

        Polling, not a subscription, because ops are the whole of the MCP surface: an
        agent cannot watch driver.update, which is why every wait here looks like this.
        """
        deadline = time.monotonic() + budget
        started = time.monotonic()
        streak = 0
        sample = (0.0, 0.0, 0.0)
        while time.monotonic() < deadline:
            set_flow, monitor = await self.flow()
            sample = (await self.pressure(), set_flow, monitor)
            streak = streak + 1 if predicate(*sample) else 0
            if streak >= samples:
                note(f"{what}: {sample[0]:.3e} Torr at {monitor:.2f} sccm, "
                     f"steady after {time.monotonic() - started:.0f}s")
                return True, sample
            await asyncio.sleep(interval)
        note(f"{what}: gave up at {sample[0]:.3e} Torr, {sample[2]:.2f} sccm after {budget:.0f}s")
        return False, sample


async def handshake(mcp: Mcp, session: ClientSession, checks: Checks) -> list[str]:
    phase("1. handshake")
    info = session.initialize_result.server_info if session.initialize_result else None
    if info is not None:
        note(f"server {info.name} {info.version}")

    tools = (await session.list_tools()).tools
    names = [t.name for t in tools]
    by_capability: dict[str, int] = {}
    for name in names:
        by_capability[name.rsplit(".", 1)[0]] = by_capability.get(name.rsplit(".", 1)[0], 0) + 1
    note(f"{len(names)} tools: " + ", ".join(f"{cap} x{n}" for cap, n in sorted(by_capability.items())))

    missing = [name for name in REQUIRED if name not in names]
    checks.ok(not missing, "every tool this script needs is exposed", f"missing: {missing}")

    # The gates are separate ops, so the only thing telling an agent it must open one is
    # the tool description -- which is the op's `doc` in contracts/experiment.py. An
    # undocumented gate op is the bug this whole exercise came from, so check for it.
    described = {t.name: (t.description or "") for t in tools}
    checks.ok(
        "set_mfc_control" in described.get("experiment.driver.set_mfc_flow", ""),
        "set_mfc_flow's description points at the gate it needs",
        f"got: {described.get('experiment.driver.set_mfc_flow', '')[:80]!r}",
    )

    # Read-only situational awareness, and nothing that runs raw MI scripts.
    checks.ok("rheed.camera.get_camera_config" in names, "rheed.camera is exposed for situational awareness")
    checks.ok(not any(n.startswith("system.") or ".mi_mode." in n for n in names),
              "no system.* or chamber.mi_mode tools -- no raw script execution",
              f"found: {[n for n in names if n.startswith('system.') or '.mi_mode.' in n]}")
    return names


async def preflight(mcp: Mcp, checks: Checks) -> bool:
    phase("2. preflight")
    ok, log = await mcp.call("experiment.driver.get_current_log")
    if not ok:
        warn(f"the experiment node did not answer: {log}")
        warn("is nodes/experiment.py running? start the stack with --with-experiment.")
        return False

    values = log.get("values") or {}
    if not values:
        warn("the chamber log has no rows yet -- is nodes/pascal.py running?")
        return False

    # `--src test` replays a recording of an idle chamber: nothing in it ever moves, so
    # every assertion below would fail for a reason that has nothing to do with MCP.
    if str(values.get("Motor Stat", "")).strip() in {"0x0000", "0"}:
        warn("the chamber looks like `--src test` (Motor Stat 0x0000). Restart it with")
        warn("`--src sim` -- against a recording none of these checks can pass.")
        return False

    temperature = (await mcp.expect("experiment.driver.get_current_temperature"))["temperature"]
    note(f"chamber at {temperature:.0f} degC, {await mcp.pressure():.2e} Torr")
    return True


async def heater(mcp: Mcp, checks: Checks, args: argparse.Namespace) -> None:
    phase("3. heater: the refusal has to reach the caller")

    # Long-running ops report failure on driver.update, which MCP does not expose -- so
    # to_temperature checks the heater in the handler, before the task is spawned, and
    # the agent gets an error on the tool call. If this ever regresses to a task-level
    # failure the call below starts "succeeding" and the chamber sits cold.
    await mcp.expect("experiment.driver.turn_off_heating_laser")
    await asyncio.sleep(2.0)  # let the log catch up with the heater going off
    ok, payload = await mcp.call("experiment.driver.to_temperature", temperature=300.0, ramp_rate=20.0)
    checks.ok(not ok, "to_temperature is refused while the heating laser is off",
              f"returned success: {payload}")
    checks.ok(not ok and "initiate_heating_laser" in str(payload),
              "the refusal names the tool to call instead", f"got: {payload}")

    if args.skip_ramp:
        note("skipping the ramp (--skip-ramp)")
        return

    await mcp.expect("experiment.driver.initiate_heating_laser")
    note("heating laser on, PID engaged")
    await asyncio.sleep(2.0)
    ok, payload = await mcp.call("experiment.driver.to_temperature",
                                 temperature=args.temperature, ramp_rate=args.ramp_rate)
    if not checks.ok(ok, "to_temperature is accepted once the laser is on", f"refused: {payload}"):
        return
    note(f"task {payload.get('task_id')} started -- polling get_current_temperature, since")
    note("an agent has no way to watch current_task (ops are the whole MCP surface)")

    deadline = time.monotonic() + args.ramp_budget
    reached = False
    while time.monotonic() < deadline:
        temperature = (await mcp.expect("experiment.driver.get_current_temperature"))["temperature"]
        if temperature >= args.temperature - 10.0:
            reached = True
            break
        await asyncio.sleep(2.0)
    checks.ok(reached, f"chamber reached ~{args.temperature:.0f} degC within {args.ramp_budget:.0f}s",
              f"stalled at {(await mcp.expect('experiment.driver.get_current_temperature'))['temperature']:.0f} degC")


async def gas(mcp: Mcp, checks: Checks, args: argparse.Namespace) -> None:
    phase("4. gas: a flow setpoint is inert until the gate opens")
    base = await mcp.pressure()
    note(f"baseline {base:.3e} Torr")

    await mcp.expect("experiment.driver.set_mfc_flow", mfc_id="1", flow=args.flow)
    await asyncio.sleep(args.gate_wait)
    set_flow, monitor = await mcp.flow()
    after = await mcp.pressure()
    note(f"after set_mfc_flow({args.flow:g}) and {args.gate_wait:.0f}s: "
         f"set={set_flow:.2f} monitor={monitor:.2f} sccm, {after:.3e} Torr")
    checks.ok(abs(set_flow - args.flow) < 0.05, "the setpoint took")
    checks.ok(monitor < 0.5 and after < base * 100,
              "no gas flows yet -- the master gate is still shut",
              f"monitor={monitor:.2f} sccm, {after:.3e} Torr")

    phase("5. gas: opening the gate moves the pressure")
    await mcp.expect("experiment.driver.set_mfc_control", enabled=True)
    settled, (after, _, monitor) = await mcp.hold(
        lambda p, s, m: p > base * 1000 and abs(m - args.flow) < 0.2,
        args.gas_budget, "pressure rose",
    )
    checks.ok(settled, f"pressure rose after set_mfc_control within {args.gas_budget:.0f}s",
              f"{after:.3e} Torr at {monitor:.2f} sccm")
    checks.ok(settled and abs(monitor - args.flow) < 0.5,
              f"MFC1 monitor followed the setpoint to {args.flow:g} sccm", f"monitor={monitor:.2f} sccm")

    phase("6. gas: closed loop holds a pressure setpoint")
    target = args.pressure
    await mcp.expect("experiment.driver.set_pressure", pressure=target)
    await mcp.expect("experiment.driver.set_pressure_control", on=True)

    # Two claims at once, and both have to hold together: the pressure is at the
    # setpoint, and the *controller* is what is holding it there. Checking only the
    # pressure would pass while the chamber merely drifts past on the flow phase 4 set,
    # so the flow having moved off args.flow is what makes this a test of the loop.
    held, (final, set_flow, monitor) = await mcp.hold(
        lambda p, s, m: abs(p - target) / target < 0.05 and abs(m - s) < 0.3 and abs(m - args.flow) > 0.2,
        args.gas_budget, f"settled on {target:.2e} Torr",
    )
    checks.ok(held, f"pressure control reached and held {target:.2e} Torr within {args.gas_budget:.0f}s",
              f"{final:.3e} Torr at set={set_flow:.2f} monitor={monitor:.2f} sccm")
    checks.ok(held and abs(monitor - args.flow) > 0.2,
              "the controller took the flow over from set_mfc_flow",
              f"set={set_flow:.2f} monitor={monitor:.2f} sccm, unchanged from set_mfc_flow")


async def cleanup(mcp: Mcp, args: argparse.Namespace) -> None:
    phase("7. cleanup")
    # Order matters: drop the loop before the gate, or the controller reopens it.
    for name, arguments in (
        ("experiment.driver.set_pressure_control", {"on": False}),
        ("experiment.driver.set_mfc_flow", {"mfc_id": "1", "flow": 0.0}),
        ("experiment.driver.set_mfc_control", {"enabled": False}),
        ("experiment.driver.turn_off_heating_laser", {}),
    ):
        ok, payload = await mcp.call(name, **arguments)
        if not ok:
            warn(f"{name} failed on the way out: {payload}")
    note("gas off, gate shut, heater off")
    if not args.skip_ramp:
        note("the chamber is still hot -- it cools on its own, or call cool_down")


async def run(session: ClientSession, args: argparse.Namespace) -> int:
    checks = Checks()
    mcp = Mcp(session, verbose=args.verbose)
    await session.initialize()

    await handshake(mcp, session, checks)
    if not await preflight(mcp, checks):
        return 1

    try:
        await heater(mcp, checks, args)
        await gas(mcp, checks, args)
    finally:
        with contextlib.suppress(Exception):
            await cleanup(mcp, args)

    phase("result")
    if checks.failed:
        for description in checks.failed:
            warn(f"failed: {description}")
        return len(checks.failed)
    note("\033[32mall checks passed\033[0m")
    return 0


async def main(args: argparse.Namespace) -> int:
    if args.http:
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        if not args.token:
            warn("the HTTP transport always requires an operator-or-admin bearer token.")
            warn("Mint one with `POST /auth/login` on the API bridge and pass --token.")
            return 1
        headers = {"Authorization": f"Bearer {args.token}"}
        note(f"connecting to {args.http}")
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(args.http, http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    return await run(session, args)

    # stdio: spawn the server exactly the way an MCP host does -- bare, so it applies its
    # own config, and passing --host only when asked to override it. Claude Code's
    # registration is the same command with no flags; anything added unconditionally here
    # would be tested but not shipped.
    server_args = ["-m", "lumi.mcp"] + (["--host", args.broker] if args.broker else [])
    note(f"spawning: {sys.executable} {' '.join(server_args)}")
    params = StdioServerParameters(command=sys.executable, args=server_args, env=dict(os.environ))

    # The server logs to stderr at INFO (stdout is the protocol stream, so it has no
    # choice), and stdio_client forwards that straight to ours -- including aio_pika's
    # queue-teardown lines, which arrive *after* the results are printed and read like
    # something went wrong at the end. Capture it instead: shown only if a check fails
    # or under -v, since it is the only diagnostic when the server cannot start at all.
    with tempfile.TemporaryFile("w+", prefix="lumi-mcp-", suffix=".log") as errlog:
        code = 1
        try:
            async with stdio_client(params, errlog=sys.stderr if args.verbose else errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    code = await run(session, args)
        finally:
            # In `finally`, not after the `with`: a server that dies during startup
            # raises out of stdio_client, and its log is the only account of why. Read
            # here rather than inside, so the context has exited and the process is done
            # writing.
            if code and not args.verbose:
                errlog.seek(0)
                captured = errlog.read().strip()
                if captured:
                    phase("mcp server log")
                    for line in captured.splitlines():
                        print(f"  \033[2m{line}\033[0m", flush=True)
    return code


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo_mcp",
        description="Exercise the experiment MCP server against the simulated chamber.",
    )
    parser.add_argument("--http", metavar="URL",
                        help="drive the streamable-HTTP transport at this URL instead of stdio")
    parser.add_argument("--token", help="bearer token for --http (operator or admin)")
    # An override, not a default. Left unset, the spawned server resolves its own broker
    # from settings.rabbitmq.host -- repeating that here would just be a second place to
    # keep in step with it. Only meaningful for stdio, where this script is the one doing
    # the spawning; under --http the server is already running and picked its own.
    parser.add_argument("--broker", metavar="HOST", default=None,
                        help="override the AMQP broker for the server this script spawns "
                             "(stdio only, not valid with --http). Default: whatever "
                             "`python -m lumi.mcp` itself picks, i.e. settings.rabbitmq.host")

    parser.add_argument("--flow", type=float, default=5.0, help="MFC1 flow to test with, sccm")
    parser.add_argument("--pressure", type=float, default=20.0e-3, help="closed-loop setpoint, Torr")
    parser.add_argument("--temperature", type=float, default=300.0, help="ramp target, degC")
    parser.add_argument("--ramp-rate", type=float, default=20.0, help="degC/min")

    parser.add_argument("--gate-wait", type=float, default=20.0,
                        help="seconds to wait before concluding a shut gate passes no gas")
    parser.add_argument("--gas-budget", type=float, default=180.0, help="seconds allowed for the chamber to settle")
    parser.add_argument("--ramp-budget", type=float, default=300.0, help="seconds allowed for the ramp")
    parser.add_argument("--skip-ramp", action="store_true",
                        help="check the refusal but do not heat the chamber")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every tool call, and let the server's own log through live")
    args = parser.parse_args()

    if args.http and args.broker is not None:
        parser.error("--broker applies only to the stdio transport, where this script spawns "
                     "the server. Under --http the server is already running and chose its own "
                     "broker; restart it with a different --host to change that.")
    return args


if __name__ == "__main__":
    arguments = cli()
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        raise SystemExit(130) from None
