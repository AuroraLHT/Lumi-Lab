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

def list_devices(verbose=True):
    """
        now only works for linux
    """
    devices = [] 
    for device in Path("/dev").glob("video*"):
        if verbose: print(device)
        devices.append(device)

    return devices

@dataclass
class GenericCameraConfig:
    fps : int = 30
    idle_time : float =  None
    queue_size : int = 2

    @property
    def spf(self):
        return 1 / self.fps

    def __post_init__(self):
        if self.idle_time is None:
            self.idle_time = self.spf / 50


class WebCameraConfig(GenericCameraConfig):
    device : int = 0

class GenericCamera(threading.Thread):
    def __init__(self, config:GenericCameraConfig, name:Union[int, str] ) -> None:        
        super().__init__(name=name)
        self.camera_io_lock = threading.Lock()
        self.config = config

        self.queue = queue.Queue(maxsize=config.queue_size)
        self.peek_queue = collections.deque(maxlen=config.queue_size) # this queue for component that need to get the latest image

        self._stop_event = threading.Event()
        self._hold_event = threading.Event()

        self.on_initiate(self.config)

    def on_initiate(self):
        raise NotImplementedError
    
    def on_grab(self):
        raise NotImplementedError


    def on_stop(self):
        raise NotImplementedError

    def is_camera_open(self):
        """
            default to True if not overwrited
        """
        return True


    def clear(self):
        while not queue.Empty():
            self.queue.get()
        self.peek_queue.clear()

    def has_frame(self):
        with self.camera_io_lock:
            return len(self.frame_queue) > 0

    def is_new_frame_avaliable(self, frame_uuid):
        return frame_uuid != self.current_frame_uuid
    
    def get_frame(self):
        # return self.queue.get()
        with self.camera_io_lock:
            if len(self.peek_queue):
                # get the latest frame without popping                
                return self.peek_queue[0]
            return (None, None)

    def run(self):
        # the camera would run indefinitely without being blocked by anything
        # the top of the queue would always be the latest frame
        prev_grab_time = time.time()
        while self.is_camera_open():
            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                time.sleep(self.config.idle_time * 10)
                continue

            if self.config.spf < time.time() - prev_grab_time:
                content = self.on_grab()

                grab_time = time.time()
                # fps = 1/(grab_time - prev_grab_time)
                # logging.info(f"fps {fps:.3f} desire fps {self.config.fps}")
                prev_grab_time = grab_time

                if not self.queue.full():
                    self.queue.put( content )

                with self.camera_io_lock:
                    self.peek_queue.appendleft( content )
                    self.current_frame_uuid = content[1]['uuid'] # update the peek queue status

        self.on_stop()

    def hold(self):
        logging.info(f"Camera thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"Camera thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()
            
    def stop(self):
        logging.info(f"Camera thread ({self.ident}) receives a stop signal")
        self._stop_event.set()



class WebCamera(GenericCamera):
    def __init__(self, config:WebCameraConfig, name:Union[int|str]) -> None:
        super().__init__(config=config, name=name)
        
    def on_initiate(self, config:WebCameraConfig):
        self.capture = cv2.VideoCapture(config.device)
        self.capture.set(cv2.CAP_PROP_FPS, config.fps)


    def on_grab(self):
        """
        content is a bundle of frame and frame header
        """
        ret, frame = self.capture.read()
        # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_time = time.time()
        frame_time_stamp = str(datetime.datetime.fromtimestamp(frame_time))
        content = (frame, {'time':frame_time, 'time_stamp':frame_time_stamp, 'uuid':str(uuid.uuid4())})
        
        return content
        
    def is_camera_open(self):
        if hasattr(self, "capture"):
            return self.capture.isOpened()
        return False
    
    def on_stop(self):
        if hasattr(self, "capture"):
            self.capture.release()
