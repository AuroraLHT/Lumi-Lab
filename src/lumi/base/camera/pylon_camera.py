from pypylon import pylon
from pypylon import genicam
import threading
import queue
import collections
import logging
import time
import uuid
import datetime
from dataclasses import dataclass
from typing import Union, Tuple, Optional
import numpy as np

from .generic_camera import GenericCamera, GenericCameraConfig

def get_camera(index=0, device=None):
    tl_factory = pylon.TlFactory.GetInstance()
    if index is not None and device is None:
        if index == 0:
            device = tl_factory.CreateFirstDevice()
        else:
            for i, d in enumerate( tl_factory.EnumerateDevices() ):
                if i == index: 
                    device = d
                    break

    assert device is not None         
    camera = pylon.InstantCamera( tl_factory.CreateDevice( device ) )
    return camera

def list_devices(verbose=True):
    # Create an instant camera object with the camera device found first.
    tl_factory = pylon.TlFactory.GetInstance()
    devices = []
    for i, device in enumerate( tl_factory.EnumerateDevices() ):
        devices.append( device )
        if verbose:
            print( f"({i})", device.GetFullName() )

    return devices

@dataclass
class PylonCameraConfig(GenericCameraConfig):
    # idle_time : float = 1 / 300
    # fps : int = 30
    # queue_size : int = 2

    device : pylon.DeviceInfo
    # camera option
    camera_max_num_buffer : int
    exposure_time : float
    gain : float
    gamma : float
    black_level : float
    auto_exposure : bool
    auto_gain : bool
    auto_aoi_intensity : bool
    auto_aoi_whitebalance : int

class PylonCamera(GenericCamera):
    config : PylonCameraConfig
    camera : Optional[pylon.InstantCamera] = None
    # FRAME_HEADER_KEYS = ["time", "uuid", "time_stamp"]

    def __init__(self, config:PylonCameraConfig, name:Union[int, str]) -> None:
        super().__init__(config=config, name=name)
        # self.camera_io_lock = threading.Lock()

        # self.config = config

        # self.queues = {}
        # self.peek_queue = collections.deque(maxlen=10) # this queue for component that need to get the latest image


        # self.camera.Open()
        # self._stop_event = threading.Event()
        # self._hold_event = threading.Event()
    
    def apply_camera_config(self):
        self.camera.MaxNumBuffer.Value = self.config.camera_max_num_buffer
        self.camera.ExposureTimeAbs.Value = self.config.exposure_time
        self.camera.GainRaw.Value = self.config.gain
        self.camera.GammaEnable.Value = True
        self.camera.Gamma.Value = self.config.gamma
        self.camera.BlackLevelRaw.Value = self.config.black_level
        self.camera.ExposureAuto.Value = "Continuous" if self.config.auto_exposure else "Off"
        self.camera.GainAuto.Value = "Continuous" if self.config.auto_gain else "Off"
        
        for aoi in self.camera.AutoFunctionAOISelector.GetSymbolics():
            self.camera.AutoFunctionAOISelector.SetValue(aoi)
            self.camera.AutoFunctionAOIUsageIntensity.SetValue(self.config.auto_aoi_intensity)
            self.camera.AutoFunctionAOIUsageWhiteBalance.SetValue(self.config.auto_aoi_whitebalance)


    def on_initiate(self, config:PylonCameraConfig):
        try:
            self.camera = get_camera(device=config.device)
            self.camera.Open()
            logging.info(f"[PylonCamera] open camera object: {self.camera}")
        except Exception as e:
            logging.error(f"Failed to open camera: {e}")
            self.camera = None
            raise e

    def on_run(self):
        pylon.AcquireContinuousConfiguration().OnOpened(self.camera)
        self.camera.StartGrabbing(pylon.GrabStrategy_UpcomingImage)

        # this would set the camera to run
        if self.camera.WaitForFrameTriggerReady(200, pylon.TimeoutHandling_ThrowException):
            self.camera.ExecuteSoftwareTrigger()

    def on_grab(self):
        grabResult = self.camera.RetrieveResult(5000, pylon.TimeoutHandling_Return)
        frame_time = time.time()
        frame_uuid = str(uuid.uuid4())
        
        frame = grabResult.Array
        if frame.ndim < 2 or frame.size == 0: return None # frame might be empty

        frame_header = {"time": str(frame_time),"uuid":frame_uuid, "time_stamp":str(datetime.datetime.fromtimestamp(frame_time))}
        content = (frame, frame_header)
        return content
    
    def is_camera_open(self):
        return self.camera is not None

    def on_stop(self):
        # Stop the grabbing.
        self.camera.StopGrabbing()

    # def register_queue(self, name):
    #     self.queues[name] = queue.Queue(maxsize=self.config.queue_size)
    #     return self.queues[name]
    
    # def remove_queue(self, name):
    #     return self.queues.pop(name)

    # def clear(self):
    #     for queue in self.queues.values():
    #         while not queue.Empty():
    #             queue.get()
    #     self.peek_queue.clear()

    # def is_new_frame_avaliable(self, frame_uuid):
    #     return frame_uuid != self.current_frame_uuid

    # def has_frame(self):
    #     with self.camera_io_lock:
    #         return len(self.frame_queue) > 0

    # def get_frame(self) -> tuple[np.ndarray | None, dict | None]:
    #     # return self.queue.get()
    #     with self.camera_io_lock:
    #         if len(self.peek_queue):
    #             # get the latest frame without popping                
    #             return self.peek_queue[0]
    #         return None, None

    # def run(self):
    #     # the camera would run indefinitely without being blocked by anything
    #     # the top of the queue would always be the latest frame
    #     self.camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
    #     self.camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
    #     # continuous mode

    #     pylon.AcquireContinuousConfiguration().OnOpened(self.camera)
    #     self.camera.StartGrabbing(pylon.GrabStrategy_UpcomingImage)

    #     # this would set the camera to run
    #     if self.camera.WaitForFrameTriggerReady(200, pylon.TimeoutHandling_ThrowException):
    #         self.camera.ExecuteSoftwareTrigger()

    #     while True:
    #         stop_flag = self._stop_event.wait(self.config.idle_time)
    #         if stop_flag : break

    #         hold_flag = self._hold_event.wait(self.config.idle_time)
    #         if hold_flag : 
    #             time.sleep(self.config.idle_time * 10)
    #             continue

    #         if self.camera.GetGrabResultWaitObject().Wait(0):
    #         grabResult = self.camera.RetrieveResult(5000, pylon.TimeoutHandling_Return)
    #         frame_time = time.time()
    #         frame_uuid = str(uuid.uuid4())
            
    #         frame = grabResult.Array
    #         if frame.ndim < 2 or frame.size == 0: continue # frame might be empty

    #         frame_header = {"time": str(frame_time),"uuid":frame_uuid, "time_stamp":str(datetime.datetime.fromtimestamp(frame_time))}
    #         content = (frame, frame_header)

    #         print(content[0].dtype, content[0].shape)

    #         for name, queue in self.queues.items():
    #             if not queue.full():
    #                 queue.put( content )

    #         with self.camera_io_lock:
    #             self.peek_queue.appendleft( content )
    #             self.current_frame_uuid = frame_uuid

    #     # Stop the grabbing.
    #     self.camera.StopGrabbing()

    # def hold(self):
    #     logging.info(f"Pylon thread ({self.ident}) receives a hold signal")
    #     self._hold_event.set()

    # def resume(self):
    #     logging.info(f"Pylon thread ({self.ident}) receives a resume signal")
    #     self._hold_event.clear()

    # def stop(self):
    #     logging.info(f"Pylon thread ({self.ident}) receives a stop signal")
    #     self._stop_event.set()

    # @property
    # def frame_dims(self):
    #     return self.config.frame_dims
    
    # @property
    # def frame_metas(self):
    #     return self.FRAME_HEADER_KEYS

    def __del__(self):
        self.camera.Close()
