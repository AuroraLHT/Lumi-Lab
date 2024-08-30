from typing import Union, Optional
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
    LiveVideoFragmentsMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    CameraMessageQueueClient,
    LiveDetectionMessageQueueClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
    StorageMessageQueueClient,
)

FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)


count_down = 100


@dataclass
class ConnectionStateManager:
    connection: Optional[AbstractConnection] = None
    channel: Optional[AbstractChannel] = None
    exchange_rheed: Optional[AbstractExchange] = None
    exchange_chamber: Optional[AbstractExchange] = None
    exchange_storage: Optional[AbstractExchange] = None

    image_client: Optional[CameraMessageQueueClient] = None
    video_fragment_client: Optional[VideoFragmentsMessageQueueClient] = None
    log_client: Optional[ChamberLogMessageQueueClient] = None
    storage_client: Optional[StorageMessageQueueClient] = None

    live_video_client: Optional[LiveVideoFragmentsMessageQueueClient] = None
    live_log_client: Optional[LiveChamberLogMessageQueueClient] = None
    live_detection_client: Optional[LiveDetectionMessageQueueClient] = None

async def main():
    connection_state = ConnectionStateManager()

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




    async def simulate_shutdown(*args, **kwargs):
        global count_down
        if count_down > 0 :
            count_down -= 1
            return False
        else:
            return True


    async def on_live_message_callback(message: AbstractIncomingMessage):
        """
        return : succ or not state flag
        """
        # TODO : add condition for finding other streaming option
        headers = message.headers
        logging.info(
            f"publish fragment {headers['frag_idx']} frame start from {headers['frame_start']} to {headers['frame_end']}"
        )
        return await simulate_shutdown()
        # return await send_fragment(message.body)

    # message = await websocket.receive()
    # logging.info(message)

    live_client = LiveVideoFragmentsMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_video",
        control_routing_key="live_video_ctrl",
        state_routing_key="live_video_state",
        on_response_callback=on_live_message_callback,
        on_state_callback=None, 
        client_name="Live Fragment",
        time_out=10,
    )

    initial_fragments = await connection_state.video_fragment_client.get_initial()
    for fragment in initial_fragments:
        await simulate_shutdown(fragment)

    await live_client.start()
    await live_client.start_streaming()

    # await ws_out_task
    while True:
        if live_client.is_main_running():
            await asyncio.sleep(0.1)
        else:
            break
    # await asyncio.Future()
    logging.info("live camera exit")

if __name__ == "__main__":
    asyncio.run(main())