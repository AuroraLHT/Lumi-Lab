import time
import asyncio
import threading
import queue
from collections import deque
import fractions
import datetime
import logging

import numpy as np

from dataclasses import dataclass

from typing import Union, List, Optional, Dict, Deque
from collections.abc import Callable, Awaitable

import json

@dataclass
class MultiBoxIntegratorConfig:
    idle_time : float = None
    output_queue_size : int = 1000
    
    def __post_init__(self):

        if self.idle_time is None:
            self.idle_time = self.spf / 10


class MultiBoxIntegrator(threading.Thread):
    HEADER_KEYS = ["time", "uuid", "time_stamp", "bbox_id"]

    def __init__(self, camera, camera_queue:queue.Queue, config:MultiBoxIntegratorConfig, name:Union[int|str]=""):
        super().__init__(name=name)
        self.io_lock = threading.Lock()
        self.config = config
        self.output_queue = queue.Queue(maxsize=self.config.output_queue_size)
        self.camera = camera
        self.camera_queue = camera_queue
        self._stop_event = threading.Event()
        self.bboxes = {} # {bbox_id: (bbox_sx, bbox_sy, bbox_ex, bbox_ey)}
        self.bboxes_integration_cache : Dict[Union[str, int], Deque] = {} 

        self.state = {"processed_frame_uuid": None}

    def register_bbox(self, bbox, bbox_id):
        self.bboxes[bbox_id] = bbox
        self.bboxes_integration_cache[bbox_id] = deque([], maxlen=1000)

    def remove_bbox(self, bbox_id):
        if bbox_id in self.bboxes:  
            self.bboxes.pop(bbox_id)
        if bbox_id in self.bboxes_integration_cache:
            self.bboxes_integration_cache.pop(bbox_id)
        
    def get_image(self, timeout=None):
        # extract camera frame
        # print(f"is queue full {self.camera_queue.full()} ")
        cv_frame, cv_frame_header = self.camera_queue.get(timeout=timeout)
        
        return cv_frame, cv_frame_header

    def compute_integration(self, image, bbox):
        aoi = image[bbox[1]:bbox[3], bbox[0]:bbox[2]]
        return {"mean": np.mean(aoi), "max": np.max(aoi), "min": np.min(aoi), "width": bbox[2]-bbox[0], "height": bbox[3]-bbox[1]}

    def prepare_content(self, integration, header):
        return ( integration, header )

    def yield_integration(self):
        logging.info("start bbox integration")

        while True:
            if not self.camera_queue.empty():
                cv_frame, cv_frame_header = self.get_image(timeout=60)
                for bbox_id, bbox in self.bboxes.items():
                    integration = self.compute_integration(cv_frame, bbox)
                    
                    content = self.prepare_content( integration, {"bbox_id": bbox_id, **cv_frame_header})
                    self.bboxes_integration_cache[bbox_id].append( content )

                    yield content
                self.state["processed_frame_uuid"] = cv_frame_header["uuid"]

            stop_flag = self._stop_event.wait(self.config.idle_time)
            if stop_flag : 
                logging.info("exit thread while loop")
                break   


        # Finalize the container
        logging.info('close bbox integration yield')

    def is_updated(self, prev_frame_uuid):
        """
        check if the given prev_frame_uuid matched with the current processed frame uuid.
        True if not matched, False otherwise
        """
        return self.state["processed_frame_uuid"] != prev_frame_uuid

    def get_processed_frame_uuid(self):
        return self.state["processed_frame_uuid"]

    def run(self):
        for content in self.yield_integration():
            self.output_queue.put(content)

    def clear(self):
        while not self.output_queue.empty():
            self.output_queue.get()
        self.bboxes_integration_cache = {}
        self.bboxes = {}

    def get_integration_cache(self, bbox_id):
        if bbox_id in self.bboxes_integration_cache:
            return self.bboxes_integration_cache[bbox_id]
        else:
            return None
        
    def get_integration_history(self, bbox_id):
        cache = self.get_integration_cache(bbox_id)
        if cache is not None:
            integration_time = np.array([x[1]["time"] for x in cache])
            integration = np.array([x[0]["mean"] for x in cache])
            return integration_time, integration
        else:
            return None

    def stop(self):
        logging.info(f"Live Integrator thread receives a stop signal")

        # clear the queue
        self.clear()
        self._stop_event.set()

