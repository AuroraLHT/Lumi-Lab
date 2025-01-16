import threading
import queue
import collections
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Union, Optional, Tuple

@dataclass
class GenericCameraConfig:
    fps : int 
    idle_time : float
    queue_size : int
    frame_dims : Tuple[int, int]

    @property
    def spf(self):
        return 1 / self.fps

    def __post_init__(self):
        if self.idle_time is None:
            self.idle_time = self.spf / 50



class GenericCamera(threading.Thread):
    FRAME_HEADER_KEYS = ["time", "uuid", "time_stamp"]
    """
        This is a generic camera class that can be used to create a camera object
        The camera object should be implement the following methods:
            on_initiate(self, config:GenericCameraConfig)
            on_run(self)
            on_grab(self)
            on_stop(self)
            Optionally, __del__(self)
    """

    def __init__(self, config:GenericCameraConfig, name:Union[int, str] ) -> None:        
        super().__init__(name=name)
        self.camera_io_lock = threading.Lock()
        self.config = config

        self.queues = {}
        self.peek_queue = collections.deque(maxlen=10) # this queue for component that need to get the latest image

        self._stop_event = threading.Event()
        self._hold_event = threading.Event()

        self.on_initiate(self.config)

    def on_initiate(self, config:GenericCameraConfig):
        raise NotImplementedError

    def on_run(self):
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

    def register_queue(self, name):
        self.queues[name] = queue.Queue(maxsize=self.config.queue_size)
        return self.queues[name]

    def clear(self):
        for queue in self.queues.values():
            while not queue.Empty():
                queue.get()
        self.peek_queue.clear()

    def is_new_frame_avaliable(self, frame_uuid):
        return frame_uuid != self.current_frame_uuid

    def has_frame(self):
        with self.camera_io_lock:
            return len(self.peek_queue) > 0

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
        self.on_run()

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
                if content is None:
                    continue

                grab_time = time.time()
                # fps = 1/(grab_time - prev_grab_time)
                # logging.info(f"fps {fps:.3f} desire fps {self.config.fps}")
                prev_grab_time = grab_time

                for name, queue in self.queues.items():
                    if not queue.full():
                        queue.put( content )

                with self.camera_io_lock:
                    self.peek_queue.appendleft( content )
                    self.current_frame_uuid = content[1]['uuid'] # update the peek queue status

        self.on_stop()

    def hold(self):
        logging.info(f"{self.name} thread ({self.ident}) receives a hold signal")
        self._hold_event.set()

    def resume(self):
        logging.info(f"{self.name} thread ({self.ident}) receives a resume signal")
        self._hold_event.clear()
            
    def stop(self):
        logging.info(f"{self.name} thread ({self.ident}) receives a stop signal")
        self._stop_event.set()

    @property
    def frame_dims(self):
        return self.config.frame_dims
    
    @property
    def frame_metas(self):
        return self.FRAME_HEADER_KEYS

    def __del__(self):
        pass

