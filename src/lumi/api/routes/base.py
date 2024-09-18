from collections.abc import Awaitable, Callable
from typing import Any, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import struct
import logging
from pathlib import Path
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, Request, APIRouter
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse

from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    BasicStreamClient,
)

import traceback

async def generic_websocket_handler(
    websocket: WebSocket,
    client_class: type[BasicStreamClient],
    client_params: dict,
    send_function: Callable[[Any, dict], Awaitable[bool]],
    endpoint_name: str,
    initial_data_function: Optional[Callable[[], Awaitable[list[Any]]]] = None,
):
    async def on_live_message_callback(message: AbstractIncomingMessage):
        return await send_function(message.body, message.headers)

    # client = None
    client = client_class(
        **client_params,
        on_response_callback=on_live_message_callback,
        on_state_callback=None,
        client_name=f"Live {endpoint_name}",
        time_out=10,
    )
    await client.start_control()
    # client.start_state()

    async def on_message():
        nonlocal client
        try:
            logging.info(f"start {endpoint_name} on_message loop")
            async for message in websocket.iter_text():
                logging.info(f"{endpoint_name} on message {message}")
                if message == "start_server":
                    if client is not None:
                        await client.start_server_streaming()
                elif message == "stop_server":
                    if client is not None:
                        await client.stop_server_streaming()
                # elif message == "init":
                elif message == "start_streaming":
                    if not client.is_main_running():
                        if initial_data_function is not None:
                            initial_data = await initial_data_function()
                            for data, headers in initial_data:
                                await send_function(data, headers)
                                logging.info(f"{endpoint_name} send initialization data {headers}")

                        await client.start_main()

                elif message == "end_streaming":
                    if client is not None:                        
                        await client.stop_main()
                        client = None

                logging.debug(message)
                await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"{endpoint_name} on_message error: {e}")

            error_info = traceback.extract_tb(e.__traceback__)[-1]
            file_name = error_info.filename
            line_number = error_info.lineno
            logging.error(f"{endpoint_name} Error in {file_name} at line {line_number}: {str(e)}")

        logging.info(f"end {endpoint_name} on message loop")

    await websocket.accept()
    logging.info(f"{endpoint_name} websocket accepted")


    ws_in_task = asyncio.create_task(on_message(), name=f"ws_{endpoint_name}_in")
    try:
        await ws_in_task
    except Exception as e:
        logging.info(f"{endpoint_name} websocket received an exception: {e}")

    # await asyncio.Future()
    if client is not None:
        await client.stop()
    logging.info(f"{endpoint_name} websocket exit")
