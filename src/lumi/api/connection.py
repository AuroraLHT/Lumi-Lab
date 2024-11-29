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

import traceback

@dataclass
class ConnectionManager:
    connection: Optional[AbstractConnection] = None
    channel: Optional[AbstractChannel] = None
    exchange_rheed: Optional[AbstractExchange] = None
    exchange_pascal: Optional[AbstractExchange] = None
    exchange_storage: Optional[AbstractExchange] = None

    image_client: Optional[CameraMessageQueueClient] = None
    video_fragment_client: Optional[VideoFragmentsMessageQueueClient] = None
    log_client: Optional[ChamberLogMessageQueueClient] = None
    storage_client: Optional[StorageMessageQueueClient] = None
    integrator_client: Optional[IntegratorMessageQueueClient] = None
    stft_client: Optional[STFTMessageQueueClient] = None

    live_video_client: Optional[LiveVideoFragmentsMessageQueueClient] = None
    live_log_client: Optional[LiveChamberLogMessageQueueClient] = None
    live_detection_client: Optional[LiveDetectionMessageQueueClient] = None
    live_camera_client: Optional[LiveCameraMessageQueueClient] = None