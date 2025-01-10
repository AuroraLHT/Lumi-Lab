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
class PylonCameraConfig:
    device : pylon.DeviceInfo
    # camera option
    frame_dims : Tuple[int, int] 

    idle_time : float = 1 / 300
    fps : int = 30
    camera_max_num_buffer : int = 15
    queue_size : int = 2



class PylonCamera(threading.Thread):
    FRAME_HEADER_KEYS = ["time", "uuid", "time_stamp"]

    def __init__(self, config:PylonCameraConfig, name:Union[int, str]) -> None:
        super().__init__(name=name)
        self.camera_io_lock = threading.Lock()

        self.config = config

        self.camera = get_camera(device=config.device)
        self.queues = {}
        self.peek_queue = collections.deque(maxlen=10) # this queue for component that need to get the latest image

        # config setting TODO
        self.camera.MaxNumBuffer.Value = self.config.camera_max_num_buffer

        self.camera.Open()
        self._stop_event = threading.Event()
        self._hold_event = threading.Event()

    def register_queue(self, name):
        self.queues[name] = queue.Queue(maxsize=self.config.queue_size)
        return self.queues[name]
    
    def remove_queue(self, name):
        return self.queues.pop(name)

    def clear(self):
        while not queue.Empty():
            self.queue.get()
        self.peek_queue.clear()

    def is_new_frame_avaliable(self, frame_uuid):
        return frame_uuid != self.current_frame_uuid

    def has_frame(self):
        with self.camera_io_lock:
            return len(self.frame_queue) > 0

    def get_frame(self) -> tuple[np.ndarray | None, dict | None]:
        # return self.queue.get()
        with self.camera_io_lock:
            if len(self.peek_queue):
                # get the latest frame without popping                
                return self.peek_queue[0]
            return None, None

    def run(self):
        # the camera would run indefinitely without being blocked by anything
        # the top of the queue would always be the latest frame
        # self.camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        # self.camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        # continuous mode

        pylon.AcquireContinuousConfiguration().OnOpened(self.camera)
        self.camera.StartGrabbing(pylon.GrabStrategy_UpcomingImage)

        # this would set the camera to run
        if self.camera.WaitForFrameTriggerReady(200, pylon.TimeoutHandling_ThrowException):
            self.camera.ExecuteSoftwareTrigger()

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                time.sleep(self.config.idle_time * 10)
                continue

            # if self.camera.GetGrabResultWaitObject().Wait(0):
            grabResult = self.camera.RetrieveResult(5000, pylon.TimeoutHandling_Return)
            frame_time = time.time()
            frame_uuid = str(uuid.uuid4())
            
            frame = grabResult.Array
            if frame.ndim < 2 or frame.size == 0: continue # frame might be empty

            frame_header = {"time": str(frame_time),"uuid":frame_uuid, "time_stamp":str(datetime.datetime.fromtimestamp(frame_time))}
            content = (frame, frame_header)

            # print(content[0].dtype, content[0].shape)

            for name, queue in self.queues.items():
                if not queue.full():
                    queue.put( content )

            with self.camera_io_lock:
                self.peek_queue.appendleft( content )
                self.current_frame_uuid = frame_uuid

        # Stop the grabbing.
        self.camera.StopGrabbing()

    def hold(self):
        logging.info(f"Pylon thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"Pylon thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()

    def stop(self):
        logging.info(f"Pylon thread ({self.ident}) receives a stop signal")
        self._stop_event.set()

    @property
    def frame_dims(self):
        return self.config.frame_dims
    
    @property
    def frame_metas(self):
        return self.FRAME_HEADER_KEYS

    def __del__(self):
        self.camera.Close()
