import time
import asyncio
import threading
import queue
from collections import deque
import fractions
import datetime
import logging
import uuid
import copy

import numpy as np

from dataclasses import dataclass
from typing import Generator, Tuple, Union, List, Optional, Dict, Deque
from collections.abc import Callable, Awaitable
from .types import IntegrationResult, IntegrationCollection

import json

@dataclass
class MultiBoxIntegratorConfig:
    idle_time : float = None
    output_queue_size : int = 50
    
    def __post_init__(self):

        if self.idle_time is None:
            self.idle_time = self.spf / 10


class MultiBoxIntegrator(threading.Thread):
    HEADER_KEYS = ["time", "uuid", "time_stamp", "bbox_id"]

    def __init__(self, camera, camera_queue:queue.Queue, config:MultiBoxIntegratorConfig, name:Union[int|str]="", daemon:bool=True):
        super().__init__(name=name, daemon=daemon)
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
        try:
            bbox = np.array(bbox).round().astype(int).tolist()
            self.bboxes[bbox_id] = bbox
            self.bboxes_integration_cache[bbox_id] = deque([], maxlen=1000)
        except Exception as e:
            logging.error(f"Failed to register bbox: {e}. Bbox: {bbox}, Bbox ID: {bbox_id}")
            raise e

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

    def compute_integration(self, image, bbox) -> IntegrationResult:
        try:
            aoi = image[bbox[1]:bbox[3], bbox[0]:bbox[2]]
            return IntegrationResult(
                mean=float(np.mean(aoi)), 
                max=float(np.max(aoi)), 
                min=float(np.min(aoi)), 
                height=int(bbox[2]-bbox[0]), 
                width=int(bbox[3]-bbox[1]), 
                center_x=float((bbox[0]+bbox[2])/2), 
                center_y=float((bbox[1]+bbox[3])/2)
            )
        except Exception as e:
            logging.error(f"Failed to compute integration: {e}. Bbox: {bbox}, Image shape: {image.shape}")
            raise e

    def prepare_content(self, integration, header) -> Tuple[IntegrationCollection, Dict]:
        return ( integration, header )

    def yield_integration(self) -> Generator[Tuple[IntegrationCollection, Dict], None, None]:
        logging.info("start bbox integration")

        while True:
            if not self.camera_queue.empty() and len(self.bboxes) > 0:
                integrations : IntegrationCollection = {}
                cv_frame, cv_frame_header = self.get_image(timeout=60)
                bboxes = copy.deepcopy(self.bboxes) # bbox might change when iterating the bbox items
                for bbox_id, bbox in bboxes.items():
                    integration = self.compute_integration(cv_frame, bbox)
                    headers = {**cv_frame_header, "bbox_id": bbox_id}
                    single_content = self.prepare_content(integration, headers)
                    self.bboxes_integration_cache[bbox_id].append( single_content )
                    integrations[bbox_id] = integration
                    # yield content

                self.state["processed_frame_uuid"] = cv_frame_header["uuid"]

                content = self.prepare_content( integrations, {**cv_frame_header, "integration_uuid": str(uuid.uuid4()) } )
                # print("integration_uuid", content[1]["integration_uuid"])
                yield content

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
            # print(content)

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
            integration_time = np.array([float(x[1]["time"]) for x in cache])
            integration = np.array([x[0]["mean"] for x in cache])
            latest_header = cache[-1][1]
            return integration_time, integration, latest_header
        else:
            return None, None, None

    def stop(self):
        logging.info(f"Live Integrator thread receives a stop signal")

        # clear the queue
        self.clear()
        self._stop_event.set()

