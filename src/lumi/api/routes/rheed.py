from collections.abc import Awaitable, Callable
from typing import Any, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import struct
import logging
from pathlib import Path
from dataclasses import dataclass
import traceback

from fastapi import FastAPI, WebSocket, Request, APIRouter
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.responses import StreamingResponse

from aio_pika.abc import AbstractIncomingMessage

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    LiveVideoFragmentsMessageQueueClient,
    LiveDetectionMessageQueueClient,
    LiveIntegratorMessageQueueClient,
    LiveSTFTMessageQueueClient
)
from ..websockets.base import generic_websocket_handler, WebsocketMultiClientsHandler, BaseClientMessageMapper, BaseStreamClientMessageMapper
from ..websockets.rheed import IntegratorClientMessageMapper, STFTClientMessageMapper, LiveDetectionStreamClientMessageMapper, VideoFragmentsMessageMapper

from ..utils import update_state, pack_payload
from ..connection_state import ConnectionState

router = APIRouter()

@router.get("/RHEED/cam/live/state")
async def get_rheed_cam_state(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state

    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"RHEED cam state put {message.body.decode()}")
            await queue.put(message.body)

        live_video_client = LiveVideoFragmentsMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_rheed,
            routing_key="live_video",
            control_routing_key="live_video_ctrl",
            state_routing_key="live_video_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live RHEED Cam Monitor",
            time_out=10,
        )
        await live_video_client.start_state()
        await live_video_client.start_control()

        try:
            state = await asyncio.wait_for(live_video_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"

        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED cam state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_video_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")




@router.get("/RHEED/image")
async def read_root(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state
    response = await connection_state.image_client.get_live_image()
    # logging.info(headers)
    return Response(
        content=response.body,
        status_code=200,
        media_type="application/octet-stream",
        headers={str(k): str(v) for k, v in response.headers.items()},
    )



# @router.websocket("/RHEED/cam/live")
# async def rheed_cam_live(websocket: WebSocket):
#     connection_state : ConnectionState = websocket.app.state.connection_state

#     async def send_fragment(fragment, headers):

#         try:
#             # header_json = json.dumps(headers)
#             # header_length = struct.pack(">I", len(header_json))
#             # data = header_length + header_json.encode("utf-8") + fragment
#             data = pack_payload(fragment, headers)
#             await websocket.send_bytes(data)
#         except Exception as e:
#             logging.error(f"/RHEED/detection/live send_fragment error: {e}")
#             return True
#         return False

#     client_params = {
#         "channel": connection_state.channel,
#         "exchange": connection_state.exchange_rheed,
#         "routing_key": "live_video",
#         "control_routing_key": "live_video_ctrl",
#         "state_routing_key": "live_video_state",
#     }

#     await generic_websocket_handler(
#         websocket,
#         LiveVideoFragmentsMessageQueueClient,
#         client_params,
#         send_fragment,
#         "RHEED cam",
#         connection_state.video_fragment_client.get_initial,
#     )

@router.websocket("/RHEED/cam/live")
async def rheed_analysis_live(websocket: WebSocket):
    connection_state : ConnectionState = websocket.app.state.connection_state

    live_video_client = LiveVideoFragmentsMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_video",
        control_routing_key="live_video_ctrl", 
        state_routing_key="live_video_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live RHEED Camera",
        time_out=10,
    )
    await live_video_client.start_control()

    websocket_handler = WebsocketMultiClientsHandler(websocket, "RHEED")
    websocket_handler.register_stream_client( BaseStreamClientMessageMapper(live_video_client), )
    websocket_handler.register_client( VideoFragmentsMessageMapper(connection_state.video_fragment_client), )

    await websocket_handler.start()

# @router.websocket("/RHEED/detection/live")
# async def rheed_detection_live(websocket: WebSocket):
#     connection_state = websocket.app.state.connection_state

#     async def send_payload(payload, header):
#         try:
#             # header_json = json.dumps(header)
#             # header_length = struct.pack(">I", len(header_json))
#             # data = header_length + header_json.encode("utf-8") + payload
#             data = pack_payload(payload, header)
#             await websocket.send_bytes(data)
#         except Exception as e:
#             logging.error(f"/RHEED/detection/live send_payload error: {e}")
#             return True
#         return False

#     client_params = {
#         "channel": connection_state.channel,
#         "exchange": connection_state.exchange_rheed,
#         "routing_key": "live_detection",
#         "control_routing_key": "live_detection_ctrl",
#         "state_routing_key": "live_detection_state",
#     }

#     await generic_websocket_handler(
#         websocket,
#         LiveDetectionMessageQueueClient,
#         client_params,
#         send_payload,
#         "RHEED detection"
#     )

@router.websocket("/RHEED/analysis/live")
async def rheed_analysis_live(websocket: WebSocket):
    connection_state : ConnectionState = websocket.app.state.connection_state

    live_detection_client = LiveDetectionMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_detection",
        control_routing_key="live_detection_ctrl",
        state_routing_key="live_detection_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live Detection",
        time_out=10,
    )
    await live_detection_client.start_control()

    live_integrator_client = LiveIntegratorMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_integrator",
        control_routing_key="live_integrator_ctrl",
        state_routing_key="live_integrator_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live Integrator",
        time_out=10,
    )
    await live_integrator_client.start_control()

    live_stft_client = LiveSTFTMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_stft",
        control_routing_key="live_stft_ctrl",
        state_routing_key="live_stft_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live STFT",
        time_out=10,
    )
    await live_stft_client.start_control()

    
    websocket_handler = WebsocketMultiClientsHandler(websocket, "Live Analysis")
    websocket_handler.register_stream_client( LiveDetectionStreamClientMessageMapper(live_detection_client), )
    websocket_handler.register_stream_client( BaseStreamClientMessageMapper(live_integrator_client), )
    websocket_handler.register_stream_client( BaseStreamClientMessageMapper(live_stft_client), )

    websocket_handler.register_client( IntegratorClientMessageMapper(connection_state.integrator_client), )
    websocket_handler.register_client( STFTClientMessageMapper(connection_state.stft_client), )

    await websocket_handler.start()


# TODO: refactor all the state SSE into in url
# TODO: maybe merge them into Websocket? 
@router.get("/RHEED/detection/live/state")
async def get_rheed_detection_state(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state

    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"detection state put {message.body.decode()}")

            await queue.put(message.body)

        live_detection_client = LiveDetectionMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_rheed,
            routing_key="live_detection",
            control_routing_key="live_detection_ctrl",
            state_routing_key="live_detection_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live Detection Monitor",
            time_out=10,
        )
        await live_detection_client.start_state()
        await live_detection_client.start_control()
        try:
            state = await asyncio.wait_for(live_detection_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"
        # print(state)
        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED detection state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_detection_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")


@router.get("/RHEED/stft/live/state")
async def get_rheed_stft_state(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state

    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"detection state put {message.body.decode()}")

            await queue.put(message.body)

        live_stft_client = LiveSTFTMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_rheed,
            routing_key="live_stft",
            control_routing_key="live_stft_ctrl",
            state_routing_key="live_stft_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live STFT Monitor",
            time_out=10,
        )
        await live_stft_client.start_state()
        await live_stft_client.start_control()
        try:
            state = await asyncio.wait_for(live_stft_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"
        # print(state)
        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED stft state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_stft_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")


@router.get("/RHEED/integrator/live/state")
async def get_rheed_stft_state(request: Request):
    connection_state : ConnectionState = request.app.state.connection_state

    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"detection state put {message.body.decode()}")

            await queue.put(message.body)

        live_integrator_client = LiveIntegratorMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_rheed,
            routing_key="live_integrator",
            control_routing_key="live_integrator_ctrl",
            state_routing_key="live_integrator_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live Integrator Monitor",
            time_out=10,
        )
        await live_integrator_client.start_state()
        await live_integrator_client.start_control()
        try:
            state = await asyncio.wait_for(live_integrator_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"
        # print(state)
        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED integrator state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_integrator_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")