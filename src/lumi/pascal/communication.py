from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
)
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
from .log_reader import LogReader
from .mi_mode import MIModeServer
from ..base.message_queue import (
    BasicServer,
    BasicStreamServer,
    BasicClient,
    BasicStreamClient,
    PubSubClient,
    PubSubServer,
    MessageQueueResponse,
    BaseControlMixin,
)
from ..utils.common import decode_json, encode_json

from typing import Callable, List, Dict, Any, Awaitable


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
    async def on_message(self, message: AbstractIncomingMessage):
        # st = time.time()
        log, headers = (
            self.log_reader.get_log()
        )  # this get the latest frame from the peek queue

        if log is not None and headers is not None:
            headers.update({"type": "log_response", "success": True})
            body = json.dumps(log).encode()
        else:
            body = "".encode()
            headers = {"type": "log_response", "success": False}
        # et = time.time()
        # logging.info(f"[x] Log processing time {et-st} sec")
        return body, headers
    

class ChamberLogMessageQueueClient(BasicClient):
    async def request(self):
        logging.info(f"{self.log_prefix} get Log")
        headers = {"type": "log_request"}
        return await super().request(body="".encode(), headers=headers)


class MIModeMessageQueueServer(PubSubServer):
    mimode_server: MIModeServer

    def __init__(
        self,
        mimode_server: MIModeServer,
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

        self.mimode_server = mimode_server
        # self.mimode_server.register_execution_finish_callback(self.execution_finished)

    def update_state(self):
        # update the state at each read out
        self.state.update({"latest_executed_commands_uuid": self.mimode_server.get_latest_execution().uuid})

        # update the state at each read out

    # def execution_finished_callback(self, result):
    #     pass

    async def handle_execution_request(self, content, headers):
        try:
            command_future = self.mimode_server.register_commands(content["commands"], content["commands_uuid"])

            result = await command_future
            body = encode_json(result)
            headers = {"type": "execution_result", "success": True}
        except Exception as e:
            logging.error(f"{self.server_name} Error in execution: {e}")

            body = encode_json({"error": str(e)})
            headers = {"type": "execution_result", "success": False}
            raise e

        return body, headers
    
    def handle_list_execution_request(self, content, headers):
        body = encode_json(self.mimode_server.get_all_executions())
        headers = {"type": "list_execution_result", "success": True}

        return body, headers


    async def on_message(self, message: AbstractIncomingMessage):
        package = decode_json( message.body.decode() )
        headers = message.headers

        if headers["type"] == "execution_request":
            return await self.handle_execution_request(package, headers)
        
        elif headers["type"] == "list_execution":
            return await self.handle_list_execution_request(package, headers)
        
        else:
            return "".encode(), {"type": "error", "success": False, "message": f"Unknown message type: {headers['type']}"}

class MIModeMessageQueueClient(PubSubClient):
    async def register_commands(self, commands: str, commands_uuid: str):
        logging.info(f"{self.client_name} request execution of commands to MIMode")
        headers = {"type": "execution_request"}
        return await self.request(body=encode_json({"commands": commands, "commands_uuid": commands_uuid}), headers=headers)
    
    async def list_execution(self):
        logging.info(f"{self.client_name} request execution of commands to MIMode")
        headers = {"type": "list_execution"}
        return await self.request(body=encode_json({"commands": commands, "commands_uuid": commands_uuid}), headers=headers)



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

    async def on_streaming(self):
        if not self.log_queue.empty():
            log, headers = self.log_queue.get()
            body = json.dumps(log).encode()

            return body, headers
        else:
            return None, None


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
