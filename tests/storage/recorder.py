from dataclasses import dataclass
from typing import List, Dict, Optional, Union
from pathlib import Path
from collections import defaultdict
import json
import PIL
import time

import numpy as np
import pandas as pd
from tqdm.notebook import tqdm
import matplotlib.pyplot as plt

import cv2
import torch
from skimage import restoration


import mmcv
from mmcv.transforms import Compose

import mmengine
from mmengine import Config
from mmengine.utils import track_iter_progress
from mmdet.registry import VISUALIZERS
from mmdet.apis import init_detector, inference_detector

import skimage.exposure as exposure

# from rhana.io.tokyo_u import RHEEDStreamReader
from rhana.pattern import Rheed
from rhana.pattern import RheedInstanceSegmentation

from rhana.labeler.unet import rle_decode_arr

from rhana.labeler.detector import CascadeMaskRCNNDetectorAndClassifier
from rhana.tracker.iou_tracker import IOUTracker, regions2detections, IOUMaskTracker
from rhana.tracker.periodicity_tracker import PeriodicityTracker

from rhana.tools.restoration import suggest_restoration
from rhana.tools.beam import get_metas, get_deposition_window, plot_metas
from rhana.periodicity import PeriodicityAnalyzer

import h5py

model_folder = Path("/home/hliang16/projects/LnFeO_TokyoU/nn/cascade_maskrcnn_exps")
aux_detector = CascadeMaskRCNNDetectorAndClassifier(
    config_path = str(model_folder / "config_predict.py"),
    checkpoint_path = str(model_folder / "epoch_12.pth"),
    classifier_config_path = str(model_folder / "classifier/config.json"),
    classifier_checkpoint_path = str(model_folder / "classifier/model.pth"),
    device = "cuda:0",
)

root_folder = Path("../../database/")
# root_folder = Path("../Data/UMD2024/")
# root_folder = Path("/mnt/terp/projects/LnFeO3_PLD/Data")
root_folder.mkdir(exist_ok=True)

project_name = "test-recorder-001"

# project_folder = root_folder / project_name
# assert not project_folder.exists(), str( project_folder.absolute() )
# project_folder.mkdir(exist_ok=True)

import requests
from lumi.utils.image import decode_img

def get_live_image(host):
    r = requests.get(f'http://{host}/RHEED/image')
    
    # print(r.status_code)
    # print(r.headers)

    image, image_header = decode_img(r.content, r.headers)
    return image, image_header

def get_live_log(host):
    r = requests.get(f'http://{host}/chamber/log')
    chamber_log = json.loads( r.content )
    return chamber_log



from lumi.rheed.test_camera import TestCameraConfig, TestCamera

from lumi.pascal.log_reader import LogReader, LogReaderConfig, TestLogReader, TestLogReaderConfig

from lumi.storage.record import RecorderConfig, Recorder

import time
import datetime

import cv2

import aio_pika
from aio_pika import ExchangeType, connect
import asyncio
import logging

from pathlib import Path

from lumi.detection.model import DetectorServer
import time

FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)


height = 540
width = 720
test_camera_config = TestCameraConfig(
    frame_dims=(height, width),
    source= Path(__file__).parent.parent.parent / "src/lumi/rheed/assets/test_frame.npy",
    fps=30,
    queue_size=2,
)
camera = TestCamera(config=test_camera_config, name="test_cam")
camera.daemon = True
camera.start()

log_config = TestLogReaderConfig(queue_size=10, idle_time=0.01, publish_interval=1, log_path=Path(__file__).parent.parent.parent / "src/lumi/pascal/assets/chamber_log_test.csv" )
log_reader = TestLogReader(config=log_config, name="test_log_reader", daemon=True)
log_reader.daemon = True
log_reader.start()


while True:
    frame, frame_header = camera.get_frame()
    print(type(frame), frame_header)
    time.sleep(0.05)
    if frame is not None: break


while True:
    log, log_header= log_reader.get_log()
    print(type(log), log_header)
    time.sleep(0.05)
    if log is not None: break

tracker = IOUTracker(t_min=100000, sigma_iou=0.4)

analyzer = PeriodicityAnalyzer(
    tolerant=0.15, 
    abs_tolerant=15, # too large the then the very closed spot would be included 
    allow_discontinue=1
)

periodicity_tracker = PeriodicityTracker(
    analyzer = analyzer,
    disconnect_time = 10,
)

rd_original = Rheed(frame, False)
rd_original.min_max_scale(0,  max_v = 2**12)
crop_setup = {"sx":60, "sy":0, "ex":500, "ey":720}
rd = rd_original.crop(**crop_setup)

       

config =  RecorderConfig(
    project_name = project_name,
    root_folder = root_folder,

    frame_dim = frame.shape,
    frame_meta_columns = list( frame_header.keys() ),

    log_columns = list(log.keys()),

    pattern_dim = rd.pattern.shape,
    detection_meta_columns = DetectorServer.DETECTION_METAS,

    classifier_classes = aux_detector.classifier_classes,

    initial_size = 10,
    
    save_frame = True,
    save_log = True,
    save_ai = True,
    force_rewrite= True,
)

recorder = Recorder(
    config = config
)

def to_np(t):
    return np.array( t.detach().cpu() )

recorder.create_dataset()
for i in range(100):
    result, cls_result = aux_detector.predict(rd, )
    rdinst = RheedInstanceSegmentation.from_mmdet(rd, result, aux_detector.model, auto_compute_regions=True)

    detections = regions2detections(rdinst.regions, rdinst.regions_label)
    region2tracks = tracker.update(detections, 0)

    recorder.save_frame( frame, frame_headers=frame_header)
    recorder.save_log( log )

    detection_meta = ({**frame_header})
    detection_meta.update( { f"crop_setup_{k}":v for k, v in crop_setup.items() } )
    print(detection_meta)
    
    print("detection_meta", recorder.ds_detection_meta.shape)

    recorder.save_prediction(
        pattern = rd.pattern,
        masks = to_np( result.pred_instances.masks ),
        labels = to_np( result.pred_instances.labels ),
        bboxes = to_np( result.pred_instances.bboxes ),
        scores = to_np( result.pred_instances.scores ),
        cls_result = to_np( cls_result.detach().cpu()[0] ),
        tracking = region2tracks,
        detection_meta = detection_meta,
    )

camera.stop()
log_reader.stop()
camera.join()
log_reader.join()