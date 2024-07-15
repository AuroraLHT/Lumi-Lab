import asyncio
from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel, AbstractExchange,  AbstractConnection, AbstractIncomingMessage, AbstractQueue,
)
from ..base.message_queue import BasicServer
from ..rheed.communitation import LiveCameraMessageQueueClient, CameraMessageQueueClient
from ..detection.communication import LiveDetectionClient, DetectionMessageQueueClient
from ..pascal.communication import LiveChamberLogMessageQueueClient, ChamberLogMessageQueueClient

from .communication import RecorderConfig

#TODO : Move client to where the server is and we would import the client to here.
# this reduce the redundancy in code.

#TODO : need to move to config
CONFIG = {
    "root_folder" : "./storage",
    "initial_size" : 1000,
}


class StorageMessageQueueServer(BasicServer):
    def __init__(
            self, 
            camera_client : CameraMessageQueueClient,
            log_client : ChamberLogMessageQueueClient,
            detector_client : Detect,
            live_camera_client : LiveCameraMessageQueueClient,
            live_detection_client : LiveDetectionClient,
            live_chamber_client : LiveChamberLogMessageQueueClient,

            channel: AbstractChannel, 
            exchange: AbstractExchange, 
            control_routing_key: str, 
            routing_key: str, 
            server_name: str
        ):
        super().__init__(channel, exchange, control_routing_key, routing_key, server_name)


    def on_message(self, message:AbstractIncomingMessage):
        body, headers = message.body, message.headers
        ctrl = headers["type"]

        if ctrl == "start":
            project_name = headers["project_name"]


    def create_storage(self, body, headers):
        recorder_config= RecorderConfig(
            project_name = headers['project_name']
            root_folder = CONFIG["ROOT_FOLDER"]

            frame_dim : Tuple[int]
            frame_meta_columns : List[str]

            log_columns : List[str]

            pattern_dim : Tuple[int]
            pattern_meta_columns : List[str] 

            classifier_classes : List[str]

            initial_size : CONFIG["initial_size"]
            
            save_frame : headers["save_frame"]
            save_log : headers["save_log"]
            save_ai : headers["save_ai"]
        )