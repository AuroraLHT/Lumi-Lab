import asyncio
from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel, AbstractExchange,  AbstractConnection, AbstractIncomingMessage, AbstractQueue,
)
import json
import numpy as np
from dataclasses import dataclass

from ..base.message_queue import BasicServer, BasicClient
from ..rheed.communitation import LiveCameraMessageQueueClient, CameraMessageQueueClient
from ..detection.communication import LiveDetectionMessageQueueClient, DetectionMessageQueueClient, decode_detections
from ..pascal.communication import LiveChamberLogMessageQueueClient, ChamberLogMessageQueueClient

from .record import RecorderConfig, Recorder
from ..utils.image import decode_img

import logging

#TODO : Move client to where the server is and we would import the client to here.
# this reduce the redundancy in code.


class StorageMessageQueueClient(BasicClient):
    def __init__(
            self, 
            channel: AbstractChannel, 
            exchange: AbstractExchange, 
            routing_key: str, 
            control_routing_key: str, 
            client_name: str, 
            time_out: float
        ) -> None:

        super().__init__(channel, exchange, routing_key, control_routing_key, client_name, time_out)

    async def start_storage(
            self,
            project_name:str, 
            save_frame:bool=True, 
            save_ai:bool=True, 
            save_log:bool=True
        ):

        return await self.request(
            body = ''.encode(),
            headers= { 
                "type" : "start",
                "save_frame" : save_frame,
                "save_ai" : save_ai,
                "save_log" : save_log,
                "project_name" : project_name
            }
        )

    async def end_storage(self):
        return await self.request(
            body = ''.encode(),
            headers= { 
                "type" : "end",
            }
        )

@dataclass
class StorageMessageQueueServerConfig:
    root_folder : str
    initial_size : int = 1000


class StorageMessageQueueServer(BasicServer):
    def __init__(
            self, 
            camera_client : CameraMessageQueueClient,
            log_client : ChamberLogMessageQueueClient,
            detector_client : DetectionMessageQueueClient,
            live_camera_client : LiveCameraMessageQueueClient,
            live_detection_client : LiveDetectionMessageQueueClient,
            live_log_client : LiveChamberLogMessageQueueClient,

            config : StorageMessageQueueServerConfig,

            channel: AbstractChannel, 
            exchange: AbstractExchange, 
            control_routing_key: str, 
            routing_key: str, 
            server_name: str,
        ):
        super().__init__(channel, exchange, control_routing_key, routing_key, server_name)

        self.camera_client = camera_client
        self.log_client = log_client
        self.detector_client = detector_client
        self.live_camera_client = live_camera_client
        self.live_detection_client = live_detection_client
        self.live_log_client = live_log_client

        self.config = config

        self.state["is_storing"] = False
        self.state["is_storing_frame"] = False
        self.state["is_storing_ai"] = False
        self.state["is_storing_log"] = False


    async def on_message(self, message:AbstractIncomingMessage):
        body, headers = message.body, message.headers
        ctrl = headers["type"]

        if ctrl == "start":
            await self.create_storages(body, headers)            
            await self.start_storages(body, headers)
            logging.info(f"{self.server_type} <{self.server_name}> starts storage")

        if ctrl == "end":
            await self.end_storages()
            self.close_storages()
            logging.info(f"{self.server_type} <{self.server_name}> ends storage")

        return "".encode(), {"succ":True}

    async def create_storages(self, body, headers):
        camera_status = await self.camera_client.get_status()
        frame_dim = camera_status["frame_dims"]
        frame_metas_columns = camera_status["frame_metas"]

        log_status = await self.log_client.get_status()
        log_columns = log_status["entries"]

        detection_status = await self.detector_client.get_status()
        pattern_dim = detection_status["pattern_dim"]
        pattern_meta_columns = detection_status["detection_metas"]
        classifier_classes = detection_status["classifier_classes"]

        self.recorder_config = RecorderConfig(
            project_name = headers["project_name"],
            root_folder = self.config.root_folder,

            frame_dim = frame_dim,
            frame_meta_columns = frame_metas_columns,

            log_columns = log_columns,

            pattern_dim = pattern_dim,
            pattern_meta_columns = pattern_meta_columns,

            classifier_classes = classifier_classes,

            initial_size = self.config.initial_size,
            
            save_frame = headers["save_frame"],
            save_log = headers["save_log"],
            save_ai = headers["save_ai"],
        )

        self.recorder = Recorder(config=self.recorder_config)

    def close_storages(self):
        self.recorder.close_h5()

    async def start_storages(self, body, headers):
        if headers["save_frame"]:
            await self.start_frame_storage()
            self.state["is_storing_frame"] = True

        if headers["save_log"]:
            await self.start_log_storage()
            self.state["is_storing_log"] = True

        if headers["save_ai"]:
            await self.start_ai_storage()
            self.state["is_storing_ai"] = True

        self.state["is_storing"] = True

    async def end_storages(self):
        await self.end_frame_storage()
        await self.end_log_storage()
        await self.end_ai_storage()
        self.state["is_storing"] = False
        self.state["is_storing_frame"] = False
        self.state["is_storing_ai"] = False
        self.state["is_storing_log"] = False


    async def start_frame_storage(self):
        async def on_response_callback(message:AbstractIncomingMessage):
            body, headers = message.body, message.headers
            frame, frame_headers = decode_img(body, headers)
            self.recorder.save_frame(frame, frame_headers)

        self.live_camera_client.update_reponse_callback(on_response_callback=on_response_callback)
        await self.live_camera_client.start(start_consume_loop=True)


    async def start_log_storage(self):
        async def on_response_callback(message:AbstractIncomingMessage):
            body, headers = message.body, message.headers
            chamber_log = json.loads(body)
            self.recorder.save_log(chamber_log=chamber_log)

        self.live_log_client.update_reponse_callback(on_response_callback=on_response_callback)
        await self.live_log_client.start(start_consume_loop=True)

    async def start_ai_storage(self):
        async def on_response_callback(message:AbstractIncomingMessage):
            body, headers = message.body, message.headers

            result, result_headers = decode_detections(body, headers)
            pattern = result["pattern"]["pattern"]
            n_detections = len( result["bboxes"].keys() )
            masks = np.stack( [ result["bboxes"][f"{i}"]["mask"]["mask"] for i in range(n_detections)] )
            bboxes = np.stack( [ result["bboxes"][f"{i}"]["bbox"] for i in range(n_detections)] )
            labels = np.stack( [ result["bboxes"][f"{i}"]["label"] for i in range(n_detections)] )
            scores = np.stack( [ result["bboxes"][f"{i}"]["score"] for i in range(n_detections)] )
            cls_result = result["classification"]
            tracking = result["region2tracks"]

            self.recorder.save_prediction(
                pattern=pattern,
                masks=masks,
                bboxes=bboxes,
                labels=labels,
                scores=scores,
                cls_result=cls_result,
                tracking=tracking,
                detection_meta=result_headers
            )


        self.live_detection_client.update_reponse_callback(on_response_callback=on_response_callback)
        await self.live_detection_client.start(start_consume_loop=True)

    async def end_log_storage(self):
        await self.live_log_client.stop()

    async def end_ai_storage(self):
        await self.live_detection_client.stop()

    async def end_frame_storage(self):
        await self.live_camera_client.stop()
