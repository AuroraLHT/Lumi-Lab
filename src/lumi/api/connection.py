from collections.abc import Awaitable, Callable
from typing import Any, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import struct
import logging
from pathlib import Path
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange

from .models import StorageRequest
from .communication import (
    BasicStreamClient,
    LiveVideoFragmentsMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    CameraMessageQueueClient,
    LiveDetectionMessageQueueClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
    StorageMessageQueueClient,
    STFTMessageQueueClient,
    IntegratorMessageQueueClient,
    LiveCameraMessageQueueClient,
)

from lumi.config import settings

import traceback

@dataclass
class ConnectionManager:
    connection: Optional[AbstractConnection] = None
    channel: Optional[AbstractChannel] = None
    exchange_rheed: Optional[AbstractExchange] = None
    exchange_pascal: Optional[AbstractExchange] = None
    exchange_storage: Optional[AbstractExchange] = None

    camera_client: Optional[CameraMessageQueueClient] = None
    video_fragment_client: Optional[VideoFragmentsMessageQueueClient] = None
    log_client: Optional[ChamberLogMessageQueueClient] = None
    storage_client: Optional[StorageMessageQueueClient] = None
    integrator_client: Optional[IntegratorMessageQueueClient] = None
    stft_client: Optional[STFTMessageQueueClient] = None

    live_video_client: Optional[LiveVideoFragmentsMessageQueueClient] = None
    live_log_client: Optional[LiveChamberLogMessageQueueClient] = None
    live_detection_client: Optional[LiveDetectionMessageQueueClient] = None
    live_camera_client: Optional[LiveCameraMessageQueueClient] = None

    # async def create_live_log_client(
    #         self, 
    #         on_state_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[None]]] = None, 
    #         on_response_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[None]]] = None,
    #         start_main: bool = True,
    #         start_control: bool = True,
    #         start_state: bool = True,
    #         name_suffix: str = "",
    # ):
    #     live_log_client = LiveChamberLogMessageQueueClient.from_config(
    #         config=settings.pascal.mq.live_chamber_log,
    #         channel=self.channel,
    #         exchange=self.exchange_pascal,
    #         time_out=10,
    #         on_response_callback=on_response_callback,
    #         on_state_callback=on_state_callback,
    #         name_suffix=name_suffix,
    #     )
    #     if start_main:
    #         await live_log_client.start_main()
    #     if start_state:
    #         await live_log_client.start_state()
    #     if start_control:
    #         await live_log_client.start_control()
    #     return live_log_client
    
    # async def create_live_video_client(
    #         self, 
    #         on_state_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[None]]] = None, 
    #         on_response_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[None]]] = None,
    #         start_main: bool = True,
    #         start_control: bool = True,
    #         start_state: bool = True,
    # ):
    #     live_video_client = LiveVideoFragmentsMessageQueueClient(
    #         channel=self.channel,
    #         exchange=self.exchange_rheed,
    #         publish_routing_key=settings.rheed.mq.live_video_fragments.publish_key,
    #         control_routing_key=settings.rheed.mq.live_video_fragments.ctrl_key,
    #         state_routing_key=settings.rheed.mq.live_video_fragments.state_key,
    #         on_response_callback=on_response_callback,
    #         on_state_callback=on_state_callback,
    #         client_name=settings.rheed.mq.live_video_fragments.state_monitor_name,
    #         time_out=10,
    #     )
