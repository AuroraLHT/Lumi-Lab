import threading
import queue
import collections
import logging
import time
import uuid
import datetime
from dataclasses import dataclass
from typing import Callable, Optional, Union, Tuple
import cv2
import numpy as np


from .generic_camera import GenericCamera, GenericCameraConfig

"""
    This is a test camera that is used to simulate the RHEED camera module.
    It is used to test the camera module and the camera component.
"""

@dataclass
class SimCameraConfig(GenericCameraConfig):
    # camera option
    source : str
    is_base_oscillation : bool
    base_oscillation_frequency : float
    base_oscillation_amplitude : float

    is_feature_oscillation : bool
    feature_bbox : Tuple[int, int, int, int]
    feature_oscillation_frequency : float
    feature_oscillation_amplitude : float

    max_intensity : int

    # from pylon camera
    exposure_time : float
    gain : float
    gamma : float

    # Optional per-frame scene render, applied to the scaled source image before the
    # oscillation effects below. None (the default) leaves the source image as-is --
    # used by Pascal's chamber camera to draw the sample rotation and the Mask1-driven
    # slit mask over the static asset; nothing else sets it.
    frame_transform : Optional[Callable[[np.ndarray], np.ndarray]] = None
    # Optional brightness factor on the whole frame, oscillations included, read per
    # frame: the RHEED sim's beam following the gun (lumi.rheed.hardware.GunBeam; 0 =
    # no beam). None leaves it at 1.
    frame_scale : Optional[Callable[[], float]] = None
    # Optional brightness factor on the image's diffraction features, read per frame:
    # the RHEED sim's growth oscillations (lumi.rheed.hardware.GrowthClock). It is
    # weighted by a soft mask of the source image's own bright spots and streaks, so
    # the background between them stays put. None leaves it at 1.
    feature_scale : Optional[Callable[[], float]] = None
    # Camera noise on each frame, after the brightness: read noise (rms, counts),
    # shot noise (variance = noise_shot x signal) and a beam flicker (relative rms of
    # the whole frame). 0 = none, every frame identical.
    noise_read : float = 0.0
    noise_shot : float = 0.0
    noise_flicker : float = 0.0


class SimCamera(GenericCamera):
    # FRAME_HEADER_KEYS = ["time", "uuid", "time_stamp"]
    config : SimCameraConfig

    def __init__(self, config:SimCameraConfig, name:Union[int, str], daemon:bool=True) -> None:
        super().__init__(config, name, daemon=daemon)

    def on_initiate(self, config:SimCameraConfig):
        # self.source_img = cv2.imread(config.source)
        if config.source.endswith(".npy"):
            self.source_img : np.ndarray = np.load(config.source)
        else:
            self.source_img : np.ndarray = cv2.imread(config.source)
            self.source_img = cv2.cvtColor(self.source_img, cv2.COLOR_BGR2RGB)

        self._base_exposure_time = config.exposure_time
        self._exposure_scale = 1
        self._base_gain = config.gain
        self._gain_scale = 1
        self._base_gamma = config.gamma
        self._gamma_scale = 1


    def on_run(self):
        pass

    def on_grab(self):
        # if self.camera.GetGrabResultWaitObject().Wait(0):
        frame_time = time.time()
        frame_uuid = str(uuid.uuid4())
        
        # so the frame would oscillate the brighness now
        frame : np.ndarray = self.source_img.copy() * self._exposure_scale * self._gain_scale * self._gamma_scale
        if frame.ndim < 2 or frame.size == 0: return None # frame might be empty

        if self.config.frame_transform is not None:
            frame = self.config.frame_transform(frame)

        if self.config.is_base_oscillation:
            frame += np.sin(2* np.pi * self.config.base_oscillation_frequency * time.time()) * self.config.base_oscillation_amplitude
        if self.config.is_feature_oscillation:
            frame[self.config.feature_bbox[1]:self.config.feature_bbox[3], self.config.feature_bbox[0]:self.config.feature_bbox[2]] += np.sin(2* np.pi * self.config.feature_oscillation_frequency * time.time()) * self.config.feature_oscillation_amplitude

        if self.config.feature_scale is not None:
            dim = 1.0 - self.config.feature_scale()
            if dim:
                frame = frame * (1.0 - dim * self.feature_mask())
        if self.config.frame_scale is not None:
            frame = frame * self.config.frame_scale()
        frame = self.add_noise(frame)

        frame = frame.clip(0, self.config.max_intensity)
        
        frame = frame.astype(self.source_img.dtype)

        frame_header = {"time": str(frame_time), "uuid":frame_uuid, "time_stamp":str(datetime.datetime.fromtimestamp(frame_time))}
        content = (frame, frame_header)
        return content
    
    def full_scale(self) -> int | None:
        return int(self.config.max_intensity)

    def feature_mask(self) -> np.ndarray:
        """0 on the background, 1 on the brightest spots, soft-edged: the source image
        above its median, over its top, blurred. Made once, from the source image."""
        mask = getattr(self, "_feature_mask", None)
        if mask is None:
            src = self.source_img.astype(np.float32)
            gray = src.mean(axis=-1) if src.ndim == 3 else src
            low, high = np.median(gray), np.percentile(gray, 99.5)
            mask = np.clip((gray - low) / max(float(high - low), 1e-6), 0.0, 1.0)
            mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=4.0)
            mask /= max(float(mask.max()), 1e-6)
            if src.ndim == 3:
                mask = mask[..., None]
            self._feature_mask = mask
        return mask

    def add_noise(self, frame: np.ndarray) -> np.ndarray:
        cfg = self.config
        if not (cfg.noise_read or cfg.noise_shot or cfg.noise_flicker):
            return frame
        rng = getattr(self, "_rng", None)
        if rng is None:
            rng = self._rng = np.random.default_rng()
        frame = frame.astype(np.float32, copy=False)
        if cfg.noise_flicker:
            frame = frame * float(1.0 + cfg.noise_flicker * rng.standard_normal())
        if cfg.noise_read or cfg.noise_shot:
            sigma = np.sqrt(cfg.noise_read ** 2 + cfg.noise_shot * np.maximum(frame, 0.0))
            frame = frame + sigma * rng.standard_normal(frame.shape, dtype=np.float32)
        return frame

    def on_stop(self):
        pass


    def apply_camera_config(self):
        logging.info("Applying camera config")
        try:
            self._exposure_scale = self.config.exposure_time / self._base_exposure_time
            self._gain_scale = np.exp(self.config.gain / self._base_gain - 1)
            # TODO: add gamma effect
            self._gamma_scale = 1 # self.config.gamma / self._base_gamma

            return True, None
        except Exception as e:
            logging.warning(f"Apply camera config fail: {e}")

            return False, e

