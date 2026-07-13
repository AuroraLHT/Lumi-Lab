"""Building the RHEED node's worker threads.

Lifted verbatim-in-spirit out of nodes/rheed.py, where ~90 lines of camera-source
branching sat inline in main(). It is real configuration logic and it belongs
somewhere importable and testable -- not in a script.

The pylon import is deliberately lazy: the detection box has no Basler camera and
should not need pypylon installed to import this module.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2

from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig
from lumi.base.camera.video_stream import VideoCompressor, VideoCompressorConfig
from lumi.base.camera.web_camera import WebCamera, WebCameraConfig
from lumi.config import settings
from lumi.path import PROJECT_ROOT
from lumi.rheed.integrator import MultiBoxIntegrator, MultiBoxIntegratorConfig
from lumi.rheed.livefft import STFTCalculator, STFTCalculatorConfig

log = logging.getLogger(__name__)


def _to_8bit(frame):
    """H.264 encodes 8-bit, and cv2.putText asserts on anything else.

    The Basler camera and the simulator both produce uint16, so this conversion has to
    happen before the frame reaches the encoder. It did not: `frame_processing_testcam`
    handed uint16 straight to putText, which raises
    `(-215:Assertion failed) img.depth() == CV_8U`, killing the compressor thread. That
    is why the video capability served zero fragments on the simulated camera.
    """
    import numpy as np

    if frame.dtype == np.uint8:
        return frame
    top = float(frame.max()) or 1.0
    return (frame.astype(np.float32) * (255.0 / top)).astype(np.uint8)


def add_time_stamp(frame, frame_header=None):
    if frame_header is None:
        return frame
    timestamp = frame_header.get("time_stamp")
    if timestamp is None:
        return frame
    return cv2.putText(
        frame, timestamp, (20, 40), cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 0), 2, cv2.LINE_AA
    )


def _to_rgb(frame, frame_header=None):
    frame = cv2.cvtColor(_to_8bit(frame), cv2.COLOR_GRAY2RGB)
    return add_time_stamp(frame, frame_header)


def build_camera(src: str):
    """Returns (camera, frame_processing, height, width)."""
    if src == "pylon":
        # Imported here, not at module scope: only the camera host has pypylon.
        from lumi.base.camera.pylon_camera import PylonCamera, PylonCameraConfig, list_devices

        cfg = settings.rheed.pylon
        devices = list_devices()
        if not devices:
            raise RuntimeError(
                "no Basler camera found -- check the network cable and the IP configuration"
            )
        camera = PylonCamera(
            config=PylonCameraConfig(
                fps=cfg.fps,
                idle_time=cfg.idle_time,
                queue_size=cfg.queue_size,
                frame_dims=(cfg.height, cfg.width),
                device=devices[0],
                camera_max_num_buffer=cfg.camera_max_num_buffer,
                exposure_time=cfg.exposure_time,
                gain=cfg.gain,
                gamma=cfg.gamma,
                black_level=cfg.black_level,
                auto_exposure=cfg.auto_exposure,
                auto_gain=cfg.auto_gain,
                auto_aoi_intensity=cfg.auto_aoi_intensity,
                auto_aoi_whitebalance=cfg.auto_aoi_whitebalance,
            ),
            name=cfg.name,
            daemon=True,
        )
        return camera, _to_rgb, cfg.height, cfg.width

    if src == "webcam":
        cfg = settings.rheed.webcam
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
        return camera, add_time_stamp, cfg.height, cfg.width

    cfg = settings.rheed.simcam
    source = Path(cfg.source)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    camera = SimCamera(
        config=SimCameraConfig(
            frame_dims=(cfg.height, cfg.width),
            # str, not Path: SimCamera.on_initiate does `config.source.endswith(".npy")`.
            # The old nodes/rheed.py passed a Path here too, so `--src test` has been
            # crashing on startup with AttributeError.
            source=str(source),
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
    return camera, _to_rgb, cfg.height, cfg.width


def build_compressor(camera, camera_queue, frame_processing, height: int, width: int) -> VideoCompressor:
    cfg = settings.rheed.video_compressor
    return VideoCompressor(
        camera=camera,
        camera_queue=camera_queue,
        config=VideoCompressorConfig(
            fps=cfg.fps,
            frames_per_keyframe=cfg.frames_per_keyframe,
            fragment_queue_size=cfg.fragment_queue_size,
            cached_startup_fragments=cfg.cached_startup_fragments,
            bit_rate=cfg.bit_rate,
            height=height,
            width=width,
        ),
        frame_processing=frame_processing,
        name=cfg.name,
        daemon=True,
    )


def build_integrator(camera_queue) -> MultiBoxIntegrator:
    cfg = settings.rheed.integrator
    return MultiBoxIntegrator(
        camera=None,
        camera_queue=camera_queue,
        config=MultiBoxIntegratorConfig(
            idle_time=cfg.idle_time,
            output_queue_size=cfg.output_queue_size,
        ),
        daemon=True,
    )


def build_stft(integrator: MultiBoxIntegrator) -> STFTCalculator:
    cfg = settings.rheed.stft_calculator
    return STFTCalculator(
        integrator=integrator,
        config=STFTCalculatorConfig(
            idle_time=cfg.idle_time,
            output_queue_size=cfg.output_queue_size,
            window_size=cfg.window_size,
            hop_size=cfg.hop_size,
            time_resolution=cfg.time_resolution,
        ),
        daemon=True,
    )
