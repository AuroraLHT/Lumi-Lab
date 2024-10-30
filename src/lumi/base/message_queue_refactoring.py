import logging
import uuid
from collections import namedtuple
from dataclasses import dataclass
import json

import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)

from dataclasses import dataclass

from typing import Callable, Tuple, List, Dict, Union, Optional, ByteString, Any, Awaitable

@dataclass
class MessageQueueResponse:
    body : Union[bytes, Dict, str]
    headers : Dict

    def __post_init__(self):
        if isinstance(self.body, dict):
            self.body = json.dumps(self.body).encode()
        elif isinstance(self.body, str):
            self.body = self.body.encode()


class BasicQueue:
    """
    Basic Queue for message queue. This queue would listen to the message from the exchange.
    Process the message and then pass the message to the on_response_callback. This is a base class not
    no callback is defined.
    """
    channel : AbstractChannel
    no_bind : bool
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue
    time_out : float
    _consumer_tag : str
    logger : logging.Logger

    def __init__(
            self, 
            channel:AbstractChannel, 
            no_bind:bool,
            exchange:AbstractExchange, 
            routing_key:str,
            time_out:float,
            logger:logging.Logger
        ) -> None:

        self.channel = channel
        self.no_bind = no_bind
        self.exchange = exchange
        self.routing_key = routing_key
        self.time_out = time_out
        self.logger = logger
        self.reset_queue()

    async def create_queue(self):
        # this queue is for listening to stream
        self.queue = await self.channel.declare_queue(exclusive=True)

        if not self.no_bind :
            await self.queue.bind(self.exchange, routing_key=self.routing_key)

    async def start(self, start_consume_loop=True):
        await self.create_queue()
        if start_consume_loop:
            try:
                self._consumer_tag = await self.queue.consume(self.on_response, no_ack=True)
            except Exception as e:
                print(e)

    def is_running(self):
        if self.queue is None and self._consumer_tag is None:
            return False
        else:
            return True

    def reset_queue(self):
        self._consumer_tag = None
        self.queue = None

    async def stop(self):
        if self.is_running():
            await self.queue.cancel(self._consumer_tag, )
            await self.queue.delete()
            self.reset_queue()

    async def on_message(self, message: AbstractIncomingMessage) -> Awaitable[bool]:
        raise NotImplementedError

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        self.logger.debug(f"on live response")

        #TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"received a bad message {message!r}")
            return

        if self.on_message is not None:
            stop_flag = await self.on_message( message )
            if stop_flag:
                logging.info(f"response callback returns True, stop the streaming process")
                await self.stop()
        else:
            pass

class BasicCallbackQueue(BasicQueue):
    """
    This is a basic queue for callback.
    """
    def __init__(
            self, 
            channel:AbstractChannel, 
            no_bind:bool,
            exchange:AbstractExchange, 
            routing_key:str,
            time_out:float,
            logger:logging.Logger,
            on_message_callback : Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ) -> None:
        super().__init__(channel, no_bind, exchange, routing_key, time_out, logger)
        self.on_message_callback = on_message_callback

    async def on_message(self, message : AbstractIncomingMessage):
        return await self.on_message_callback(message)
    
class ControlMixin:
    """
    This mixin would register callback of a server or client. It would be passed to the control queue when it is mounted
    """
    _control_callbacks : Dict[ str, Callable[ [ByteString, Dict], Awaitable[MessageQueueResponse] ] ] = {}
    @classmethod
    def register_control_callback(cls, name:str):
        def _register_callback(callback:Callable[ [ByteString, Dict], Awaitable[MessageQueueResponse] ]):
            cls._control_callbacks[name] = callback
            return callback
        return _register_callback

class ControlServerQueue(BasicQueue):
    """
    Basic Queue for message queue. This queue would listen to the message from the exchange.
    Process the message and then pass the message to the on_response_callback.
    """
    control_callbacks : Dict[ str, Callable[ [ByteString, Dict], Awaitable[MessageQueueResponse] ] ] = {}

    def __init__(
            self, 
            channel:AbstractChannel, 
            no_bind:bool,
            exchange:AbstractExchange, 
            routing_key:str,
            time_out:float,
            logger:logging.Logger,

            callback_exchange:AbstractExchange, 
            control_callbacks : Dict[ str, Callable[ [ByteString, Dict], Awaitable[MessageQueueResponse] ] ]
        ) -> None:
        super().__init__(channel, no_bind, exchange, routing_key, time_out, logger)
        self.callback_exchange = callback_exchange
        self.control_callbacks = control_callbacks
    
    def control_callback(self, name, *args, **kargs):
        return self.control_callbacks[name](self, *args, **kargs)
    
    async def on_message(self, message : AbstractIncomingMessage):
        async with message.process():
            body, headers = message.body, message.headers
            ctrl = headers["type"]
            self.logger.info(f"on control message '{ctrl}'.")

            if ctrl in self.control_callbacks:
                response = await self.control_callback(ctrl, body, headers)

            else:
                self.logger.info(f"received an unknown control text '{ctrl}'")

            assert message.reply_to is not None, f"Receive a bad control request without .reply_to field"

            await self.callback_exchange.publish(
                Message(
                    body = response.body,
                    correlation_id = message.correlation_id,
                    headers = response.headers,
                ),
                routing_key=message.reply_to
            )
            self.logger.info(f"control response delivered to {message.reply_to}")

            # the example didn't ack back if process() method is used
            # await message.ask()
        return True

class ContentStreamer:
    pass

class StateStreamer:
    """
    This queue is designed for state sync between server and client.
    """
    control_callbacks : Dict[ str, Callable[ [ByteString, Dict], Awaitable[MessageQueueResponse] ] ] = {}

    def __init__(
            self, 
            channel:AbstractChannel, 
            exchange:AbstractExchange, 
            routing_key:str,
            logger:logging.Logger,

        ) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.logger = logger
    
