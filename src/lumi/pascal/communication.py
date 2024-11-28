from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
)
from lumi.base.models import BaseRequestMessageHeader, RequestMessageQueueMessage, ResponseMessageQueueMessage, StreamMessageQueueMessage, UpdateMessageQueueMessage
import numpy as np
import datetime
import struct
import time
import argparse
import logging

# from video_stream import VideoCompressor, VideoRecorder
import json
import asyncio

import queue
from .log_reader import LogReader, LogContentHeader

from .mi_mode import MIModeServer, MIModeExecution, MIModeResponseHeader
from ..base.message_queue import (
    BasicServer,
    BasicStreamServer,
    BasicClient,
    BasicStreamClient,
    PubSubClient,
    PubSubServer,
    BaseMessageQueueMessage,
    BaseControlMixin,
)
from ..utils.common import decode_json, encode_json

from typing import Callable, List, Dict, Any, Awaitable, Optional, TypedDict


# class ChamberLogMessageHeader(LogContentHeader):
#     """Interface for message queue headers that require type and success fields"""
#     type: str
#     success: bool

# class MIModeMessageHeader(MIModeResponseHeader):
#     """Interface for message queue headers that require type and success fields"""
#     type: str
#     success: bool

class ChamberLogMessageQueueServer(BasicServer):
    log_reader: LogReader

    def __init__(
        self,
        log_reader,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            routing_key=routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )

        self.log_reader = log_reader

    def update_state(self):
        # update the state at each read out
        self.state.update({"entries": self.log_reader.entries, "file_path": self.log_reader._log_file_path})

        # update the state at each read out
    async def on_message(self, message: AbstractIncomingMessage) -> ResponseMessageQueueMessage:
        # st = time.time()
        log, headers = (
            self.log_reader.get_log()
        )  # this get the latest frame from the peek queue

        if log is not None and headers is not None:
            body = json.dumps(log).encode()
            response = self.create_response_message(
                body,
                headers,
                request_type="log",
                response_type="log",
                succ=True,
                error_type="",
                error_message="",
            )
        else:
            response = self.create_response_message(
                body="".encode(),
                headers={},
                request_type="log",
                response_type="log",
                succ=False,
                error_type="accessError",
                error_message="Failed to access log file",
            )
        return response
        

class ChamberLogMessageQueueClient(BasicClient):
    async def get_log(self):
        logging.info(f"{self.log_prefix} get Log")
        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="log")
        return await self.request(request_message)



class MIModeMessageQueueServer(PubSubServer):
    mi_mode_server: MIModeServer

    def __init__(
        self,
        mi_mode_server: MIModeServer,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        request_routing_key: str,
        response_routing_key: str,
        update_routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            request_routing_key=request_routing_key,
            response_routing_key=response_routing_key,
            update_routing_key=update_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )

        self.mi_mode_server = mi_mode_server

    def update_state(self):
        # update the state at each read out
        self.state.update({"latest_executed_commands_uuid": self.mi_mode_server.get_latest_execution().uuid, "num_executions": len(self.mi_mode_server.list_executions())})

        # update the state at each read out

    async def handle_register_commands_request(self, content, headers, message: AbstractIncomingMessage):
        def register_commands_callback(future : asyncio.Future):
            update_message = self.create_update_message(
                body=future.result(),
                headers={},
                update_type="execution_commands",
            )
            asyncio.create_task( self.publish_update(update_message) )

        try:
            command_future = self.mi_mode_server.register_commands(content["commands"], content["commands_uuid"])
            command_future.add_done_callback(register_commands_callback)

            response = self.create_response_message(
                body=b"",
                headers={},
                request_type="register_commands",
                response_type="register_commands",
                succ=True,
            )
        except Exception as e:
            logging.error(f"{self.server_name} Error in execution: {e}")
            response = self.create_response_message(
                body=b"",
                headers={},
                request_type="register_commands",
                response_type="register_commands",
                succ=False,
                error_type="executionError",
                error_message=str(e),
            )
            # raise e

        return response
    
    async def handle_list_execution_request(self):
        body = encode_json(self.mi_mode_server.list_executions())
        response = self.create_response_message(
            body=body,
            headers={},
            request_type="list_execution",
            response_type="list_execution",
            succ=True,
        )

        return response


    async def on_message(self, message: AbstractIncomingMessage) -> ResponseMessageQueueMessage:
        headers : BaseRequestMessageHeader = message.headers

        request_type = headers["request_type"]
        if request_type == "register_commands":
            package = decode_json( message.body.decode() )
            return await self.handle_register_commands_request(package, headers, message)
        
        elif request_type == "list_execution":
            return await self.handle_list_execution_request()
        
        else:
            return self.create_response_message(
                body="".encode(),
                headers={},
                request_type=request_type,
                response_type=request_type,
                succ=False,
                error_type="unknownRequestError",
                error_message=f"Unknown request type: {request_type}",
            )
        
    async def on_update(self) -> UpdateMessageQueueMessage | None:
        if self.mi_mode_server.update_queue.empty():
            return None
        content, headers = self.mi_mode_server.update_queue.get()
        return self.create_update_message(
            body=encode_json(content),
            headers=headers,
            update_type=headers["update_content"],
        )

class MIModeMessageQueueClient(PubSubClient):
    async def register_commands(self, commands: str, commands_uuid: str = None):

        logging.info(f"{self.client_name} request execution of commands to MIMode")
        message = self.create_request_message(
            body=encode_json({"commands": commands, "commands_uuid": commands_uuid}), 
            headers={}, request_type="register_commands")
        return await self.request(message)
    
    async def list_execution(self):
        logging.info(f"{self.client_name} request execution of commands to MIMode")
        message = self.create_request_message(
            body="", 
            headers={}, request_type="list_execution")

        return await self.request(message)



class LiveChamberLogMessageQueueServer(BasicStreamServer):
    log_reader: LogReader
    log_queue: queue.Queue

    def __init__(
        self,
        log_reader,
        log_queue,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        publish_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        """ """
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            publish_routing_key=publish_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )
        self.log_reader = log_reader
        self.log_queue = log_queue

    def update_state(self):
        self.state.update({"entries": self.log_reader.entries, "file_path": self.log_reader._log_file_path})

    async def on_streaming(self) -> StreamMessageQueueMessage | None:
        if not self.log_queue.empty():
            log, headers = self.log_queue.get()
            body = json.dumps(log).encode()

            response = self.create_stream_message(
                body=body, 
                headers=headers,
                stream_type="live_log",
            )
        
        else:
            response = None

        return response
    
    
class LiveChamberLogMessageQueueClient(BasicStreamClient):
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        on_state_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        client_name: str,
        time_out: float,
    ) -> None:
        super().__init__(
            channel,
            exchange,
            routing_key,
            control_routing_key,
            state_routing_key,
            on_response_callback,
            on_state_callback,
            client_name,
            time_out,
        )
