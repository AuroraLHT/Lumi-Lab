from pypylon import pylon
from pypylon import genicam
import threading
import queue
import collections
import logging
import time
from dataclasses import dataclass

def get_camera(index=0, device=None):
    if index is not None and device is None:
        tl_factory = pylon.TlFactory.GetInstance()
        if index == 0:
            device = tl_factory.CreateFirstDevice()
        else:
            for i, d in enumerate( tl_factory.EnumerateDevices() ):
                if i == index: 
                    device = d
                    break

    assert device is not None         
    camera = pylon.InstantCamera( device )
    return camera

def list_devices(verbose=True):
    # Create an instant camera object with the camera device found first.
    tl_factory = pylon.TlFactory.GetInstance()
    devices = []
    devices_info = [ ]
    for i, device in enumerate( tl_factory.EnumerateDevices() ):
        devices.append( device )
        if verbose:
            print( f"({i})", device.GetFullName() )

    return devices

@dataclass
class PylonCameraConfig:
    device : pylon.DeviceInfo
    idle_time : float = 1 / 300
    fps : int = 30
    camera_max_num_buffer : int = 15

class PylonCamera(threading.Thread):
    def __init__(self, config:PylonCameraConfig, ident:int) -> None:
        self.camera_io_lock = threading.Lock()

        self.config = config
        self.ident = ident

        self.camera = get_camera(device=config.device)
        self.queue = queue.Queue(maxsize=10)
        self.peek_queue = collections.deque(maxlen=10) # this queue for component that need to get the latest image

        # config setting TODO
        self.camera.MaxNumBuffer.Value = self.config.camera_max_num_buffer

        self.camera.Open()
        self._stop_event = threading.Event()
        self._hold_event = threading.Event()

    def clear(self):
        while not queue.Empty():
            self.queue.get()
        self.peek_queue.clear()

    def has_frame(self):
        with self.camera_io_lock:
            return len(self.frame_queue) > 0

    def get_frame(self):
        # return self.queue.get()
        with self.camera_io_lock:
            if len(self.peek_queue):
                # get the latest frame without popping                
                return self.peek_queue[0]
            return None, None

    def run(self):
        # the camera would run indefinitely without being blocked by anything
        # the top of the queue would always be the latest frame
        self.camera.StartGrabbing(pylon.GrabStrategy_OneByOne)

        while True:
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                time.sleep(self.config.idle_time * 10)
                continue


            if self.camera.WaitForFrameTriggerReady(200, pylon.TimeoutHandling_ThrowException):
                self.camera.ExecuteSoftwareTrigger()

            if self.camera.GetGrabResultWaitObject().Wait(0):
                grabResult = self.camera.RetrieveResult(0, pylon.TimeoutHandling_Return)

                
                content = tuple(grabResult.Array, time.time())

                if not self.queue.full():
                    self.queue.put( content )

                with self.camera_io_lock:
                    self.peek_queue.appendleft( content )

        # Stop the grabbing.
        self.camera.StopGrabbing()
        self.camera.Close()


    def hold(self):
        logging.info(f"Pylon thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"Pylon thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()

            
    def stop(self):
        logging.info(f"Pylon thread ({self.ident}) receives a stop signal")
        self._stop_event.set()

