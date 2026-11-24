"""Building the RHEED node's worker threads.

Lifted verbatim-in-spirit out of nodes/rheed.py, where ~90 lines of camera-source
branching sat inline in main(). It is real configuration logic and it belongs
somewhere importable and testable -- not in a script.

The pylon import is deliberately lazy: the detection box has no Basler camera and
should not need pypylon installed to import this module.
"""

from __future__ import annotations

import logging
import math
import time

import cv2

from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig
from lumi.base.camera.video_stream import VideoCompressor, VideoCompressorConfig
from lumi.base.camera.web_camera import WebCamera, WebCameraConfig
from lumi.config import settings
from lumi.rheed.integrator import MultiBoxIntegrator, MultiBoxIntegratorConfig
from lumi.rheed.livefft import STFTCalculator, STFTCalculatorConfig
from lumi.rheed.sim_frames import DEFAULT_SIM_FRAME
from lumi.rheed.sim_frames import resolve as resolve_sim_frame

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


class GunBeam:
    """The simulated RHEED image's brightness from pld-service's gun, for a twin: no
    beam (0) with the filament or HV off, otherwise the emission current over
    `reference_ua`, the emission the sim frame was taken at. The screen's intensity
    follows the beam current, so it levels off where the emission saturates.

    Until the first reading, and with no RHEED link or no emission reading, the image
    stays as it is (1), as it was before the sim followed the gun."""

    def __init__(self, reference_ua: float = 8.0) -> None:
        self.reference_ua = reference_ua
        self._scale = 1.0

    def update(self, rheed) -> None:
        """`rheed` is a `PldRheed`, or None for no reading."""
        if rheed is None or not rheed.present:
            self._scale = 1.0
        elif not (rheed.filament_on and rheed.hv_on):
            self._scale = 0.0
        elif rheed.emission_ua is None:
            self._scale = 1.0
        else:
            self._scale = max(0.0, rheed.emission_ua / self.reference_ua)

    def scale(self) -> float:
        return self._scale


class GrowthClock:
    """RHEED oscillations on the simulated image from pld-service's deposition, for a
    twin: every pulse fired with the sample shutter open grows 1/`pulses_per_monolayer`
    of a monolayer, and the specular region dims and recovers once per monolayer, the
    dip `depth` deep at first and damped over `decay_ml` monolayers. With no deposition
    the image holds still.

    pld-service is read about once a second; between readings a firing train is
    counted on at its rate, so the trace is smooth rather than a step per poll."""

    def __init__(self, pulses_per_monolayer: float = 40.0, depth: float = 0.4,
                 decay_ml: float = 10.0, clock=time.monotonic) -> None:
        self.pulses_per_monolayer = pulses_per_monolayer
        self.depth = depth
        self.decay_ml = decay_ml
        self._clock = clock
        self._grown = 0.0  # pulses deposited before the present reading
        self._last_fired: int | None = None
        self._open = False
        self._firing = False
        self._rate = 0.0
        self._cap = 0.0  # pulses the present train can still add
        self._at = clock()

    def update(self, state) -> None:
        """`state` is a `PldChamberState`, or None for no reading."""
        if state is None:
            self._firing = False
            return
        laser = state.laser
        fired = laser.pulses_fired
        if self._last_fired is not None and self._open:
            # A count lower than the last one is a new train, counted from 0.
            self._grown += fired - self._last_fired if fired >= self._last_fired else fired
        self._last_fired = fired
        self._open = bool(state.sample_shutter_away)
        self._firing = laser.status == "firing" and laser.rate_hz > 0
        self._rate = laser.rate_hz
        self._cap = max(0.0, laser.pulses_requested - fired) if laser.pulses_requested else float("inf")
        self._at = self._clock()

    def monolayers(self) -> float:
        pulses = self._grown
        if self._open and self._firing:
            pulses += min(self._cap, self._rate * (self._clock() - self._at))
        return pulses / self.pulses_per_monolayer

    def scale(self) -> float:
        ml = self.monolayers()
        if ml <= 0:
            return 1.0
        dip = 0.5 * (1.0 - math.cos(2.0 * math.pi * ml))
        return 1.0 - self.depth * math.exp(-ml / self.decay_ml) * dip


def build_camera(src: str, sim_frame: str | None = None, frame_scale=None,
                 feature_scale=None):
    """Returns (camera, frame_processing, height, width).

    `sim_frame` (simcam only) picks what the simulated camera shows: a name from
    lumi.rheed.sim_frames ("sto", "ysz") or a path. None takes `rheed.simcam.source`.
    `frame_scale` (simcam only): `() -> float` on each frame's brightness, e.g.
    `GunBeam.scale`. `feature_scale` (simcam only): `() -> float` on the feature
    region's (`rheed.simcam.feature_bbox`), e.g. `GrowthClock.scale`; it replaces the
    clock-driven base and feature oscillations, so the image changes only with it.
    """
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
                device=cfg.device,
            ),
            name=cfg.name,
            daemon=True,
        )
        return camera, add_time_stamp, cfg.height, cfg.width

    cfg = settings.rheed.simcam
    source, frame = resolve_sim_frame(sim_frame or cfg.get("source") or DEFAULT_SIM_FRAME)
    log.info("simcam shows %s", frame.description if frame else source)
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
            is_base_oscillation=cfg.is_base_oscillation and feature_scale is None,
            base_oscillation_frequency=cfg.base_oscillation_frequency,
            base_oscillation_amplitude=cfg.base_oscillation_amplitude,
            is_feature_oscillation=cfg.is_feature_oscillation and feature_scale is None,
            feature_bbox=cfg.feature_bbox,
            feature_oscillation_frequency=cfg.feature_oscillation_frequency,
            feature_oscillation_amplitude=cfg.feature_oscillation_amplitude,
            exposure_time=cfg.exposure_time,
            gain=cfg.gain,
            gamma=cfg.gamma,
            max_intensity=cfg.max_intensity,
            frame_scale=frame_scale,
            feature_scale=feature_scale,
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
