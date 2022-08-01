from typing import Union

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from models import *

import numpy as np

from rhana.labeler.masker import UnetMasker
from rhana.pattern import Rheed, RheedMask
from rhana.utils import load_yaml
from rhana.tracker.iou import IOUMaskTracker, regions2detections

app = FastAPI()
path = "/home/hliang16/codebases/rhana/learner/UNet_1647_Apr27_2022.pkl"
masker = UnetMasker(path, cpu=False, device="cuda")

pipeline_config = load_yaml("pipeline_config.yml")[0]
input_config = {}


def load_binary_rheed(binary_data, data_type="H", data_shape=None):
    data = np.frombuffer(binary_data, dtype=data_type, offset=0)
    data = data.reshape(data_shape)
    return data


def save_binary_masks(masks):
    mask_binary = masks.tobytes()
    return mask_binary
        

@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/pipeline_config")
async def get_unet_config():
    return GetPipelineConfig(**pipeline_config)


@app.post("/pipeline_config")
async def update_unet_config(config:UpdatePipelineConfig):
    pipeline_config.update(config.dict())
    return pipeline_config


@app.post("/input_config")
async def update_unet_input_config(config:UNetInputConfig):
    input_config.update(config.dict())
    return input_config


@app.websocket("/ws/unet")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    batch = []
    batch_frame_num = []
    frame_num = 0

    if pipeline_config['do_tracking']:
        trackers_by_classes = {
            c : IOUMaskTracker(
                t_min=pipeline_config['tracker_t_min'], 
                sigma_iou=pipeline_config['tracker_sigma_iou']) 
                for c in pipeline_config['classes'] 
        }


    while True:
        binary_data = await websocket.receive_bytes()
        # await websocket.send_bytes(binary_data)
        # make it sync since the gpu resource is limitted
        data_shape = input_config['shape']

        data = load_binary_rheed(
            binary_data,
            data_type=input_config['dtype'],
            data_shape=data_shape
            )

        rd = Rheed(pattern=data)
        if pipeline_config["do_min_max"]:
            rd.min_max_scale(inplace=True)
        if pipeline_config['do_mean_clip']:
            rd.mean_clip(inplace=True)

        batch.append(rd)
        batch_frame_num.append(frame_num)
        frame_num += 1

        assert input_config['batch'] >= len(batch)
        if input_config['batch'] == len(batch):
            masks = masker.predict_batch(batch, return_raw=True)
            for i in range(input_config['batch']):
                masks_binary = save_binary_masks(masks[i, ...])
                await websocket.send_bytes(masks_binary)

                result = {}
                for ci, c in enumerate(pipeline_config['classes']):
                    rdm = RheedMask(batch[i], masks[i][ci])
                    rdm.get_regions()
                    rdm.filter_regions(min_area=pipeline_config['min_area'])

                # (minr, minc, maxr, maxc) bbox format

                result_single_class = { "regions" : {
                     i : { 
                        "bbox": r.bbox,
                        "centroid" : r.centroid,
                        "area" : int(r.area)
                    }  for i ,r in enumerate(rdm.regions) } }
                if pipeline_config['do_tracking']:
                    tracker = trackers_by_classes[c]
                    region2track = tracker.update(regions2detections(rdm.regions), frame_num=batch_frame_num[i])
                    result_single_class['track'] = region2track
                result[c] = result_single_class
                await websocket.send_json(result)

            batch = []


@app.websocket("/ws/test")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    batch = []
    while True:
        binary_data = await websocket.receive_bytes()
        # await websocket.send_bytes(binary_data)
        # make it sync since the gpu resource is limitted
        data_shape = input_config['shape']

        data = load_binary_rheed(
            binary_data,
            data_type=input_config['dtype'],
            data_shape=data_shape
            )

        rd = Rheed(pattern=data)
        if pipeline_config["do_min_max"]:
            rd.min_max_scale(inplace=True)
        if pipeline_config['do_mean_clip']:
            rd.mean_clip(inplace=True)

        batch.append(rd)
        assert input_config['batch'] >= len(batch)
        if input_config['batch'] == len(batch):
            for i in range(input_config['batch']):
                pattern = batch[i].pattern.astype(np.float32)
                rd_binary = (pattern).tobytes()
                await websocket.send_bytes(rd_binary)

            batch = []


@app.websocket("/ws/test2")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    batch = []
    while True:
        binary_data = await websocket.receive_bytes()
        batch.append(binary_data)
        assert input_config['batch'] >= len(batch)
        if input_config['batch'] == len(batch):
            for i in range(input_config['batch']):
                await websocket.send_bytes(batch[i])
            batch = []


@app.websocket("/ws/test3")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        binary_data = await websocket.receive_bytes()
        pass