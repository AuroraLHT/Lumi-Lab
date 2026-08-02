"""Building the chamber node's worker threads.

Lifted out of nodes/pascal.py, where the same three-way `if args.src == ...` branching
was written out inline for the log reader, the config reader, the MI server and the
camera.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lumi.base.camera.jpeg_stream import JpegEncoder, JpegEncoderConfig
from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig
from lumi.base.camera.web_camera import WebCamera, WebCameraConfig
from lumi.config import settings
from lumi.pascal.config_reader import ConfigReader, ConfigReaderConfig
from lumi.pascal.log_reader import (
    LogReader,
    LogReaderConfig,
    TestLogReader,
    TestLogReaderConfig,
)
from lumi.pascal.mi_mode import MIModeBackendSimulator, MIModeServer, MIModeServerConfig
from lumi.path import PROJECT_ROOT

log = logging.getLogger(__name__)

ASSETS = PROJECT_ROOT / "src" / "lumi" / "pascal" / "assets"


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
    cfg = settings.pascal.config_reader.test if src == "test" else settings.pascal.config_reader
    return ConfigReader(
        config=ConfigReaderConfig(idle_time=cfg.idle_time, config_path=cfg.config_path),
        name=cfg.name,
        daemon=True,
    )


def build_mi_mode(src: str, mi_folder: str | None):
    """Returns (mi_server, simulator_or_None)."""
    cfg = settings.pascal.mi_mode_server

    if src == "path":
        if not mi_folder:
            raise ValueError("--mi is required when --src=path")
        config = MIModeServerConfig(
            queue_size=cfg.queue_size,
            idle_time=cfg.idle_time,
            mi_folder=mi_folder,
            assist_file_name=cfg.assist_file_name,
        )
        return MIModeServer(config=config, name=cfg.name, daemon=True), None

    folder = ASSETS / "mi_mode"
    folder.mkdir(parents=True, exist_ok=True)
    log.info("simulating MI mode in %s", folder)
    config = MIModeServerConfig(
        queue_size=cfg.queue_size,
        idle_time=cfg.idle_time,
        mi_folder=str(folder),
        assist_file_name=cfg.assist_file_name,
    )
    return (
        MIModeServer(config=config, name=cfg.name, daemon=True),
        MIModeBackendSimulator(config),
    )


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
