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


import h5py


root_folder = Path("../../database/")
# root_folder = Path("../Data/UMD2024/")
# root_folder = Path("/mnt/terp/projects/LnFeO3_PLD/Data")
root_folder.mkdir(exist_ok=True)

project_name = "test-recserver-001"

# project_folder = root_folder / project_name
# assert not project_folder.exists(), str( project_folder.absolute() )
# project_folder.mkdir(exist_ok=True)

import requests
from lumi.utils.image import decode_img


from lumi.rheed.test_camera import TestCameraConfig, TestCamera

from lumi.pascal.log_reader import LogReader, LogReaderConfig, TestLogReader, TestLogReaderConfig

from lumi.storage.record import RecorderConfig, Recorder, RecorderServerConfig, RecorderServer

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
from tqdm import tqdm

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



config =  RecorderConfig(
    project_name = project_name,
    root_folder = root_folder,

    frame_dim = frame.shape,
    frame_meta_columns = list( frame_header.keys() ),

    log_columns = list(log.keys()),

    pattern_dim = (100, 100),
    detection_meta_columns = [],

    classifier_classes = [],
    detector_classes= [],

    initial_size = 10,
    
    save_frame = True,
    save_log = True,
    save_ai = True,
    save_integration = True,
    force_rewrite= True,

    # compression= None,
    # compression_opts=None,
    compression= "gzip",
    compression_opts=4,

)

recorder = Recorder(
    config = config
)
recorder.create_datasets()

recorder_server_config = RecorderServerConfig(
    idle_time = 0.1
)
recorder_server = RecorderServer(
    config = recorder_server_config,
    recorder = recorder,
    name = "rec_server"
)
recorder_server.start()

def to_np(t):
    return np.array( t.detach().cpu() )

n = 100
prev_time = time.time()
for i in tqdm(range(100), total=100):
    recorder_server.save_frame(frame + np.random.randn(*frame.shape), frame_headers=frame_header)
    current_time = time.time()
    print(f"fps: {1/(current_time - prev_time)}")
    prev_time = current_time

camera.stop()
log_reader.stop()
recorder_server.stop()

camera.join()
log_reader.join()
recorder_server.join()

recorder.close_h5()
