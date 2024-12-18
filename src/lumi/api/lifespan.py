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
)

import traceback

from .connection import ConnectionManager

from lumi.config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    # https://apidog.com/articles/fastapi-multiple-threading-python/#:~:text=In%20the%20context%20of%20FastAPI,network%20requests)%20to%20separate%20threads.
    # https://fastapi.tiangolo.com/advanced/events/
    connection_state = ConnectionManager()

    # see https://stackoverflow.com/questions/71298179/fastapi-how-to-get-app-instance-inside-a-router
    # for why we need to set app.state.connection_state
    app.state.connection_state = connection_state

    # put startup code here
    connection = await connect(f"amqp://guest:guest@{settings.rabbitmq.host}/")
    connection_state.connection = connection

    channel = await connection.channel()
    connection_state.channel = channel

    exchange_rheed = await channel.declare_exchange(settings.rheed.exchange, type=ExchangeType(settings.rheed.exchange_type))
    exchange_pascal = await channel.declare_exchange(
        settings.pascal.exchange, type=ExchangeType(settings.pascal.exchange_type)
    )
    exchange_storage = await channel.declare_exchange(
        settings.storage.exchange, type=ExchangeType(settings.storage.exchange_type)
    )
    connection_state.exchange_pascal = exchange_pascal
    connection_state.exchange_rheed = exchange_rheed
    connection_state.exchange_storage = exchange_storage

    # image client is for all user that connect to this api node
    image_client = CameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        request_routing_key=settings.rheed.mq.camera.request_key,
        control_routing_key=settings.rheed.mq.camera.ctrl_key,
        state_routing_key=settings.rheed.mq.camera.state_key,
        on_state_callback=None,
        client_name=settings.rheed.mq.camera.name,
        time_out=10,
    )
    await image_client.start()
    connection_state.image_client = image_client

    video_fragment_client = VideoFragmentsMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        request_routing_key=settings.rheed.mq.video.request_key,
        control_routing_key=settings.rheed.mq.video.ctrl_key,
        state_routing_key=settings.rheed.mq.video.state_key,
        on_state_callback=None,
        client_name=settings.rheed.mq.video.name,
        time_out=10,
    )
    await video_fragment_client.start()
    connection_state.video_fragment_client = video_fragment_client

    log_client = ChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_pascal,
        request_routing_key=settings.pascal.mq.chamber_log.request_key,
        control_routing_key=settings.pascal.mq.chamber_log.ctrl_key,
        state_routing_key=settings.pascal.mq.chamber_log.state_key,
        on_state_callback=None,
        client_name=settings.pascal.mq.chamber_log.name,
        time_out=10,
    )
    await log_client.start()
    connection_state.log_client = log_client

    storage_client = StorageMessageQueueClient(
        channel=channel,
        exchange=exchange_storage,
        request_routing_key=settings.storage.mq.storage.request_key,
        control_routing_key=settings.storage.mq.storage.ctrl_key,
        state_routing_key=settings.storage.mq.storage.state_key,
        on_state_callback=None,
        client_name=settings.storage.mq.storage.name,
        time_out=10,
    )
    await storage_client.start()
    connection_state.storage_client = storage_client

    integrator_client = IntegratorMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        request_routing_key=settings.rheed.mq.integrator.request_key,
        control_routing_key=settings.rheed.mq.integrator.ctrl_key,
        state_routing_key=settings.rheed.mq.integrator.state_key,
        on_state_callback=None,
        client_name=settings.rheed.mq.integrator.name,
        time_out=10,
    )
    await integrator_client.start()
    connection_state.integrator_client = integrator_client

    stft_client = STFTMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        request_routing_key=settings.rheed.mq.stft.request_key,
        control_routing_key=settings.rheed.mq.stft.ctrl_key,
        state_routing_key=settings.rheed.mq.stft.state_key,
        on_state_callback=None,
        client_name=settings.rheed.mq.stft.name,
        time_out=10,
    )
    await stft_client.start()
    connection_state.stft_client = stft_client
    
    # # this globle client is only open for status checking
    # live_video_client = LiveVideoFragmentsMessageQueueClient(
    #     channel=connection_state.channel,
    #     exchange=connection_state.exchange_rheed,
    #     routing_key="live_video",
    #     control_routing_key="live_video_ctrl",
    #     state_routing_key="live_video_state",
    #     on_response_callback=None,
    #     on_state_callback=None,
    #     client_name="Live Fragment Monitor",
    #     time_out=10,
    # )
    # await live_video_client.start_control()
    # connection_state.live_video_client = live_video_client

    # live_log_client = LiveChamberLogMessageQueueClient(
    #     channel=connection_state.channel,
    #     exchange=connection_state.exchange_pascal,
    #     publish_routing_key=settings.pascal.mq.live_chamber_log.publish_key,
    #     control_routing_key=settings.pascal.mq.live_chamber_log.ctrl_key,
    #     state_routing_key=settings.pascal.mq.live_chamber_log.state_key,
    #     on_response_callback=None,
    #     on_state_callback=None,
    #     client_name=settings.pascal.mq.live_chamber_log.state_monitor_name,
    #     time_out=10,
    # )
    # await live_log_client.start_state()
    # await live_log_client.start_control()
    # connection_state.live_log_client = live_log_client

    yield
    # put shutdown code here
    await connection.close()
