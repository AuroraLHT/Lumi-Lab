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

from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractConnection,
    AbstractChannel,
    AbstractExchange,
)

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    BasicStreamClient,
)

from ..models import WebsocketMessageHeaders
from ..utils import unpack_websocket_payload, parse_payload, pack_websocket_payload
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
                                logging.info(
                                    f"{endpoint_name} send initialization data {headers}"
                                )

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
            logging.error(
                f"{endpoint_name} Error in {file_name} at line {line_number}: {str(e)}"
            )

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


class WebsocketHandler:
    def __init__(self, websocket: WebSocket, endpoint_name: str):
        self.websocket = websocket
        self.endpoint_name = endpoint_name
        self.clients = {}
        self.client_initial_data_functions = {}
        # self.clients_queue = asyncio.Queue()

    async def send_data(self, target: str, headers: dict, data: bytes):
        try:
            websocket_headers = WebsocketMessageHeaders(
                target=target, operation="data", payload_type="bytes"
            )
            await self.websocket.send_bytes(
                pack_websocket_payload(data, headers, websocket_headers.to_dict())
            )
        except Exception as e:
            logging.error(f"{target} send_data error: {e}")
            return True
        return False

        
    def register_client(
        self,
        client: BasicStreamClient,
        client_initial_data_function: Optional[
            Callable[[], Awaitable[list[Any]]]
        ] = None,
    ):
        """
        Register a client to the websocket handler. The handler would forward the data from the client to the websocket.

        Name is used to identify the client in the websocket.
        which would be in the topic field of the websocket headers
        """

        async def on_response_callback(message: AbstractIncomingMessage):
            await self.send_data(client.client_name, message.headers, message.body)
            # websocket_headers = WebsocketMessageHeaders(
            #     target=client.client_name, operation="data", payload_type="bytes"
            # )
            # await self.websocket.send_bytes(
            #     pack_websocket_payload(data, headers, websocket_headers.to_dict())
            # )
            # await self.clients_queue.put((WebsocketMessageHeaders(target=client_name, operation="data"), headers, data))

        self.clients[client.client_name] = client
        self.client_initial_data_functions[client.client_name] = client_initial_data_function
        client.update_reponse_callback(on_response_callback)

    def parse_message(self, message: bytes):
        websocket_headers, headers, payload = unpack_websocket_payload(message)
        parsed_payload = parse_payload(payload, websocket_headers)

        return websocket_headers, headers, parsed_payload

    def parse_topic(self, topic: str):
        # we expect topic to be in format "target.operation"
        target, operation = topic.split(".")
        return target, operation

    async def get_initial_data(self, client_name: str):
        if client_name not in self.client_initial_data_functions or self.client_initial_data_functions[client_name] is None:
            return []
        else:
            return await self.client_initial_data_functions[client_name]()

    async def on_client_message(
        self,
        client_name: str,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ):
        if client_name not in self.clients:
            logging.error(f"client {client_name} not found")
            return
        else:
            client: BasicStreamClient = self.clients[client_name]

        if operation == "control":
            if headers["type"] == "start_server":
                if client is not None:
                    await client.start_server_streaming()
            elif headers["type"] == "stop_server":
                if client is not None:
                    await client.stop_server_streaming()

            elif headers["type"] == "start_streaming":
                if not client.is_main_running():
                    if client_name in self.client_initial_data_functions is not None:
                        initial_data = await self.get_initial_data(client_name)
                        for data, headers in initial_data:
                            await self.send_data(client_name, headers, data)
                            logging.info(
                                f"{self.endpoint_name} send initialization data {headers}"
                            )

                    await client.start_main()

            elif headers["type"] == "end_streaming":
                if client is not None:
                    await client.stop_main()
                    client = None

    async def handle_incoming_messages(self):
        logging.info(f"start {self.endpoint_name} on_message loop")
        async for message in self.websocket.iter_bytes():
            websocket_headers, headers, parsed_payload = self.parse_message(message)
            await self.on_client_message(
                websocket_headers.target,
                websocket_headers.operation,
                headers,
                parsed_payload,
            )

    async def start(self):
        await self.websocket.accept()
        logging.info(f"{self.endpoint_name} websocket accepted")

        ws_in_task = asyncio.create_task(self.handle_incoming_messages(), name=f"ws_{self.endpoint_name}_in")
        try:
            await ws_in_task
        except Exception as e:
            raise e
            logging.info(f"{self.endpoint_name} websocket received an exception: {e}")

        # await asyncio.Future()
        for client in self.clients.values():
            await client.stop()
        logging.info(f"{self.endpoint_name} websocket exit")
