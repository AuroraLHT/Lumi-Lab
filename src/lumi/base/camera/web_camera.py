import cv2
import threading
import queue
import collections
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Union
import uuid
import datetime

from .generic_camera import GenericCamera, GenericCameraConfig
from typing import Tuple

def list_devices(verbose=True):
    """
        now only works for linux
    """
    devices = [] 
    for device in Path("/dev").glob("video*"):
        if verbose: print(device)
        devices.append(device)

    return devices


class WebCameraConfig(GenericCameraConfig):
    device : Union[int, str]

class WebCamera(GenericCamera):
    def __init__(self, config:WebCameraConfig, name:Union[int|str]) -> None:
        super().__init__(config=config, name=name)
        
    def on_initiate(self, config:WebCameraConfig):
        self.capture = cv2.VideoCapture(config.device)
        self.capture.set(cv2.CAP_PROP_FPS, config.fps)
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, config.frame_dims[0])
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, config.frame_dims[1])

    def on_grab(self):
        """
        content is a bundle of frame and frame header
        """
        ret, frame = self.capture.read()
        # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_time = time.time()
        frame_time_stamp = str(datetime.datetime.fromtimestamp(frame_time))
        content = (frame, {'time':str(frame_time), 'time_stamp':frame_time_stamp, 'uuid':str(uuid.uuid4())})
        
        return content
        
    def is_camera_open(self):
        if hasattr(self, "capture"):
            return self.capture.isOpened()
        return False
    
    def on_stop(self):
        if hasattr(self, "capture"):
            self.capture.release()
