import logging

import uuid
import asyncio
import aio_pika

from aio_pika import Message, connect
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)

from collections.abc import Callable, Awaitable


class LiveFragmentMessageQueueClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    callback_queue : AbstractQueue
    on_response_callback : Awaitable

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, on_response_callback:Awaitable) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None
        self.on_response_callback = on_response_callback

    async def create_queue(self):
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        logging.info(f"Create LiveFragment Queue: {self.queue.name}")

    async def start(self, start_consume_loop=True):
        print("start live")
        await self.create_queue()
        
        if start_consume_loop:
            print("start live loop")

            try:
                self._consumer_tag = await self.queue.consume(self.on_response, no_ack=True)
            except Exception as e:
                print(e)


    async def get(self):
        return await self.queue.get(no_ack=True)

    async def on_response(self, message: AbstractIncomingMessage) -> None:

        #TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"Bad message {message!r}")
            return

        succ_flag = await self.on_response_callback( message )
        if not succ_flag:
            logging.info("Terminate consume")
            await self.queue.cancel(self._consumer_tag, )
            await self.queue.delete()            



class FragmentMessageQueueClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None

    async def create_queue(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        self.futures = {}
        logging.info(f"Create Fragment Queue: {self.callback_queue.name}")


    async def start(self):
        await self.create_queue()
        # the consume here is not blocking
        await self.callback_queue.consume(self.on_response, no_ack=True)
        print("video fragment client start up")
        return self

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"Bad message {message!r}")
            return

        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result( (message.body, message.headers) )

    async def get(self, n: int, is_initial:bool) -> int:
        print(f"get fragment {n} is_initial:{is_initial}")
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self.futures[correlation_id] = future
        if is_initial:
            headers = {
                "type":"initial"
            }
        else:
            headers = {
                "type":"history"
            }

        await self.exchange.publish(
            Message(
                str(n).encode(),
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers
            ),
            routing_key=self.routing_key,
        )
        print(f"get fragment {n} finished")

        return await future

    async def get_initial(self,) -> int:
        print("call get initial")

        initial_fragments = None
        initial_fragment, initial_fragment_header = await self.get(0, is_initial=True) # get the first frame
        print("get first fragment")

        size = initial_fragment_header['size']
        initial_fragments = [None] * size
        initial_fragments[0] = initial_fragment

        other_fragments_result = await asyncio.gather( *[ self.get(i, is_initial=True) for i in range(1, size)] )
        print(f"get rest of the fragments with total length {size}")
        for other_fragment_result in other_fragments_result:
            fragment, headers = other_fragment_result
            initial_fragments[ headers["index"] ] = fragment
        print(f"get rest of the fragments with actual total length {len(initial_fragments)}")

        print("return get initial")

        return initial_fragments
    

class ImageMessageQueueClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None

    async def create_queue(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        self.futures = {}
        logging.info(f"Create ImageMessage Callback queue: {self.callback_queue.name}")


    async def start(self):
        await self.create_queue()
        # the consume here is not blocking
        await self.callback_queue.consume(self.on_response, no_ack=True)
        print("image client start up")
        return self

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"Bad message {message!r}")
            return
        
        logging.info("receive image")
        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result( (message.body, message.headers) )

    async def get(self) -> int:
        logging.info(f"Get live image")
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self.futures[correlation_id] = future
        headers = {
            "type":"image"
        }

        await self.exchange.publish(
            Message(
                ''.encode(),
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers
            ),
            routing_key=self.routing_key,
        )

        return await future
    

class LiveDetectionClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    control_routing_key : str
    queue : AbstractQueue
    on_response_callback : Callable

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, on_response_callback:Callable) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.queue = None
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None
        self.on_response_callback = on_response_callback

    async def create_queue(self):
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        logging.info(f"Create LiveDetection Queue: {self.queue.name}")


    async def start(self, start_consume_loop=True):
        print("start live detection")
        await self.create_queue()

        if start_consume_loop:
            print("start live detection loop")

            try:
                self._consumer_tag = await self.queue.consume(self.on_response, no_ack=True)
            except Exception as e:
                print(e)

    async def start_detection(self):
        await self.exchange.publish(
            message= Message(
                body = "start".encode(),
                headers={}
            ),
            routing_key= self.control_routing_key
        )

    async def stop_detection(self):
        await self.exchange.publish(
            message= Message(
                body = "stop".encode(),
                headers={}
            ),
            routing_key= self.control_routing_key
        )

    async def get(self):
        return await self.queue.get(no_ack=True)

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        print("on live response")

        #TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"Bad message {message!r}")
            return

        succ_flag = await self.on_response_callback( message )
        if not succ_flag:
            logging.info("Terminate consume")
            await self.queue.cancel(self._consumer_tag, )
            await self.queue.delete()


class LogMessageQueueClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.futures = None
        self.queue = None

    async def create_queue(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        self.futures = {}
        logging.info(f"Create Log Callback Queue: {self.callback_queue.name}")


    async def start(self):
        await self.create_queue()
        # the consume here is not blocking
        await self.callback_queue.consume(self.on_response, no_ack=True)
        logging.info("log client start up")
    
        return self

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"Bad message {message!r}")
            return
        
        logging.info(f"receive Log")
        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result( (message.body, message.headers) )

    async def get(self) -> int:
        logging.info(f"Get Log")
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self.futures[correlation_id] = future
        headers = {
            "type":"log"
        }

        await self.exchange.publish(
            Message(
                ''.encode(),
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers
            ),
            routing_key=self.routing_key,
        )

        return await future


class LiveLogMessageQueueClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    control_routing_key : str
    queue : AbstractQueue
    on_response_callback : Callable

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, on_response_callback:Callable) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.queue = None
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None
        self.on_response_callback = on_response_callback

    async def create_queue(self):
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        logging.info(f"Create LiveLog Queue: {self.queue.name}")

    async def start(self, start_consume_loop=True):
        print("start live log")
        await self.create_queue()
        if start_consume_loop:
            print("start live log loop")

            try:
                self._consumer_tag = await self.queue.consume(self.on_response, no_ack=True)
            except Exception as e:
                print(e)

    async def start_operation(self):
        await self.exchange.publish(
            message= Message(
                body = "start".encode(),
                headers={}
            ),
            routing_key= self.control_routing_key
        )

    async def stop_operation(self):
        await self.exchange.publish(
            message= Message(
                body = "stop".encode(),
                headers={}
            ),
            routing_key= self.control_routing_key
        )

    async def get(self):
        return await self.queue.get(no_ack=True)

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        print("on live response")

        #TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"Bad message {message!r}")
            return

        succ_flag = await self.on_response_callback( message )
        if not succ_flag:
            logging.info("Terminate consume")
            await self.queue.cancel(self._consumer_tag, )
            await self.queue.delete()