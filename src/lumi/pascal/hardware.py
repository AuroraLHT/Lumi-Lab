"""Building the chamber node's worker threads.

Lifted out of nodes/pascal.py, where the same three-way `if args.src == ...` branching
was written out inline for the log reader, the config reader, the MI server and the
camera.

Three sources now:

  path  the real chamber -- a PASCAL-written log file and MI folder on disk
  sim   the state-model simulator in `lumi.pascal.sim`
  test  replay of a recorded log, with an MI backend that ignores the script

`test` is kept for reproducing a specific recorded session. It is not a substitute for
`sim`: the recorded session is an idle chamber, so nothing that drives the chamber can
be exercised against it (see `lumi.pascal.sim.__init__`).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from lumi.base.camera.jpeg_stream import JpegEncoder, JpegEncoderConfig
from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig
from lumi.base.camera.web_camera import WebCamera, WebCameraConfig
from lumi.config import settings
from lumi.pascal.config_reader import ConfigParser, ConfigReader, ConfigReaderConfig
from lumi.pascal.log_reader import (
    LogReader,
    LogReaderConfig,
    TestLogReader,
    TestLogReaderConfig,
)
from lumi.pascal.mi_mode import MIModeBackendSimulator, MIModeServer, MIModeServerConfig
from lumi.pascal.sim import ChamberSimConfig, build_chamber_sim
from lumi.pascal.sim.model import DEFAULT_TARGET_ANGLES
from lumi.path import PROJECT_ROOT

log = logging.getLogger(__name__)

ASSETS = PROJECT_ROOT / "src" / "lumi" / "pascal" / "assets"


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def sim_log_dir(override: str | None = None) -> Path:
    """The *directory* the simulated controller writes its log into.

    A directory rather than a file because that is what `LogReader` watches: the real
    PASCAL starts a new file per session and the reader is told about it by a watchdog
    event, so pointing the simulator at a folder exercises the same discovery path.
    """
    return _resolve(override or settings.pascal.sim.log_dir)


def sim_mi_folder(override: str | None = None) -> Path:
    return _resolve(override or settings.pascal.sim.mi_folder)


def build_log_reader(src: str, log_path: str | None):
    if src == "path":
        if not log_path or not Path(log_path).exists():
            raise FileNotFoundError(f"chamber log not found: {log_path}")
        cfg = settings.pascal.log_reader
        return LogReader(
            config=LogReaderConfig(
                queue_size=cfg.queue_size, idle_time=cfg.idle_time, log_path=log_path
            ),
            name=cfg.name,
            daemon=True,
        )

    if src == "sim":
        cfg = settings.pascal.log_reader
        folder = sim_log_dir(log_path)
        # The watchdog observer refuses to schedule a path that does not exist, and it
        # is scheduled when the thread starts -- so create the folder now, not later.
        folder.mkdir(parents=True, exist_ok=True)
        return LogReader(
            config=LogReaderConfig(
                queue_size=cfg.queue_size, idle_time=cfg.idle_time, log_path=str(folder)
            ),
            name="sim_log_reader",
            daemon=True,
        )

    cfg = settings.pascal.log_reader.test
    return TestLogReader(
        config=TestLogReaderConfig(
            queue_size=cfg.queue_size,
            idle_time=cfg.idle_time,
            publish_interval=cfg.publish_interval,
            log_path=ASSETS / cfg.log_file,
        ),
        name="test_log_reader",
        daemon=True,
    )


def build_config_reader(src: str):
    # Anything that is not the real chamber reads the bundled INI. This used to test
    # `src == "test"`, which silently sent `--src sim` at the Windows PLDconfig path.
    cfg = settings.pascal.config_reader if src == "path" else settings.pascal.config_reader.test
    return ConfigReader(
        config=ConfigReaderConfig(idle_time=cfg.idle_time, config_path=cfg.config_path),
        name=cfg.name,
        daemon=True,
    )


def _mi_config(mi_folder: str | Path) -> MIModeServerConfig:
    cfg = settings.pascal.mi_mode_server
    return MIModeServerConfig(
        queue_size=cfg.queue_size,
        idle_time=cfg.idle_time,
        mi_folder=str(mi_folder),
        assist_file_name=cfg.assist_file_name,
    )


def build_mi_mode(src: str, mi_folder: str | None):
    """Returns (mi_server, simulator_or_None). `sim` builds its backend in
    `build_simulated_chamber` instead, because it shares the chamber model."""
    cfg = settings.pascal.mi_mode_server

    if src == "path":
        if not mi_folder:
            raise ValueError("--mi is required when --src=path")
        return MIModeServer(config=_mi_config(mi_folder), name=cfg.name, daemon=True), None

    folder = ASSETS / "mi_mode"
    folder.mkdir(parents=True, exist_ok=True)
    log.info("simulating MI mode in %s", folder)
    config = _mi_config(folder)
    return (
        MIModeServer(config=config, name=cfg.name, daemon=True),
        MIModeBackendSimulator(config),
    )


def target_angles_from_config(config_path: str | Path) -> tuple[dict[str, float], dict[str, int]]:
    """Read the carousel geometry out of PLDconfig.ini.

    `settings.experiment.target_mapper` maps a slot letter to a `TG<n>name` field, so
    slot n is `TG<n>angle`. Taking the angles from the same INI the config capability
    serves keeps `Select Target C` and `get_target_name_by_id("C")` talking about the
    same target instead of two independent guesses.
    """
    angles = dict(DEFAULT_TARGET_ANGLES)
    numbers: dict[str, int] = {}
    try:
        with _resolve(config_path).open() as handle:
            parsed = ConfigParser(io.StringIO(handle.read())).get_configs_by_section("TGsettings")
    except (OSError, KeyError):
        log.warning("could not read [TGsettings] from %s; using the built-in angles", config_path)
        return angles, {}

    for slot, field in settings.experiment.target_mapper.items():
        # "TG3name" -> 3
        index = int(str(field).removeprefix("TG").removesuffix("name"))
        numbers[slot] = index
        angle = parsed.get(f"TG{index}angle")
        if angle is not None:
            angles[slot] = float(angle)
    return angles, numbers


def build_simulated_chamber(log_path: str | None, mi_folder: str | None, time_scale: float = 1.0):
    """The `--src sim` bundle: one model, the log writer, and the MI backend.

    Returns `(model, log_writer, mi_server, mi_backend)`. The log *reader* is built
    separately by `build_log_reader("sim", ...)` -- it is the production reader tailing
    what the writer produces, and knows nothing about the simulator.
    """
    cfg = settings.pascal.sim
    folder = sim_mi_folder(mi_folder)
    folder.mkdir(parents=True, exist_ok=True)

    angles, numbers = target_angles_from_config(settings.pascal.config_reader.test.config_path)
    sim_config = ChamberSimConfig(
        target_angles=angles,
        missed_pulse_rate=float(cfg.missed_pulse_rate),
        temperature_noise=float(cfg.temperature_noise),
        # The measured calibration. Overridable because it is a measurement.
        ld_min=float(cfg.ld_min),
        current_at_temperature_min=float(cfg.current_at_temperature_min),
        current_at_temperature_max=float(cfg.current_at_temperature_max),
        temperature_min=float(cfg.temperature_min),
        temperature_max=float(cfg.temperature_max),
        flow_at_pressure_min=float(cfg.flow_at_pressure_min),
        flow_at_pressure_max=float(cfg.flow_at_pressure_max),
        pressure_at_flow_min=float(cfg.pressure_at_flow_min),
        pressure_at_flow_max=float(cfg.pressure_at_flow_max),
        # Measured axis speeds, likewise.
        target_rotation_speed=float(cfg.target_rotation_speed),
        sample_rotation_speed=float(cfg.sample_rotation_speed),
        sample_spin_speed=float(cfg.sample_spin_speed),
        mask_speed=float(cfg.mask_speed),
        rheed_gun_speed=float(cfg.rheed_gun_speed),
        shutter_travel_time=float(cfg.shutter_travel_time),
        gate_travel_time=float(cfg.gate_travel_time),
        pressure_tau=float(cfg.pressure_tau),
    )
    if numbers:
        sim_config.target_numbers = numbers

    mi_config = _mi_config(folder)
    model, writer, backend = build_chamber_sim(
        sim_log_dir(log_path),
        mi_config,
        sim_config=sim_config,
        time_scale=time_scale,
        log_interval=float(cfg.log_interval),
        tick_interval=float(cfg.tick_interval),
        command_timeout=float(cfg.command_timeout),
    )
    mi_server = MIModeServer(
        config=mi_config, name=settings.pascal.mi_mode_server.name, daemon=True
    )
    log.info(
        "simulated chamber: log -> %s, MI -> %s, time scale %gx",
        sim_log_dir(log_path), folder, time_scale,
    )
    return model, writer, mi_server, backend


def build_camera(src: str):
    """The chamber's own webcam. Returns (camera, height, width)."""
    if src == "path":
        cfg = settings.pascal.webcam
        camera = WebCamera(
            config=WebCameraConfig(
                fps=cfg.fps,
                queue_size=cfg.queue_size,
                idle_time=cfg.idle_time,
                frame_dims=(cfg.height, cfg.width),
            ),
            name=cfg.name,
            daemon=True,
        )
        return camera, cfg.height, cfg.width

    cfg = settings.pascal.simcam
    source = Path(cfg.source)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    camera = SimCamera(
        config=SimCameraConfig(
            frame_dims=(cfg.height, cfg.width),
            source=str(source),  # str, not Path: SimCamera does source.endswith(".npy")
            fps=cfg.fps,
            queue_size=cfg.queue_size,
            idle_time=cfg.idle_time,
            is_base_oscillation=cfg.is_base_oscillation,
            base_oscillation_frequency=cfg.base_oscillation_frequency,
            base_oscillation_amplitude=cfg.base_oscillation_amplitude,
            is_feature_oscillation=cfg.is_feature_oscillation,
            feature_bbox=cfg.feature_bbox,
            feature_oscillation_frequency=cfg.feature_oscillation_frequency,
            feature_oscillation_amplitude=cfg.feature_oscillation_amplitude,
            exposure_time=cfg.exposure_time,
            gain=cfg.gain,
            gamma=cfg.gamma,
            max_intensity=cfg.max_intensity,
        ),
        name=cfg.name,
        daemon=True,
    )
    return camera, cfg.height, cfg.width


def build_jpeg_encoder(camera, camera_queue) -> JpegEncoder:
    """The chamber camera's live stream encoder. See CHAMBER's camera capability."""
    cfg = settings.pascal.jpeg_encoder
    return JpegEncoder(
        camera=camera,
        camera_queue=camera_queue,
        config=JpegEncoderConfig(
            quality=cfg.quality,
            queue_size=cfg.queue_size,
            idle_time=cfg.idle_time,
            max_intensity=cfg.max_intensity,
        ),
        name=cfg.name,
        daemon=True,
    )
