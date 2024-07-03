from collections import defaultdict
import PIL
import json
import copy

import time
from dataclasses import dataclass
from pathlib import Path
from tqdm.notebook import tqdm

import torch
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# import cv2
import mmcv
from mmcv.transforms import Compose

import mmengine
from mmengine import Config
from mmengine.utils import track_iter_progress
# this import take a lot of time
# from mmengine import runner`

# https://github.com/open-mmlab/mmdetection
from mmdet.registry import VISUALIZERS
from mmdet.apis import init_detector, inference_detector

import skimage.exposure as exposure

from rhana.io.tokyo_u import RHEEDStreamReader
# from rhana.labeler.unet import rle_decode_arr
from rhana.pattern import Rheed

# Instance Segmentation
from rhana.labeler.detector import CascadeMaskRCNNDetectorAndClassifier
from rhana.pattern import RheedInstanceSegmentation

# tracking
from rhana.tracker.iou_tracker import IOUTracker, regions2detections, IOUMaskTracker
from rhana.periodicity import PeriodicityAnalyzer
from rhana.tracker.periodicity_tracker import PeriodicityTracker

# some tools 
from rhana.tools.restoration import suggest_restoration
from rhana.tools.beam import get_metas, get_deposition_window, plot_metas

import threading
from queue import Queue

from typing import Union, Optional, List, Dict
import logging

MODEL_FOLDER = Path("/home/hliang16/projects/LnFeO_TokyoU/nn/cascade_maskrcnn_exps")
MODEL_DEVICE = "cuda:3"

@dataclass
class DetectorConfig:
    input_queue_size : int = 10
    output_queue_size : int = 10
    model_folder : Optional[str] = None
    model_device : Optional[str] = None
    idle_time : float = 0.1

    def __post_init__(self):
        self.model_folder = MODEL_FOLDER if self.model_folder is None else self.model_folder
        self.model_device = MODEL_DEVICE if self.model_device is None else self.model_device

def get_model(model_folder, device):

    aux_detector = CascadeMaskRCNNDetectorAndClassifier(
        config_path = str(model_folder / "config_predict.py"),
        checkpoint_path = str(model_folder / "epoch_12.pth"),
        classifier_config_path = str(model_folder / "classifier/config.json"),
        classifier_checkpoint_path = str(model_folder / "classifier/model.pth"),
        device = device,
    )
    return aux_detector

def predict(frame, aux_detector=None):
    if frame.ndim == 3:
        frame = frame[..., 0]
        
    rd = Rheed(np.array(frame), True)
    result, cls_result = aux_detector.predict(rd)
    rdinst = RheedInstanceSegmentation.from_mmdet(rd, result, aux_detector.model, auto_compute_regions=True)

    classification = { aux_detector.classifier_classes[i] : float(cls_result[0][i]) for i in range(len(cls_result[0]))}

    instances = result.pred_instances

    bbox_outputs = {}                
    for i in range(len(instances.bboxes)):
        bbox_outputs[f"{i}"] = {
            "score":instances.scores[i].cpu().tolist(),
            "label":instances.labels[i].cpu().tolist(),
            # "mask":np.array(instances.masks[i].cpu()),
            "bbox":instances.bboxes[i].cpu().tolist(),
        }

    return {'bboxes':bbox_outputs, 'classification':classification, 'mmdet_result':result, 'instance_segementation':rdinst}

        
class DetectorServer(threading.Thread):

    def __init__(self, config:DetectorConfig, name:Union[int, str] ) -> None:        
        super().__init__(name=name)
        self.camera_io_lock = threading.Lock()
        self.config = config

        self.input_queue = Queue(maxsize=config.input_queue_size)
        self.output_queue = Queue(maxsize=config.output_queue_size)

        self._stop_event = threading.Event()

        self.on_initiate()


    def predict(self, frame, frame_headers={}):
        self.input_queue.put( (frame, frame_headers) )
        return self.output_queue.get()

    def _predict(self, frame):
        return predict(frame, self.aux_detector)


    def on_initiate(self):
        self.aux_detector = get_model(model_folder=self.config.model_folder, device=self.config.model_device)

    
    def on_stop(self):
        # raise NotImplementedError
        pass

    def is_continue(self):
        """
            default to True if not overwrited
        """
        stop_flag = self._stop_event.wait(self.config.idle_time)
        if stop_flag : return False

        return True


    def clear(self):
        while not self.input_queue.Empty():
            self.input_queue.get()
        while not self.output_queue.Empty():
            self.output_queue.get()
        

    def run(self):
        # the camera would run indefinitely without being blocked by anything
        # the top of the queue would always be the latest frame
        while self.is_continue():
            logging.info("Detection Loop start")
            frame, headers = self.input_queue.get()
            logging.info("Detection Input Acquired")

            result = self._predict(frame)
            logging.info("Detection")

            self.output_queue.put({"input": (frame, headers), **result })
            logging.info("Detection Output Inserted")

        self.on_stop()

            
    def stop(self):
        logging.info(f"Camera thread ({self.ident}) receives a stop signal")
        self._stop_event.set()