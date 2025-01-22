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

from typing import Union, Optional, List, Dict, Tuple
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

@dataclass
class DetectorState:
    horizontal_center : int
    pattern_dims : Tuple[int, int]
    crop_setup : Dict
    db_track : object
    # add more

def get_model(model_folder, device):
    model_folder = Path(model_folder)
    aux_detector = CascadeMaskRCNNDetectorAndClassifier(
        config_path = str(model_folder / "config_predict.py"),
        checkpoint_path = str(model_folder / "epoch_12.pth"),
        classifier_config_path = str(model_folder / "classifier/config.json"),
        classifier_checkpoint_path = str(model_folder / "classifier/model.pth"),
        device = device,
    )
    return aux_detector


class DetectorServer(threading.Thread):
    DETECTION_METAS = ["time_stamp", "uuid", "time", "crop_setup_sx", "crop_setup_sy", "crop_setup_ex", "crop_setup_ey"]

    def __init__(self, config:DetectorConfig, name:Union[int, str], daemon:bool=True ) -> None:        
        super().__init__(name=name, daemon=daemon)
        self.config = config
        # TODO: may we initialize it with some config or some cache
        self.state = DetectorState(None, None, {"sx":60, "sy":0, "ex":500, "ey":720}, None)

        self.input_queue = Queue(maxsize=config.input_queue_size)
        self.output_queue = Queue(maxsize=config.output_queue_size)

        self._stop_event = threading.Event()

        self.on_initiate()


    def predict(self, frame, frame_headers={}):
        self.input_queue.put( (frame, frame_headers) )
        return self.output_queue.get()

    def pipeline(self, frame, frame_meta=None):
        # this is the pipepline function
        # we need to perform 
        # 1. crop
        # 2. scaling
        # 3. detection
        # 4. tracking
        # 5. periodict analyis
        # all in this function
        if frame.ndim == 3:
            frame = frame[..., 0]
            
        rd = Rheed(np.array(frame), False)
        rd = rd.min_max_scale(min_v=0, max_v=2**12)

        if self.state.crop_setup is not None:
            rd = rd.crop(**self.state.crop_setup)

        # update the pattern_dim on the fly
        self.state.pattern_dims = rd.pattern.shape
        
        result, cls_result = self.aux_detector.predict(rd)
        rdinst = RheedInstanceSegmentation.from_mmdet(rd, result, self.aux_detector.model, auto_compute_regions=True)


        detections = regions2detections(rdinst.regions, rdinst.regions_label)
        region2tracks = self.tracker.update(detections, self._frame_idx)

        # center is extracted from the initial figure
        if len(rdinst.regions) == 0:
            # self.state.horizontal_center  = r_max_centroid[1]
            pass # find not region do not update the center
        else:
            if any( [ l ==3 for l in rdinst.regions_label ] ):
                db_xy, db_r_i, self.state.db_track = rdinst.get_direct_beam(method='top+tracker', direct_beam_label=3, tracker=self.tracker, track=self.state.db_track)
                self.state.horizontal_center = db_xy[1]
            else:
                # guess center by finding the region with the maximum mean intensity
                rid_max_intensity = np.argmax( [ r.intensity_mean for r in rdinst.regions ] )
                r_max_centroid = rdinst.regions[rid_max_intensity].centroid_weighted
                self.state.horizontal_center  = r_max_centroid[1]
                        
        rdinst.get_regions_collapse()
        rdinst.clean_collapse()
        rdinst.fit_collapse_peaks(height=0.001, threshold=0.000, prominence=0.001)

        try:
            periodicity = rdinst.analyze_peaks_periodicity(
                center=self.state.horizontal_center, 
                method='track', 
                tracker=self.periodicity_tracker, 
                    track_frame_num=self._frame_idx, 
                )
        except Exception as e:
            logging.error(f"Periodicity analysis failed: {e}, setting periodicity to None")
            periodicity = None

        classification = { self.aux_detector.classifier_classes[i] : float(cls_result[0][i]) for i in range(len(cls_result[0]))}
        instances = result.pred_instances

        bbox_outputs = {}                
        for i in range(len(instances.bboxes)):
            bbox_outputs[f"{i}"] = {
                "score":instances.scores[i].cpu().tolist(),
                "label":instances.labels[i].cpu().tolist(),
                "mask":np.array(instances.masks[i].cpu()),
                "bbox":instances.bboxes[i].cpu().tolist(),
            }

        result = {
            "bboxes" : bbox_outputs, 
            "classification" : classification, 
            "mmdet_result" : result, 
            "instance_segementation" : rdinst, 
            "region2tracks" : region2tracks, 
            "periodicity" : periodicity,
        }
        result_headers = {
            **frame_meta, 
        }
        result_headers.update(
            {f"crop_setup_{k}": v for k,v in self.state.crop_setup.items()}
        )
        # print("result header", result_headers)

        return result, result_headers



    def on_initiate(self):
        self.aux_detector = get_model(model_folder=self.config.model_folder, device=self.config.model_device)

        # TODO: make the argument passed down from config
        self.tracker = IOUTracker(t_min=100000, sigma_iou=0.4)

        self.analyzer = PeriodicityAnalyzer(
            tolerant = 0.15, 
            abs_tolerant = 15, # too large the then the very closed spot would be included 
            allow_discontinue = 1
        )

        self.periodicity_tracker = PeriodicityTracker(
            analyzer = self.analyzer,
            disconnect_time = 10,
        )

        self.reset_counter()

    def reset_counter(self):
        self._frame_idx = 0

    def increase_counter(self):
        self._frame_idx += 1

    def reset_tracker(self):
        self.reset_counter()
        self.tracker.clear_history()
        self.periodicity_tracker.clear_history()

    def on_stop(self):
        # raise NotImplementedError
        pass

    def is_continue(self):
        """
            default to True if not overwrited
        """
        stop_flag = self._stop_event.wait(self.config.idle_time)
        if stop_flag : 
            logging.debug("Detection server discontinue")
            return False
        else:
            return True


    def clear(self):
        while not self.input_queue.Empty():
            self.input_queue.get()
        while not self.output_queue.Empty():
            self.output_queue.get()
        

    def run(self):
        # the camera would run indefinitely without being blocked by anything
        # the top of the queue would always be the latest frame

        tmp_counter = 0
        logging.debug("Detection Loop start")
        while self.is_continue():
            if self.input_queue.empty():
                continue

            frame, frame_headers = self.input_queue.get()
            logging.debug("Detection Input Acquired")

            try:
                result, result_headers = self.pipeline(frame, frame_headers)
                logging.debug("Detection Finished")

                self.output_queue.put( (result, result_headers) )
                logging.debug("Detection Output Inserted")

                self.increase_counter()
                tmp_counter += 1
                if tmp_counter % 1000 == 0: self.reset_tracker()
            except Exception as e:
                logging.error(f"Detection Error: {e}")
                self.reset_tracker()

        self.on_stop()

            
    def stop(self):
        logging.info(f"Camera thread ({self.ident}) receives a stop signal")
        self._stop_event.set()


    @property
    def pattern_dims(self):
        if self.state.pattern_dims is not None:
            return self.state.pattern_dims
        else:
            logging.debug("Cannot access the pattern_dim state. Return empty dims")
            return []
    
    @property
    def metas(self):
        return self.DETECTION_METAS