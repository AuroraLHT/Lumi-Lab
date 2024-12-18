from collections.abc import Awaitable, Callable
import signal
from typing import Any, List, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import logging
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, Request, APIRouter
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse

from aio_pika.abc import AbstractIncomingMessage, AbstractChannel, AbstractExchange
from lumi.base.message_queue import BasicClient, BasicStreamClient, PubSubClient
from lumi.detection.communication import LiveDetectionMessageQueueClient
from lumi.rheed.communication import (
    LiveCameraMessageQueueClient,
    LiveIntegratorMessageQueueClient,
    LiveSTFTMessageQueueClient,
    LiveVideoFragmentsMessageQueueClient,
)
from lumi.utils.common import decode_json, encode_json

from ..models import StorageRequest
from ..communication import LiveChamberLogMessageQueueClient, MIModeMessageQueueClient

import traceback

from ..websockets.base import (
    generic_websocket_handler,
    WebsocketMultiClientsHandler,
    BaseClientMessageMapper,
    BaseStreamClientMessageMapper,
)
from ..websockets.chamber import (
    LiveChamberLogClientMessageMapper,
    MIModePubSubClientMessageMapper,
)
from ..utils import update_state
from ..connection import ConnectionManager

from lumi.config import settings

"""
Nodes API
This node is ussd manage all the node in the network
"""
router = APIRouter()


def prepare_state(state: dict, client_name: str):
    return encode_json({"client_name": client_name, "state": state}, encode=False)

def create_on_state_callback(client_name: str, queue: asyncio.Queue):

    async def on_state_callback(message: AbstractIncomingMessage):
        # logging.info(f"detection state put {message.body.decode()}")
        state = decode_json(message.body)

        state["is_available"] = True
        
        # print("on state callback", state)

        await queue.put(
            prepare_state(state, client_name)
        )

    return on_state_callback


@router.get("/nodes/state")
async def get_nodes_state(request: Request):
    # logging.info("get_chamber_log_state connection_state")
    # logging.info("-" * 100)
    shutdown_event = asyncio.Event()

    queue = asyncio.Queue()
    clients: List[Union[BasicStreamClient, PubSubClient]] = []

    def handle_sigterm():
        logging.info("Received SIGTERM signal.Shutting down nodes state")
        shutdown_event.set()
        # queue.shutdown() # this only available in python > 3.13

    signal.signal(signal.SIGTERM, lambda signum, frame: handle_sigterm())

    def register_live_client(
        client_cls: type[BasicStreamClient],
        config: dict,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        time_out: float,
    ):
        client = client_cls.from_config(
            config=config,
            channel=channel,
            exchange=exchange,
            time_out=time_out,
            on_state_callback=None,
            on_response_callback=None,
            name_suffix=" State Monitor",
        )
        client.on_state_callback = create_on_state_callback(client.client_name, queue)
        clients.append(client)

    def register_pubsub_client(
        client_cls: type[PubSubClient],
        config: dict,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        time_out: float,
    ):
        client = client_cls.from_config(
            config=config,
            channel=channel,
            exchange=exchange,
            time_out=time_out,
            on_state_callback=None,
            on_update_callback=None,
            name_suffix=" State Monitor",
        )
        client.on_state_callback = create_on_state_callback(client.client_name, queue)
        clients.append(client)

    connection_state: ConnectionManager = request.app.state.connection_state

    async def state_generator():

        async def periodic_update(interval: float):

            async def get_state(client: Union[BasicStreamClient, PubSubClient, BasicClient]):
                try:
                    state = await asyncio.wait_for(
                            client.get_state(return_bytes=False), timeout=1.0
                    )
                    state["is_available"] = True
                except asyncio.TimeoutError as e:
                    state = {
                        "is_available": False,
                        "error": "Timeout Error"
                    }
                except Exception as e:
                    raise e

                return state, client.client_name


            while not shutdown_event.is_set():
                states = await asyncio.gather(
                    *[ get_state(client) for client in clients]
                )

                # print("periodic update", states)

                for state, client_name in states:
                    await queue.put( prepare_state(state, client_name) ) 

                await asyncio.sleep(interval)

        register_live_client(
            LiveChamberLogMessageQueueClient,
            settings.pascal.mq.live_chamber_log,
            connection_state.channel,
            connection_state.exchange_pascal,
            10,
        )

        register_pubsub_client(
            MIModeMessageQueueClient,
            settings.pascal.mq.mi_mode,
            connection_state.channel,
            connection_state.exchange_pascal,
            10,
        )

        register_live_client(
            LiveVideoFragmentsMessageQueueClient,
            settings.rheed.mq.live_video,
            connection_state.channel,
            connection_state.exchange_rheed,
            10,
        )

        register_live_client(
            LiveCameraMessageQueueClient,
            settings.rheed.mq.live_camera,
            connection_state.channel,
            connection_state.exchange_rheed,
            10,
        )

        register_live_client(
            LiveDetectionMessageQueueClient,
            settings.detection.mq.live_detection,
            connection_state.channel,
            connection_state.exchange_rheed,
            10,
        )


        register_live_client(
            LiveSTFTMessageQueueClient,
            settings.rheed.mq.live_stft,
            connection_state.channel,
            connection_state.exchange_rheed,
            10,
        )

        register_live_client(
            LiveIntegratorMessageQueueClient,
            settings.rheed.mq.live_integrator,
            connection_state.channel,
            connection_state.exchange_rheed,
            10,
        )


        for client in clients:
            logging.info(f"Starting {client.client_name} state")
            await client.start_state()
            logging.info(f"Starting {client.client_name} control")
            await client.start_control()

        periodic_task = asyncio.create_task(periodic_update(1))
        
        try:
            while not shutdown_event.is_set():
                if queue.empty():
                    # this prevent the queue.get() blocked the shutdown event
                    await asyncio.sleep(0.1)
                    continue
                else:
                    data = await queue.get()
                    # print("data", decode_json(data))
                    yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            logging.info("State generator cancelled")
            periodic_task.cancel()
            logging.info("periodic task cancelled")
            for client in clients:
                await client.stop()
            logging.info("clients stopped")

        finally:
            logging.info("Stopping clients of nodes state in finally block")
            periodic_task.cancel()
            logging.info("periodic task cancelled")
            for client in clients:
                await client.stop()
            logging.info("clients stopped")

        
    return StreamingResponse(state_generator(), media_type="text/event-stream")
