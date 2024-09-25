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
from lumi.base.message_queue import BasicClient, MessageQueueResponse

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


class BaseClientMessageMapper:
    client : BasicClient

    def __init__(self, client: BasicClient):
        self.client = client

    async def map(
        self,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ):
        if operation == "request":
            return await self.client.request(parsed_payload, headers)
        
        return self.client.empty_response


class BaseStreamClientMessageMapper:
    client: BasicStreamClient
    initial_data_function: Optional[Callable[[], Awaitable[list[Any]]]] = None

    def __init__(self, client: BasicStreamClient, initial_data_function: Optional[Callable[[], Awaitable[list[Any]]]] = None):
        self.client = client
        self.initial_data_function = initial_data_function

    async def stream_map(
        self,
        target: str,
        headers: dict,
        body: bytes,
    ):
        # by default, do nothing, just return the original target, headers, body
        return target, headers, body

    async def map(
        self,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ):
        if operation == "control":
            if headers["type"] == "start_server":
                if self.client is not None:
                    await self.client.start_server_streaming()
            elif headers["type"] == "stop_server":
                if self.client is not None:
                    await self.client.stop_server_streaming()

            elif headers["type"] == "start_streaming":
                if not self.client.is_main_running():
                    if self.initial_data_function is not None:
                        initial_data = await self.initial_data_function()
                        for data, headers in initial_data:
                            await self.send_data(self.client.client_name, headers, data)
                    await self.client.start_main()

            elif headers["type"] == "end_streaming":
                await self.client.stop_main()


class WebsocketMultiClientsHandler:
    def __init__(self, websocket: WebSocket, endpoint_name: str):
        self.websocket = websocket
        self.endpoint_name = endpoint_name
        self.stream_clients = {}
        self.clients = {}
        self.stream_client_initial_data_functions = {}
        # self.clients_queue = asyncio.Queue()

    async def send_data(self, target: str, headers: dict, data: bytes):
        try:
            websocket_headers = WebsocketMessageHeaders(
                target=target, operation="data", payload_type="bytes"
            )
            payload = pack_websocket_payload(data, headers, websocket_headers.to_dict())
            # logging.info(f"{target} send_data payload size: {len(payload)}")
            await self.websocket.send_bytes(payload)

        except Exception as e:
            logging.error(f"{target} send_data error: {e}")
            return True
        return False
    
    async def send_response(self, target: str, response: MessageQueueResponse):
        try:
            websocket_headers = WebsocketMessageHeaders(
                target=target, operation="response", payload_type="bytes"
            )
            payload = pack_websocket_payload(response.body, response.headers, websocket_headers.to_dict())
            logging.info(f"{target} send_response payload size: {len(payload)}")

            await self.websocket.send_bytes(payload)
        except Exception as e:
            logging.error(f"{target} send_response error: {e}")
            return True
        return False

    def register_client(
        self,
        client: BaseClientMessageMapper,
    ):        
        self.clients[client.client.client_name] = client

    def register_stream_client(
        self,
        client: BaseStreamClientMessageMapper,
    ):
        """
        Register a client to the websocket handler. The handler would forward the data from the client to the websocket.

        Name is used to identify the client in the websocket.
        which would be in the topic field of the websocket headers
        """

        async def on_response_callback(message: AbstractIncomingMessage):
            target, headers, body = await client.stream_map(client.client.client_name, message.headers, message.body)
            await self.send_data(target, headers, body)

        client.client.update_reponse_callback(on_response_callback)
        self.stream_clients[client.client.client_name] = client

    def parse_message(self, message: bytes):
        websocket_headers, headers, payload = unpack_websocket_payload(message)
        parsed_payload = parse_payload(payload, websocket_headers)

        return websocket_headers, headers, parsed_payload

    async def handle_incoming_messages(self):
        logging.info(f"start {self.endpoint_name} on_message loop")
        async for message in self.websocket.iter_bytes():
            websocket_headers, headers, parsed_payload = self.parse_message(message)

            if websocket_headers.target in self.stream_clients:
                mapper : BaseStreamClientMessageMapper = self.stream_clients[websocket_headers.target]
                await mapper.map(
                    websocket_headers.operation,
                    headers,
                    parsed_payload,
                )

            elif websocket_headers.target in self.clients:
                mapper : BaseClientMessageMapper = self.clients[websocket_headers.target]
                response =await mapper.map(
                    websocket_headers.operation,
                    headers,
                    parsed_payload,
                )
                try:    
                    if response is not None and response.body is not None:
                        await self.send_response(websocket_headers.target, response)
                except Exception as e:
                    logging.error(f"{websocket_headers.target} send_response error: {e}. Response: {response}")
                    raise e
                
    async def start(self):
        await self.websocket.accept()
        logging.info(f"{self.endpoint_name} websocket accepted")

        ws_in_task = asyncio.create_task(self.handle_incoming_messages(), name=f"ws_{self.endpoint_name}_in")
        try:
            await ws_in_task
        except Exception as e:
            logging.info(f"{self.endpoint_name} websocket received an exception: {e}")
            raise e

        # await asyncio.Future()
        for client_mapper in self.stream_clients.values():
            client_mapper : BaseStreamClientMessageMapper

            await client_mapper.client.stop()
        logging.info(f"{self.endpoint_name} websocket exit")
