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

from lumi.base.message_queue import (
    BasicClient,
    BasicStreamClient,
    PubSubClient,
    BaseMessageQueueMessage,
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


class BaseClientMessageMapper:
    client: BasicClient

    def __init__(self, client: BasicClient):
        self.client = client

    async def map(
        self,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ) -> BaseMessageQueueMessage:
        raise NotImplementedError(
            f"BaseClientMessageMapper <{self.client.client_name}> does not implement map for operation: {operation}"
        )


class BasePubSubClientMessageMapper:
    client: PubSubClient

    def __init__(self, client: PubSubClient):
        self.client = client

    async def map(
        self,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ) -> BaseMessageQueueMessage:
        raise NotImplementedError(
            f"PubSubClientMessageMapper <{self.client.client_name}> does not implement map for operation: {operation}"
        )

    async def update_map(self, target: str, headers: dict, body: bytes):
        return target, headers, body


class BaseStreamClientMessageMapper:
    client: BasicStreamClient
    initial_data_function: Optional[Callable[[], Awaitable[list[Any]]]] = None

    def __init__(
        self,
        client: BasicStreamClient,
        initial_data_function: Optional[Callable[[], Awaitable[list[Any]]]] = None,
    ):
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
            
        response = None
        try:
            if operation == "control_request":
                if headers["control_request_type"] == "start_server":
                    if self.client is not None:
                        response = await self.client.start_server_streaming()

                elif headers["control_request_type"] == "stop_server":
                    if self.client is not None:
                        response = await self.client.stop_server_streaming()
                else:
                    logging.error(f"{self.client.client_name} stream_map: control_request_type not recognized: {headers['control_request_type']}", exc_info=True)
                    # raise ValueError(f"{self.client.client_name} stream_map: control_request_type not recognized: {headers['control_request_type']}")
                    response = self.client.create_response_message(
                        body="",
                        headers={},
                        request_type=headers["control_request_type"],
                        response_type="bytes",
                        succ=False,
                        error_type="UnknownRequest",
                        error_message=f"Invalid control_request_type in BaseStreamClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}",
                    )
            elif operation == "command":
                if headers["command_type"] == "start_streaming":
                    if not self.client.is_main_running():
                        # if self.initial_data_function is not None:
                        #     initial_data = await self.initial_data_function()
                        #     for data, headers in initial_data:
                        #         await self.send_data(self.client.client_name, headers, data)
                        await self.client.start_main()
                        response = None

                elif headers["command_type"] == "end_streaming":
                    await self.client.stop_main()
                    response = None
                else:
                    logging.error(f"{self.client.client_name} stream_map: command_type not recognized: {headers['command_type']}", exc_info=True)
                    # raise ValueError(f"{self.client.client_name} stream_map: command_type not recognized: {headers['command_type']}")
                    response = self.client.create_response_message(
                        body="",
                        headers={},
                        request_type=headers["command_type"],
                        response_type="bytes",
                        succ=False,
                        error_type="UnknownRequest",
                        error_message=f"Invalid command_type in BaseStreamClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}",
                    )
            else:
                logging.error(f"{self.client.client_name} stream_map: operation not recognized: {operation}", exc_info=True)
                # raise ValueError(f"{self.client.client_name} stream_map: operation not recognized: {operation}")
                response = self.client.create_response_message(
                    body="",
                    headers={},
                    request_type=operation,
                    response_type="bytes",
                    succ=False,
                    error_type="UnknownRequest",
                    error_message=f"Invalid operation in BaseStreamClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}",
                )
        except Exception as e:
            logging.error(
                f"{self.client.client_name} map error: {e}. \n\n headers: {headers} \n\n parsed_payload: {parsed_payload}", exc_info=True
            )
            response = self.client.create_response_message(
                body="",
                headers={},
                request_type=headers["request_type"],
                response_type=headers["request_type"],
                succ=False,
                error_type="RequestExecutionError",
                error_message=f"Error in BaseStreamClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}. Error: {e}",
            )

        return response


class WebsocketMultiClientsHandler:
    def __init__(self, websocket: WebSocket, endpoint_name: str):
        self.websocket = websocket
        self.endpoint_name = endpoint_name
        self.stream_clients = {}
        self.clients = {}
        self.pubsub_clients = {}
        self.stream_client_initial_data_functions = {}
        # self.clients_queue = asyncio.Queue()

    async def send_data(self, target: str, operation: str, headers: dict, data: bytes):
        try:
            websocket_headers = WebsocketMessageHeaders(
                target=target, operation=operation, payload_type="bytes"
            )
            payload = pack_websocket_payload(data, headers, websocket_headers.to_dict())
            logging.debug(f"{target} send_data payload size: {len(payload)}")
            await self.websocket.send_bytes(payload)

        except Exception as e:
            logging.error(f"{target} send_data error: {e}")
            return True
        return False

    def register_pubsub_client(
        self,
        mapper: BasePubSubClientMessageMapper,
    ):

        async def on_update_callback(message: AbstractIncomingMessage):
            target, headers, body = await mapper.update_map(
                mapper.client.client_name, message.headers, message.body
            )
            await self.send_data(target, headers, body)

        mapper.client.update_on_update_callback(on_update_callback)
        self.pubsub_clients[mapper.client.client_name] = mapper

    def register_client(
        self,
        mapper: BaseClientMessageMapper,
    ):
        self.clients[mapper.client.client_name] = mapper

    def register_stream_client(
        self,
        mapper: BaseStreamClientMessageMapper,
    ):
        """
        Register a client to the websocket handler. The handler would forward the data from the client to the websocket.

        Name is used to identify the client in the websocket.
        which would be in the topic field of the websocket headers
        """

        async def on_response_callback(message: AbstractIncomingMessage):
            """
            This function is called when the client sends a response to the server.
            The response is then sent to the websocket.

            return False if the message is sent successfully, otherwise return True
            """
            target, headers, body = await mapper.stream_map(
                mapper.client.client_name, message.headers, message.body
            )
            try:
                await self.send_data(target, "stream", headers, body)
            except Exception as e:
                logging.error(f"{self.endpoint_name} send_data error: {e}")
                return True
            return False

        mapper.client.update_on_reponse_callback(on_response_callback)
        self.stream_clients[mapper.client.client_name] = mapper

    def parse_message(self, message: bytes):
        try:
            websocket_headers, headers, payload = unpack_websocket_payload(message)
        except Exception as e:
            logging.error(
                f"{self.endpoint_name} parse_message error: {e}.\n\n message: {message}"
            )
            raise e
        try:
            parsed_payload = parse_payload(payload, websocket_headers)
        except Exception as e:
            logging.error(
                f"{self.endpoint_name} parse_payload error: {e}.\n\n payload: {payload} \n\n websocket_headers: {websocket_headers}"
            )
            raise e
        return websocket_headers, headers, parsed_payload

    async def handle_incoming_messages(self):
        logging.info(f"start {self.endpoint_name} on_message loop")
        async for message in self.websocket.iter_bytes():
            websocket_headers, headers, parsed_payload = self.parse_message(message)
            logging.info(
                f"WS Endpoint {self.endpoint_name} handle_incoming_messages: {websocket_headers.target} {websocket_headers.operation}"
            )

            if websocket_headers.target in self.stream_clients:
                logging.info(
                    f"WS Endpoint {self.endpoint_name} handle_incoming_messages: {websocket_headers.target} {websocket_headers.operation} is stream client"
                )
                mapper: BaseStreamClientMessageMapper = self.stream_clients[
                    websocket_headers.target
                ]
                response = await mapper.map(
                    websocket_headers.operation,
                    headers,
                    parsed_payload,
                )
                try:
                    if response is not None and response.body is not None:
                        await self.send_data(
                            websocket_headers.target,
                            response.headers["message_type"],
                            response.headers,
                            response.body,
                        )
                except Exception as e:
                    logging.error(
                        f"{websocket_headers.target} send_data error: {e}. Response: {response}"
                    )
                    raise e

            elif websocket_headers.target in self.clients:
                logging.info(
                    f"WS Endpoint {self.endpoint_name} handle_incoming_messages: {websocket_headers.target} {websocket_headers.operation} is client"
                )
                mapper: BaseClientMessageMapper = self.clients[websocket_headers.target]
                response = await mapper.map(
                    websocket_headers.operation,
                    headers,
                    parsed_payload,
                )
                try:
                    if response is not None and response.body is not None:
                        await self.send_data(
                            websocket_headers.target,
                            response.headers["message_type"],
                            response.headers,
                            response.body,
                        )
                except Exception as e:
                    if response is not None:
                        response_body = response.body
                        response_headers = response.headers
                    else:
                        response_body = None
                        response_headers = None
                    logging.error(
                        f"{websocket_headers.target} send_response error: {e}. Response body: {response_body} headers: {response_headers}"
                    )
                    raise e

            elif websocket_headers.target in self.pubsub_clients:
                logging.info(
                    f"WS Endpoint {self.endpoint_name} handle_incoming_messages: {websocket_headers.target} {websocket_headers.operation} is pubsub client"
                )
                mapper: BasePubSubClientMessageMapper = self.pubsub_clients[
                    websocket_headers.target
                ]
                response = await mapper.map(
                    websocket_headers.operation,
                    headers,
                    parsed_payload,
                )
                try:
                    if response is not None and response.body is not None:
                        await self.send_data(
                            websocket_headers.target,
                            response.headers["message_type"],
                            response.headers,
                            response.body,
                        )
                except Exception as e:
                    logging.error(
                        f"{websocket_headers.target} send_response error: {e}. Response: {response}"
                    )
                    raise e
            else:                
                logging.error(
                    f"WS Endpoint {self.endpoint_name} handle_incoming_messages: {websocket_headers.target} {websocket_headers.operation} is not registered. Registered clients: stream_clients={list(self.stream_clients.keys())}, clients={list(self.clients.keys())}, pubsub_clients={list(self.pubsub_clients.keys())}"
                )

    async def start(self):
        await self.websocket.accept()
        logging.info(f"{self.endpoint_name} websocket accepted")

        ws_in_task = asyncio.create_task(
            self.handle_incoming_messages(), name=f"ws_{self.endpoint_name}_in"
        )
        try:
            await ws_in_task
        except Exception as e:
            logging.error(f"{self.endpoint_name} websocket received an exception: {e}")
            logging.info(
                f"{self.endpoint_name} check the websocket binaryType option. should be 'arraybuffer'."
            )
            raise e

        # await asyncio.Future()
        for client_mapper in self.stream_clients.values():
            client_mapper: BaseStreamClientMessageMapper

            await client_mapper.client.stop()
        logging.info(f"{self.endpoint_name} websocket exit")
