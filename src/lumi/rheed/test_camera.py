import threading
import queue
import collections
import logging
import time
import uuid
import datetime
from dataclasses import dataclass
from typing import Union, Tuple, Optional
# import cv2
import numpy as np

@dataclass
class TestCameraConfig:
    # camera option
    frame_dims : Tuple[int, int]
    source : str

    idle_time : float = 1 / 300
    fps : int = 30
    queue_size : int = 2



class TestCamera(threading.Thread):
    FRAME_HEADER_KEYS = ["time", "uuid", "time_stamp"]

    def __init__(self, config:TestCameraConfig, name:Union[int, str]) -> None:
        super().__init__(name=name)
        self.camera_io_lock = threading.Lock()

        self.config = config

        # self.source_img = cv2.imread(config.source)
        self.source_img : np.ndarray = np.load(config.source)
        self.queues = {}
        self.peek_queue = collections.deque(maxlen=10) # this queue for component that need to get the latest image

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

    def get_frame(self):
        # return self.queue.get()
        with self.camera_io_lock:
            if len(self.peek_queue):
                # get the latest frame without popping                
                return self.peek_queue[0]
            return None, None

    def run(self):
        prev_time = 0
        spf = 1 / self.config.fps
        while True:
            curr_time = time.time()
            if curr_time - prev_time < spf: 
                time.sleep(self.config.idle_time)
                continue
            else:
                prev_time = curr_time

            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : 
                logging.info(f"Test camera ({self.ident}) exits the run loop")
                break

            hold_flag = self._hold_event.wait(self.config.idle_time)
            if hold_flag : 
                time.sleep(self.config.idle_time * 10)
                continue

            # if self.camera.GetGrabResultWaitObject().Wait(0):
            frame_time = time.time()
            frame_uuid = str(uuid.uuid4())
            
            # so the frame would oscillate the brighness now
            frame : np.ndarray = self.source_img.copy() * (np.sin(2* np.pi * 1/10 * time.time()) + 1) / 2
            frame = frame.astype(self.source_img.dtype)

            if frame.ndim < 2 or frame.size == 0: continue # frame might be empty

            frame_header = {"time": str(frame_time),"uuid":frame_uuid, "time_stamp":str(datetime.datetime.fromtimestamp(frame_time))}
            content = (frame, frame_header)

            for name, queue in self.queues.items():
                if not queue.full():
                    # print(frame_time)
                    queue.put( content )

            with self.camera_io_lock:
                self.peek_queue.appendleft( content )
                self.current_frame_uuid = frame_uuid


    def hold(self):
        logging.info(f"Test camera thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"Test camera thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()

    def stop(self):
        logging.info(f"Test camera thread ({self.ident}) receives a stop signal")
        self._stop_event.set()

    @property
    def frame_dims(self):
        return self.config.frame_dims
    
    @property
    def frame_metas(self):
        return self.FRAME_HEADER_KEYS

    def __del__(self):
        pass
