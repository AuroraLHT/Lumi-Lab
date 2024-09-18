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

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    BasicStreamClient,
    LiveVideoFragmentsMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    CameraMessageQueueClient,
    LiveDetectionMessageQueueClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
    StorageMessageQueueClient,
)

import traceback

from .connection_state import ConnectionState

@asynccontextmanager
async def lifespan(app: FastAPI):
    # https://apidog.com/articles/fastapi-multiple-threading-python/#:~:text=In%20the%20context%20of%20FastAPI,network%20requests)%20to%20separate%20threads.
    # https://fastapi.tiangolo.com/advanced/events/
    connection_state = ConnectionState()

    # see https://stackoverflow.com/questions/71298179/fastapi-how-to-get-app-instance-inside-a-router
    # for why we need to set app.state.connection_state
    app.state.connection_state = connection_state

    # put startup code here
    connection = await connect("amqp://guest:guest@localhost/")
    connection_state.connection = connection

    channel = await connection.channel()
    connection_state.channel = channel

    exchange_rheed = await channel.declare_exchange("RHEED", type=ExchangeType.DIRECT)
    exchange_chamber = await channel.declare_exchange(
        "chamber", type=ExchangeType.DIRECT
    )
    exchange_storage = await channel.declare_exchange(
        "storage", type=ExchangeType.DIRECT
    )
    connection_state.exchange_chamber = exchange_chamber
    connection_state.exchange_rheed = exchange_rheed
    connection_state.exchange_storage = exchange_storage

    # image client is for all user that connect to this api node
    image_client = CameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="image",
        control_routing_key="image_ctrl",
        state_routing_key="image_state",
        on_state_callback=None,
        client_name="Camera",
        time_out=10,
    )
    await image_client.start()
    connection_state.image_client = image_client

    video_fragment_client = VideoFragmentsMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="live_video_history",
        control_routing_key="live_video_history_ctrl",
        state_routing_key="live_video_history_state",
        on_state_callback=None,
        client_name="Fragment",
        time_out=10,
    )
    await video_fragment_client.start()
    connection_state.video_fragment_client = video_fragment_client

    log_client = ChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_chamber,
        routing_key="log",
        control_routing_key="log_ctrl",
        state_routing_key="log_state",
        on_state_callback=None,
        client_name="Chamber Log",
        time_out=10,
    )
    await log_client.start()
    connection_state.log_client = log_client

    storage_client = StorageMessageQueueClient(
        channel=channel,
        exchange=exchange_storage,
        routing_key="storage",
        control_routing_key="storage_ctrl",
        state_routing_key="storage_state",
        on_state_callback=None,
        client_name="Storage",
        time_out=10,
    )
    await storage_client.start()
    connection_state.storage_client = storage_client

    # this globle client is only open for status checking
    live_video_client = LiveVideoFragmentsMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_video",
        control_routing_key="live_video_ctrl",
        state_routing_key="live_video_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live Fragment Monitor",
        time_out=10,
    )
    await live_video_client.start_control()
    connection_state.live_video_client = live_video_client



    live_log_client = LiveChamberLogMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_chamber,
        routing_key="live_log",
        control_routing_key="live_log_ctrl",
        state_routing_key="live_log_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live Log Monitor",
        time_out=10,
    )
    await live_log_client.start_control()
    connection_state.live_log_client = live_log_client

    yield
    # put shutdown code here
    await connection.close()
