from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel, AbstractExchange,  AbstractConnection, AbstractIncomingMessage, AbstractQueue,
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
from ..base.message_queue import BasicServer, BasicStreamServer, BasicClient, BasicStreamClient, MessageQueueResponse, BaseControlMixin

from typing import Callable, List, Dict, Any, Awaitable

class ChamberLogMessageQueueServer(BasicServer):
    log_reader : LogReader

    def __init__(
            self, 
            log_reader, 
            channel:AbstractChannel, 
            exchange:AbstractExchange, 
            routing_key:str, 
            control_routing_key:str, 
            server_name:str
        ):
        super().__init__(channel=channel, exchange=exchange, control_routing_key=control_routing_key, routing_key=routing_key, server_name=server_name)

        self.log_reader = log_reader

    async def on_message(self, message:AbstractIncomingMessage):
        # st = time.time()
        log, headers = self.log_reader.get_log() # this get the latest frame from the peek queue
        body = json.dumps(log).encode()
        # et = time.time()
        # logging.info(f"[x] Log processing time {et-st} sec")
        return body, headers

    async def on_status(self, body, headers):
        """
            We temporary get the key from live 
        """
        
        status = await super().on_status(body, headers)

        status.update( {
            "entries" : self.log_reader.entries
        } )
        return status

class ChamberLogMessageQueueClient(BasicClient):
    async def request(self):
        logging.info(f"{self.client_name} get Log")
        headers = { "type":"log" }
        return await super().request( body=''.encode(), headers=headers )


class LiveChamberLogMessageQueueServer(BasicStreamServer):
    log_reader : LogReader
    log_queue : queue.Queue

    def __init__(self, log_reader, log_queue, channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, publish_routing_key:str, server_name:str):
        """
        """
        super().__init__(channel=channel, exchange=exchange, control_routing_key=control_routing_key, publish_routing_key=publish_routing_key, server_name=server_name)
        self.log_reader = log_reader
        self.log_queue = log_queue

    async def on_streaming(self):
        if not self.log_queue.empty():
            log, headers = self.log_queue.get()
            body= json.dumps(log).encode()
            return body, headers
        else:
            return None, None

    async def on_status(self, body, headers):
        """
            We temporary get the key from live 
        """
        status = await super().on_status(body, headers)

        status.update( {
            "entries" : self.log_reader.entries
        } )
        return status


class LiveChamberLogMessageQueueClient(BasicStreamClient):
    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange, routing_key: str, control_routing_key: str, on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]], client_name: str, time_out: float) -> None:
        super().__init__(channel, exchange, routing_key, control_routing_key, on_response_callback, client_name, time_out)

