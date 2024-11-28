from collections.abc import Awaitable, Callable
from typing import Any, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import logging
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, Request, APIRouter
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse

from aio_pika.abc import AbstractIncomingMessage

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    LiveChamberLogMessageQueueClient,
    MIModeMessageQueueClient
)

import traceback

from ..websockets.base import generic_websocket_handler, WebsocketMultiClientsHandler, BaseClientMessageMapper, BaseStreamClientMessageMapper
from ..websockets.chamber import LiveChamberLogClientMessageMapper, MIModePubSubClientMessageMapper
from ..utils import update_state
from ..connection_state import ConnectionState

router = APIRouter()
@router.get("/chamber/log")
async def get_chamber_log(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state
    response = await connection_state.log_client.get_log()

    if len(response.body) == 0:
        return Response(
            content=response.body,
            status_code=500,
            media_type="application/json",
            headers={"msg": "fail to acquire log"},
        )

    else:
        return Response(
            content=response.body,
            status_code=200,
            media_type="application/json",
            headers={str(k): str(v) for k, v in response.headers.items()},
        )



# @router.websocket("/chamber/log/live")
# async def chamber_log_live(websocket: WebSocket):
#     connection_state : ConnectionState = websocket.app.state.connection_state

#     async def send_json(body, headers):
#         try:
#             json_text = body.decode()
#             await websocket.send_text(json_text)
#         except Exception as e:
#             logging.error(f"/chamber/log/live send_json error: {e}")
#             return True
#         return False

#     client_params = {
#         "channel": connection_state.channel,
#         "exchange": connection_state.exchange_chamber,
#         "routing_key": "live_log",
#         "control_routing_key": "live_log_ctrl",
#         "state_routing_key": "live_log_state",
#     }

#     await generic_websocket_handler(
#         websocket,
#         LiveChamberLogMessageQueueClient,
#         client_params,
#         send_json,
#         "chamber log"
#     )


@router.websocket("/chamber/live")
async def chamber_live(websocket: WebSocket):
    async def send_json(body, headers):
        try:
            json_text = body.decode()
            await websocket.send_text(json_text)
        except Exception as e:
            logging.error(f"/chamber/log/live send_json error: {e}")
            return True
        return False

    connection_state : ConnectionState = websocket.app.state.connection_state

    live_log_client = LiveChamberLogMessageQueueClient(
        channel = connection_state.channel,
        exchange = connection_state.exchange_chamber,
        routing_key = "live_log",
        control_routing_key = "live_log_ctrl",
        state_routing_key = "live_log_state",
        on_response_callback=send_json,
        on_state_callback=None,
        client_name="Live Chamber Log",
        time_out=10,
    )

    await live_log_client.start_control()

    mi_mode_client = MIModeMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_chamber,
        request_routing_key="mi_mode_request",
        response_routing_key="mi_mode_response",
        update_routing_key="mi_mode_update",
        control_routing_key="mi_mode_ctrl",
        state_routing_key="mi_mode_state",
        on_update_callback=None,
        on_state_callback=None,
        client_name="MI Mode",
        time_out=10,
    )

    await mi_mode_client.start_control()

    
    websocket_handler = WebsocketMultiClientsHandler(websocket, "Chamber")
    websocket_handler.register_stream_client( LiveChamberLogClientMessageMapper(live_log_client), )
    websocket_handler.register_pubsub_client( MIModePubSubClientMessageMapper(mi_mode_client), )

    await websocket_handler.start()

# @router.websocket("/chamber/log/live")
# async def chamber_log_live(websocket: WebSocket):
#     connection_state : ConnectionState = websocket.app.state.connection_state

#     async def send_json(body, headers):
#         try:
#             json_text = body.decode()
#             await websocket.send_text(json_text)
#         except Exception as e:
#             logging.error(f"/chamber/log/live send_json error: {e}")
#             return True
#         return False

#     client_params = {
#         "channel": connection_state.channel,
#         "exchange": connection_state.exchange_chamber,
#         "routing_key": "live_log",
#         "control_routing_key": "live_log_ctrl",
#         "state_routing_key": "live_log_state",
#     }

#     await generic_websocket_handler(
#         websocket,
#         LiveChamberLogMessageQueueClient,
#         client_params,
#         send_json,
#         "chamber log"
#     )



@router.get("/chamber/log/live/state")
async def get_chamber_log_state(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state

    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"chamber log state put {message.body.decode()}")
            await queue.put(message.body)

        live_log_client = LiveChamberLogMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_chamber,
            routing_key="live_log",
            control_routing_key="live_log_ctrl",
            state_routing_key="live_log_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live Log Monitor",
            time_out=10,
        )
        await live_log_client.start_state()
        await live_log_client.start_control()
        try:
            state = await asyncio.wait_for(live_log_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"

        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"Chamber Log state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_log_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")
            

@router.get("/chamber/mi/live/state")
async def get_chamber_mi_state(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state

    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"chamber log state put {message.body.decode()}")
            await queue.put(message.body)

        mi_mode_client = MIModeMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_chamber,
            routing_key="mi_mode",
            control_routing_key="mi_mode_ctrl",
            state_routing_key="mi_mode_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="MI mode Monitor",
            time_out=10,
        )
        await mi_mode_client.start_state()
        await mi_mode_client.start_control()
        try:
            state = await asyncio.wait_for(mi_mode_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"

        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"MI mode state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await mi_mode_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")
            