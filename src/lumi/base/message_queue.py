import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)
import logging
import uuid

from collections import namedtuple
from typing import Callable, List, Dict, Union, Optional

import json

MessageQueueResponse = namedtuple("MessageQueueResponse", ["content", "header"], defaults=[None, None])

class BaseControlMixin:
    control_callbacks : Dict

    def reset_control_callbacks(self):
        self.control_callbacks = Dict()

    def register_callback(self, name, callback):
        self.control_callbacks[name] = callback

    async def on_control_message(self, message : AbstractIncomingMessage):
        async with message.process():
            body, headers = message.body, message.headers
            ctrl = headers["type"]
            logging.info(f"{self.server_name} on control message '{ctrl}'.")

            if ctrl in self.control_callbacks:
                self.control_callbacks[ctrl](body, headers)            
            else:
                logging.info(f"{self.server_name} Unknown ctrl text '{ctrl}'")


class BasicClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    callback_queue : AbstractQueue
    client_name : str
    time_out : float

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, client_name:str, time_out:float) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.client_name = client_name
        self.time_out = time_out
        self.reset_queues()

    def reset_queues(self):
        self.callback_queue = None
        self._callback_queue_consume_tag = None

    async def create_queues(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        self.futures = {}
        logging.info(f"Create {self.client_name} Callback queue: {self.callback_queue.name}")


    async def start(self):
        await self.create_queues()
        # the consume here is not blocking
        self._callback_queue_consume_tag = await self.callback_queue.consume(self.on_response, no_ack=True)
        logging.info(f"{self.client_name} start up")
        return self

    async def stop(self):
        if self.callback_queue is not None and self._callback_queue_consume_tag is not None:
            await self.callback_queue.cancel(self._callback_queue_consume_tag)
            await self.callback_queue.delete()
        self.reset_queues()


    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"{self.client_name} receive bad message {message!r}")
            return
        
        logging.info(f"{self.client_name} receive an item ({message.correlation_id})")
        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result( (message.body, message.headers) )

    @property
    def empty_response(self):
        return MessageQueueResponse(None, None)

    async def get(self, message, headers) -> int:
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(f"{self.client_name} request an item ({correlation_id})")

        self.futures[correlation_id] = future

        await self.exchange.publish(
            Message(
                message,
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers
            ),
            routing_key=self.routing_key,
        )

        # this code block works for python 3.12
        # try:
        #     async with asyncio.timeout():
        #         return await future
        # except TimeoutError:
        #     logging.info("fail to acquire response from the pascal node")

        # https://docs.python.org/3/library/asyncio-task.html#timeouts
        # wait_for is on both 3.10 and 3.12
        # timeout unit is in second
        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info("fail to acquire response from the pascal node")
            return self.empty_response
    

class BasicStreamClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    control_routing_key : str
    queue : AbstractQueue
    on_response_callback : Callable
    client_name : str
    time_out : float


    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, on_response_callback:Callable, client_name:str, time_out:float) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.on_response_callback = on_response_callback
        self.client_name = client_name
        self.time_out = time_out

        self.reset_queues()

    def reset_queues(self):
        self._consumer_tag = None
        self.queue = None

    async def create_queues(self):
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        logging.info(f"{self.client_name} create Queue: {self.queue.name}")

    async def start(self, start_consume_loop=True):
        logging.info("{self.client_name} start live streaming")
        await self.create_queues()

        if start_consume_loop:
            logging.info("{self.client_name} start live streaming consume loop")

            try:
                self._consumer_tag = await self.queue.consume(self.on_response, no_ack=True)
            except Exception as e:
                print(e)

    async def stop(self):
        logging.info(f"{self.client_name} terminate consume")
        if self.queue is not None and self._consumer_tag is not None:
            await self.queue.cancel(self._consumer_tag, )
            await self.queue.delete()

            self.reset_queues()
        
    async def start_streaming(self):
        await self.exchange.publish(
            message= Message(
                body = "".encode(),
                headers = {"type":"start"}
            ),
            routing_key= self.control_routing_key
        )

    async def stop_streaming(self):
        await self.exchange.publish(
            message= Message(
                body = "".encode(),
                headers={{"type":"stop"}}
            ),
            routing_key= self.control_routing_key
        )

    async def shutdown_streaming(self):
        await self.exchange.publish(
            message= Message(
                body = "".encode(),
                headers={{"type":"shutdown"}}
            ),
            routing_key= self.control_routing_key
        )

    async def config_streaming(self, config):
        await self.exchange.publish(
            message= Message(
                body = json.dumps(config).encode(),
                headers = {"type":"config"}
            ),
            routing_key= self.control_routing_key
        )

    async def get(self):
        return await self.queue.get(no_ack=True)
    
    async def on_response(self, message: AbstractIncomingMessage) -> None:
        logging.debug(f"{self.client_name} on live response")

        #TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"{self.client_name} received a bad message {message!r}")
            return

        succ_flag = await self.on_response_callback( message )
        if not succ_flag:
            self.stop()


class BasicStreamServer(BaseControlMixin):
    
    channel : AbstractChannel
    exchange : AbstractExchange
    control_routing_key:str
    publish_routing_key:str
    
    control_queue : AbstractQueue
    server_name : str

    start_flag : bool

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, publish_routing_key:str, server_name:str):
        """
            No need input routing key
        """
        super().__init__()
        self.channel = channel
        self.exchange = exchange

        self.control_routing_key = control_routing_key
        self.publish_routing_key = publish_routing_key

        self.server_name = server_name

        self.start_flag = False
        self.reset_queues()
        self.register_basic_control_callback()

    def set_start_flag(self, state):
        self.start_flag = state

    def register_basic_control_callback(self):
        self.register_callback("start", callback=( lambda body, headers: self.set_start_flag(True) ) )
        self.register_callback("stop", callback=( lambda body, headers: self.set_start_flag(False) ) )
        self.register_callback("cancel", callback=( lambda body, headers: self.cancel() ) )
        self.register_callback("config", callback=( lambda body, headers: self.config_server(json.loads(body)) ) )

    def reset_queues(self):
        self.control_queue = None
        self._control_consume_tag = None

    async def create_queues(self):
        logging.info(f"{self.server_name} create temporary queue")
        # for control
        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, self.control_routing_key)

    async def on_message():
        raise NotImplementedError()

    async def publish(self):
        while True:
            
            if self.start_flag:
                body, headers = await self.on_message()
                if body is not None:
                    logging.debug(f"{self.server_name} content prepared")

                    await self.exchange.publish(
                        Message(
                            body = body,
                            headers = headers,
                        ),
                        routing_key=self.publish_routing_key
                    )
                    logging.debug(f"{self.server_name} send content")
                else:
                    logging.debug(f"{self.server_name} fail to prepare content")
                    asyncio.sleep(0.01)
            else:
                logging.debug(f"{self.server_name} stream loop is paused")
                asyncio.sleep(0.1)

        # the example didn't ack back if process() method is used
        # await message.ask()

    def config_server(self, config):
        pass

    async def start(self):
        await self.create_queues()
        logging.info(f"{self.server_name} start server")
        try:
            # self._consume_tag = await self.queue.consume(callback=self.on_message ,no_ack=True)
            self._control_consume_tag = await self.control_queue.consume(callback=self.on_control_message ,no_ack=False)
            self._publish_task = await asyncio.create_task(self.publish())
        except Exception as e:
            print(e)

    async def cancel(self):
        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)

        self.reset_queues()


class BasicServer(BaseControlMixin):
    channel : AbstractChannel
    exchange : AbstractExchange
    control_routing_key : str
    routing_key : str
    queue : AbstractQueue
    server_name : str

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, routing_key:str, server_name:str):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key

        self.server_name = server_name
        self.reset_queues()
        self.register_basic_control_callback()

    def reset_queues(self):
        self.queue = None
        self._consume_tag = None
        self.control_queue = None
        self._control_consume_tag = None

    def register_basic_control_callback(self):
        self.register_callback("cancel", callback=( lambda body, headers: self.cancel() ) )
        self.register_callback("config", callback=( lambda body, headers: self.config_server(json.loads(body)) ) )


    async def create_queues(self):
        logging.info(f"{self.server_name} create temporary queue")

        # for hosting
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        # for control
        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, routing_key=self.control_routing_key)

    async def on_message(self, message):
        raise NotImplementedError()

    async def on_message(self, message: AbstractIncomingMessage):
        logging.info(f"{self.server_name} on request")
        async with message.process():
            body, headers = await self.on_message(message)
            logging.info(f"{self.server_name} content prepared")

            await self.callback_exchange.publish(
                Message(
                    body = body,
                    correlation_id = message.correlation_id,
                    headers = headers,
                ),
                routing_key=message.reply_to
            )
            logging.info(f"{self.server_name} content delivered")

            # the example didn't ack back if process() method is used
            # await message.ask()

    def config_server(self, config):
        pass


    async def start(self):
        await self.create_queues()
        logging.info(f"{self.server_name} starts")

        try:
            self._control_consume_tag = await self.control_queue.consume(callback=self.on_control_message, no_ack=False)
            self._consume_tag = await self.queue.consume(self.on_message, no_ack=False)
        except Exception as e:
            print(e)

    async def cancel(self):
        if self.queue is not None and self._consume_tag is not None:
            await self.queue.cancel(self._consume_tag)
            await self.queue.delete()

        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)
            await self.control_queue.delete()

        self.reset_queues()