"""The Pascal chamber node: growth log, chamber config, the chamber driver, chamber
camera, fiducial markers.

    python -m nodes.pascal --src sim           # simulated chamber (state model)
    python -m nodes.pascal --src sim --speed 30
    python -m nodes.pascal --src test          # replay of a recorded log
    python -m nodes.pascal --src path --log ...                  # pld-service on this PC
    python -m nodes.pascal --src path --log ... --pld-address 127.0.0.1:50051
    python -m nodes.pascal --src path --log ... --mi ... --use-legacy-mi-mode

The driver is pld-service (`chamber.pld`), the REA control service on the PLD PC,
reached over gRPC on loopback. This node writes the growth-log CSV into `--log` from the
service's state, since PLD Controller is not running. `--use-legacy-mi-mode` goes back
to driving PLD Controller through MI mode (`chamber.mi_mode`), with PLD Controller
writing the log. Whichever driver is not in use is still served, and refuses every op.

`--src sim` runs the chamber simulator behind a stand-in pld-service (or, with
`--use-legacy-mi-mode`, behind the simulated MI backend, as before).

Was 316 lines.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from lumi.base.camera.handlers import JpegCameraHandler
from lumi.config import settings
from lumi.contracts.chamber import CHAMBER
from lumi.node import EquipmentNode
from lumi.pascal.handlers import (
    ChamberConfigHandler,
    ChamberLogHandler,
    DisabledCapability,
    FiducialHandler,
    MIModeHandler,
)
from lumi.contracts.chamber import MI_MODE, PLD
from lumi.contracts.payloads.chamber import MIModeReadout
from lumi.contracts.payloads.pld import PldReadout
from lumi.pascal.hardware import (
    build_camera,
    build_config_reader,
    build_fiducials,
    build_jpeg_encoder,
    build_log_reader,
    build_mi_mode,
    build_pld_driver,
    build_sim_pld_service,
    build_simulated_chamber,
    sim_log_dir,
)
from lumi.pascal.log_archive import LogArchive

log = logging.getLogger(__name__)


def build(args: argparse.Namespace) -> EquipmentNode:
    if args.src == "path" and not args.use_legacy_mi_mode:
        # This node writes the log under the pld driver, so the folder need not exist
        # yet -- but it has to be named, and it has to exist before the reader watches it.
        if not args.log:
            raise SystemExit("--src path needs --log: the folder this node writes the growth log into")
        Path(args.log).mkdir(parents=True, exist_ok=True)
    log_reader = build_log_reader(args.src, args.log)
    config_reader = build_config_reader(args.src)
    sim_workers: list = []
    drain_hooks: list = []
    model = None
    mi_server = mi_simulator = pld_handler = None
    if args.use_legacy_mi_mode:
        if args.src == "sim":
            # One chamber model, shared: the MI backend drives it, the log writer renders
            # it, and the camera's scene overlay reads it (sample rotation, Mask1's slit).
            # The log *reader* above is the production one, tailing the CSV the writer
            # produces -- it has no idea a simulator is on the other end.
            model, log_writer, mi_server, mi_backend = build_simulated_chamber(
                args.log, args.mi, time_scale=args.speed
            )
            sim_workers = [log_writer, mi_backend]
        else:
            mi_server, mi_simulator = build_mi_mode(args.src, args.mi)
    else:
        address = args.pld_address
        if args.src in ("sim", "test"):
            # The simulator behind a stand-in pld-service: the same client, handler and
            # log writer as production, only the far end of the channel swapped.
            model, service = build_sim_pld_service(time_scale=args.speed)
            service.start()
            drain_hooks.append(service.stop)
            sim_workers = [service.ticker]
            address = service.address
        # The log goes where the reader watches; `test` replays a recorded log instead.
        log_dir = {"path": args.log, "sim": sim_log_dir(args.log), "test": None}[args.src]
        pld_handler, pld_workers = build_pld_driver(address, log_dir)
        sim_workers += pld_workers
        drain_hooks.insert(0, pld_handler.release_on_shutdown)
    camera, _, _ = build_camera(args.src, model=model)

    # The camera's in-process fan-out: one grab feeds every consumer. The stream is
    # served from the encoder's output, not from a camera queue directly, so `frames`
    # is the encoder's input rather than the handler's.
    frames_q = camera.register_queue("frames")
    jpeg_encoder = build_jpeg_encoder(camera, frames_q)
    # A second consumer of the same grab: the marker statistics are measured on the
    # camera's own pixels, not on the JPEG the browser is shown.
    fiducial_store, fiducial_stats = build_fiducials(camera.register_queue("fiducials"))

    node = EquipmentNode(
        CHAMBER,
        amqp_url=f"amqp://{args.user}:{args.password}@{args.host}/",
        instance_id=args.instance,
    )

    # The reader watches a folder (a single file for --src test); earlier files in it
    # are the history `list_log_files` / `log_window` read back.
    log_path = Path(log_reader.config.log_path)
    log_archive = LogArchive(
        log_path if log_path.is_dir() else log_path.parent,
        live_path=lambda: getattr(log_reader, "_log_file_path", None),
    )
    node.mount("log", ChamberLogHandler(log_reader, getattr(log_reader, "queue", None), log_archive))
    node.mount("config", ChamberConfigHandler(config_reader))
    if pld_handler is not None:
        node.mount("pld", pld_handler)
        node.mount("mi_mode", DisabledCapability(
            MI_MODE, "this chamber node drives the chamber through pld-service; MI mode is the "
                     "legacy fallback -- restart the chamber node with --use-legacy-mi-mode to use it",
            MIModeReadout()))
    else:
        node.mount("mi_mode", MIModeHandler(mi_server))
        node.mount("pld", DisabledCapability(
            PLD, "this chamber node runs --use-legacy-mi-mode, so the pld-service driver is off; "
                 "drive the chamber through mi_mode, or restart the node without the flag",
            PldReadout(driver="legacy-mi")))
    for hook in drain_hooks:
        node.on_drain(hook)
    node.mount("camera", JpegCameraHandler(camera, jpeg_encoder))
    node.mount("fiducial", FiducialHandler(fiducial_store, fiducial_stats))

    # The simulator's writer goes first: it creates the CSV the log reader is waiting
    # for a watchdog event on.
    workers = [*sim_workers, log_reader, config_reader, camera, jpeg_encoder, fiducial_stats]
    if mi_server is not None:
        workers.append(mi_server)
    if mi_simulator is not None:
        workers.append(mi_simulator)
    for worker in workers:
        node.thread(worker)

    return node


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lumi-pascal", description="Pascal chamber node")
    parser.add_argument("--src", choices=("path", "sim", "test"), default="sim",
                        help="'path' for the real chamber, 'sim' for the state-model "
                             "simulator, 'test' to replay a recorded log")
    parser.add_argument("--log", default=None,
                        help="chamber log folder (--src=path: the pld driver writes the "
                             "CSV here; under --use-legacy-mi-mode PLD Controller does) or "
                             "output directory (--src=sim)")
    parser.add_argument("--mi", default=None,
                        help="MI mode folder (--use-legacy-mi-mode, --src=path or sim)")
    parser.add_argument("--use-legacy-mi-mode", action="store_true",
                        help="drive the chamber through PLD Controller's MI mode instead of "
                             "pld-service (the fallback; PLD Controller must be running and "
                             "writing the log)")
    parser.add_argument("--pld-address", default=settings.get("pascal.pld.address", "127.0.0.1:50051"),
                        help="pld-service's gRPC address, --src=path (it listens on loopback "
                             "only, so this node runs on the PLD PC)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="simulated-time multiplier for --src=sim; 1.0 is real time, "
                             "30 makes a 300s deposition take 10s")
    parser.add_argument("--host", default=settings.rabbitmq.host)
    parser.add_argument("--user", default="guest")
    parser.add_argument("--password", default="guest")
    parser.add_argument("--instance", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    build(args).run()


if __name__ == "__main__":
    cli()
