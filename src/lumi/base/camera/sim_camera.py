import threading
import queue
import collections
import logging
import time
import uuid
import datetime
from dataclasses import dataclass
from typing import Union, Tuple, Optional
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


class SimCamera(GenericCamera):
    # FRAME_HEADER_KEYS = ["time", "uuid", "time_stamp"]
    config : SimCameraConfig

    def __init__(self, config:SimCameraConfig, name:Union[int, str]) -> None:
        super().__init__(config, name)

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


        if self.config.is_base_oscillation:
            frame += np.sin(2* np.pi * self.config.base_oscillation_frequency * time.time()) * self.config.base_oscillation_amplitude
        if self.config.is_feature_oscillation:
            frame[self.config.feature_bbox[1]:self.config.feature_bbox[3], self.config.feature_bbox[0]:self.config.feature_bbox[2]] += np.sin(2* np.pi * self.config.feature_oscillation_frequency * time.time()) * self.config.feature_oscillation_amplitude

        frame = frame.clip(0, self.config.max_intensity)
        
        frame = frame.astype(self.source_img.dtype)

        frame_header = {"time": str(frame_time), "uuid":frame_uuid, "time_stamp":str(datetime.datetime.fromtimestamp(frame_time))}
        content = (frame, frame_header)
        return content
    
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

