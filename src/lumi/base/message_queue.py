import logging
import uuid
from collections import namedtuple
from dataclasses import dataclass
import json

import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractQueue,
    AbstractIncomingMessage,
    AbstractQueue,
)

from dataclasses import dataclass

from typing import (
    Callable,
    Tuple,
    List,
    Dict,
    Union,
    Optional,
    ByteString,
    Any,
    Awaitable,
)


# MessageQueueResponse = namedtuple("MessageQueueResponse", ["content", "header"], defaults=[None, None])
class MessageQueueResponse:
    body: bytes
    headers: Dict

    def __init__(self, body: Union[bytes, Dict, str], headers: Dict):
        self.body = self.encode_body(body)
        self.headers = headers

    def encode_body(self, body: Union[bytes, Dict, str]):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        return body


class BaseControlMixin:
    _control_callbacks: Dict[
        str, Callable[[ByteString, Dict], Awaitable[MessageQueueResponse]]
    ] = {}

    @classmethod
    def register_control_callback(cls, name: str):
        def _register_callback(
            callback: Callable[[ByteString, Dict], Awaitable[MessageQueueResponse]]
        ):
            cls._control_callbacks[name] = callback
            return callback

        return _register_callback

    def control_callback(self, name, *args, **kargs):
        return self._control_callbacks[name](self, *args, **kargs)

    async def on_control_request(self, message: AbstractIncomingMessage):
        async with message.process():
            body, headers = message.body, message.headers
            ctrl = headers["type"]
            logging.info(
                f"{self.server_type} <{self.server_name}> on control message '{ctrl}'."
            )

            if ctrl in self._control_callbacks:
                response = await self.control_callback(ctrl, body, headers)

            else:
                logging.info(
                    f"{self.server_type} <{self.server_name}> received an unknown control text '{ctrl}'"
                )

            assert (
                message.reply_to is not None
            ), f"Receive a bad control request without .reply_to field"

            await self.callback_exchange.publish(
                Message(
                    body=response.body,
                    correlation_id=message.correlation_id,
                    headers=response.headers,
                ),
                routing_key=message.reply_to,
            )
            logging.info(
                f"{self.server_type} <{self.server_name}> control response delivered to {message.reply_to}"
            )

            # the example didn't ack back if process() method is used
            # await message.ask()


class BasicClient:
    channel: Optional[AbstractChannel]
    exchange: Optional[AbstractExchange]
    routing_key: str
    control_routing_key: str
    state_routing_key: str
    callback_queue: Optional[AbstractQueue]
    client_name: str
    time_out: float
    on_state_callback: Optional[ Callable[[AbstractIncomingMessage], Awaitable[bool]] ]

    client_type: str = "MQ client"

    def __init__(
        self,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        client_name: str,
        time_out: float,
        on_state_callback: Optional[ Callable[[AbstractIncomingMessage], Awaitable[bool]] ],
    ) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key
        self.client_name = client_name
        self.time_out = time_out
        self.on_state_callback = on_state_callback
        self.reset_queues()

    def reset_queues(self):
        self.callback_queue = None
        self._callback_queue_consume_tag = None

        self.control_callback_queue = None
        self._control_callback_queue_consume_tag = None

        self.state_queue = None
        self._state_consume_tag = None

    async def create_state_queue(self):
        logging.info(f"{self.client_type} <{self.client_name}> creates state queue")
        self.state_queue = await self.channel.declare_queue(exclusive=True)
        await self.state_queue.bind(self.exchange, self.state_routing_key)

    async def create_queues(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)

        self.futures = {}
        logging.info(
            f"{self.client_type} <{self.client_name}> creates callback queue: {self.callback_queue.name}"
        )

        self.control_callback_queue = await self.channel.declare_queue(exclusive=True)

        self.control_futures = {}
        logging.info(
            f"{self.client_type} <{self.client_name}> creates control callback queue: {self.control_callback_queue.name}"
        )

        await self.create_state_queue()

    async def start(self):
        await self.create_queues()
        # the consume here is not blocking
        self._callback_queue_consume_tag = await self.callback_queue.consume(
            self.on_response, no_ack=True
        )

        self._control_callback_queue_consume_tag = (
            await self.control_callback_queue.consume(
                self.on_control_response, no_ack=True
            )
        )

        self._state_consume_tag = await self.state_queue.consume(
            self.on_state_response, no_ack=True
        )

        logging.info(f"{self.client_type} <{self.client_name}> starts up")

        return self

    async def stop(self):
        if (
            self.callback_queue is not None
            and self._callback_queue_consume_tag is not None
        ):
            await self.callback_queue.cancel(self._callback_queue_consume_tag)
            await self.callback_queue.delete()

        if (
            self.control_callback_queue is not None
            and self._control_callback_queue_consume_tag is not None
        ):
            await self.control_callback_queue.cancel(
                self._control_callback_queue_consume_tag
            )
            await self.control_callback_queue.delete()

        self.reset_queues()

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(
                f"{self.client_type} <{self.client_name}> receives bad message {message!r}"
            )
            return

        logging.info(
            f"{self.client_type} <{self.client_name}> receives an item ({message.correlation_id})"
        )
        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result(MessageQueueResponse(message.body, message.headers))

    async def on_control_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(
                f"{self.client_type} <{self.client_name}> receive bad control message {message!r}"
            )
            return

        logging.info(
            f"{self.client_type} <{self.client_name}> receives an control response ({message.correlation_id})"
        )
        future: asyncio.Future = self.control_futures.pop(message.correlation_id)
        future.set_result(MessageQueueResponse(message.body, message.headers))

    async def on_state_response(self, message: AbstractIncomingMessage) -> None:
        logging.info(f"{self.client_type} <{self.client_name}> on state response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(
                f"{self.client_type} <{self.client_name}> received a bad message {message!r}"
            )
            return

        if self.on_state_callback is not None:
            stop_flag = await self.on_state_callback(message)
            if stop_flag:
                logging.info(
                    f"{self.client_type} <{self.client_name}> state callback stop_flag: {stop_flag} raises, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    @property
    def empty_response(self):
        return MessageQueueResponse(None, None)

    async def request(self, body, headers) -> MessageQueueResponse:
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(
            f"{self.client_type} <{self.client_name}> requests an item ({correlation_id})"
        )

        self.futures[correlation_id] = future

        await self.exchange.publish(
            Message(
                body,
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers,
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
            logging.info(
                f"{self.client_type} <{self.client_name}> waits for response with timeout {self.time_out}s"
            )
            return await asyncio.wait_for(future, timeout=self.time_out)

        except asyncio.TimeoutError:
            logging.info(f"{self.client_type} <{self.client_name}> resquests time out")
            return self.empty_response

    async def request_control(self, body, headers):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(
            f"{self.client_type} {self.client_name} requests an control item ({correlation_id})"
        )

        self.control_futures[correlation_id] = future

        await self.exchange.publish(
            Message(
                body,
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.control_callback_queue.name,
                headers=headers,
            ),
            routing_key=self.control_routing_key,
        )

        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info(
                f"{self.client_type} <{self.client_name}> requests control times out"
            )
            return self.empty_response

    async def shutdown_server(self):
        response = await self.request_control(
            body="".encode(), headers={"type": "shutdown"}
        )

    async def config_server(self, config):
        response = await self.request_control(
            body=json.dumps(config).encode(), headers={"type": "config"}
        )

        return response

    async def get_state(self, return_bytes=False):
        response = await self.request_control(
            body="".encode(), headers={"type": "state"}
        )
        if return_bytes:
            return response.body
        else:
            return json.loads(response.body)

class BasicStreamClient:
    channel: Optional[AbstractChannel]
    exchange: Optional[AbstractExchange]
    routing_key: str
    control_routing_key: str
    state_routing_key: str
    queue: Optional[AbstractQueue]
    control_callback_queue: Optional[AbstractQueue]
    on_response_callback: Optional[ Callable[[AbstractIncomingMessage], Awaitable[bool]] ]
    on_state_callback: Optional[ Callable[[AbstractIncomingMessage], Awaitable[bool]] ]
    client_name: str
    time_out: float

    client_type: str = "MQ stream client"

    def __init__(
        self,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_response_callback: Optional[ Callable[[AbstractIncomingMessage], Awaitable[bool]] ],
        on_state_callback: Optional[ Callable[[AbstractIncomingMessage], Awaitable[bool]] ],
        client_name: str,
        time_out: float,
    ) -> None:

        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key
        self.on_response_callback = on_response_callback
        self.on_state_callback = on_state_callback
        self.client_name = client_name
        self.time_out = time_out

        self.reset_queues()

    @property
    def empty_response(self):
        return MessageQueueResponse(None, None)

    def update_reponse_callback(
        self, on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]]
    ):
        if not self.is_main_running():
            self.on_response_callback = on_response_callback
        else:
            raise Exception(
                "Current job is not stopped, run .stop to stop the current job first."
            )

    def is_main_running(self):
        if self.queue is None and self._consumer_tag is None:
            return False
        else:
            return True

    def is_control_running(self):
        if (
            self.control_callback_queue is None
            and self._control_callback_consumer_tag is None
        ):
            return False
        else:
            return True

    def is_state_running(self):
        if self.state_queue is None and self._state_consumer_tag is None:
            return False
        else:
            return True

    def reset_main_queue(self):
        self._consumer_tag = None
        self.queue = None

    def reset_control_queue(self):
        self.control_callback_queue = None
        self._control_callback_consumer_tag = None

    def reset_state_queue(self):
        self._state_consumer_tag = None
        self.state_queue = None

    def reset_queues(self):
        self.reset_control_queue()
        self.reset_main_queue()
        self.reset_state_queue()

    async def create_main_queue(self):
        # this queue is for listening to stream
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        logging.info(
            f"{self.client_type} <{self.client_name}> creates queue: {self.queue.name}"
        )

    async def create_control_queue(self):
        self.control_callback_queue = await self.channel.declare_queue(exclusive=True)
        self.control_futures = {}
        logging.info(
            f"{self.client_type} <{self.client_name}> creates control callback queue: {self.control_callback_queue.name}"
        )

    async def create_state_queue(self):
        logging.info(f"{self.client_type} <{self.client_name}> creates state queue")
        self.state_queue = await self.channel.declare_queue(exclusive=True)
        await self.state_queue.bind(self.exchange, self.state_routing_key)

    # async def create_queues(self):
    #     await self.create_main_queue()
    #     await self.create_control_queue()
    #     await self.create_state_queue()

    async def start_main(self, start_consume_loop=True):
        await self.create_main_queue()
        if start_consume_loop:
            logging.info(
                f"{self.client_type} <{self.client_name}> starts live streaming consume loop"
            )

            try:
                self._consumer_tag = await self.queue.consume(
                    self.on_response, no_ack=True
                )
            except Exception as e:
                print(e)

    async def start_control(self, start_consume_loop=True):
        await self.create_control_queue()
        if start_consume_loop:
            try:
                self._control_callback_consumer_tag = (
                    await self.control_callback_queue.consume(
                        self.on_control_response, no_ack=True
                    )
                )
            except Exception as e:
                print(e)

    async def start_state(self, start_consume_loop=True):
        await self.create_state_queue()
        if start_consume_loop:
            try:
                self._state_consumer_tag = await self.state_queue.consume(
                    self.on_state_response, no_ack=True
                )
            except Exception as e:
                print(e)

    async def start(self, start_consume_loop=True):
        await self.start_main(start_consume_loop=start_consume_loop)
        await self.start_control(start_consume_loop=start_consume_loop)
        await self.start_state(start_consume_loop=start_consume_loop)

        logging.info(f"{self.client_type} <{self.client_name}> starts live streaming")

    async def stop_main(self):
        if self.is_main_running():
            await self.queue.cancel(
                self._consumer_tag,
            )
            await self.queue.delete()
            self.reset_main_queue()

    async def stop_control(self):
        if self.is_control_running():
            await self.control_callback_queue.cancel(
                self._control_callback_consumer_tag,
            )
            await self.control_callback_queue.delete()
            self.reset_control_queue()

    async def stop_state(self):
        if self.is_state_running():
            await self.state_queue.cancel(
                self._state_consumer_tag,
            )
            await self.state_queue.delete()
            self.reset_state_queue()

    async def stop(self):
        logging.info(f"{self.client_type} <{self.client_name}> terminate consume")
        await self.stop_main()
        await self.stop_control()
        await self.stop_state()

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        logging.debug(f"{self.client_type} <{self.client_name}> on live response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(
                f"{self.client_type} <{self.client_name}> received a bad message {message!r}"
            )
            return

        if self.on_response_callback is not None:
            succ_flag = await self.on_response_callback(message)
            if not succ_flag:
                logging.info(
                    f"{self.client_type} <{self.client_name}> state callback succ_flag not True, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    async def on_control_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(
                f"{self.client_type} <{self.client_name}> receive bad control message {message!r}"
            )
            return

        logging.info(
            f"{self.client_type} <{self.client_name}> receives an control response ({message.correlation_id})"
        )
        future: asyncio.Future = self.control_futures.pop(message.correlation_id)
        future.set_result(MessageQueueResponse(message.body, message.headers))

    async def on_state_response(self, message: AbstractIncomingMessage) -> None:
        logging.info(f"{self.client_type} <{self.client_name}> on state response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(
                f"{self.client_type} <{self.client_name}> received a bad message {message!r}"
            )
            return

        if self.on_state_callback is not None:
            stop_flag = await self.on_state_callback(message)
            if stop_flag:
                logging.info(
                    f"{self.client_type} <{self.client_name}> state callback stop_flag: {stop_flag}, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    async def request_control(self, body, headers):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(
            f"{self.client_type} <{self.client_name}> request an control item ({correlation_id})"
        )

        self.control_futures[correlation_id] = future

        await self.exchange.publish(
            Message(
                body,
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.control_callback_queue.name,
                headers=headers,
            ),
            routing_key=self.control_routing_key,
        )

        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info(
                f"{self.client_type} <{self.client_name}> requests control times out"
            )
            return self.empty_response

    async def start_streaming(self):
        response = await self.request_control(
            body="".encode(), headers={"type": "start"}
        )

    async def stop_streaming(self):
        response = await self.request_control(
            body="".encode(), headers={"type": "stop"}
        )

    async def shutdown_server(self):
        response = await self.request_control(
            body="".encode(), headers={"type": "shutdown"}
        )

    async def config_server(self, config):
        response = await self.request_control(
            body=json.dumps(config).encode(), headers={"type": "config"}
        )

    async def get_state(self, return_bytes=False):
        response = await self.request_control(
            body="".encode(), headers={"type": "state"}
        )
        if return_bytes:
            return response.body
        else:
            return json.loads(response.body)
    
    async def get(self):
        return await self.queue.get(no_ack=True)


class ServerState(dict):
    def __init__(
        self,
        initial_state: Dict,
        on_state_change_callback: Optional[
            Callable[["ServerState"], Awaitable[None]]
        ] = None,
    ):
        super().__init__(initial_state)
        self.on_state_change_callback = on_state_change_callback

    def __setitem__(self, key, value):
        if key not in self or self[key] != value:
            super().__setitem__(key, value)
            self._notify_state_change()

    def update(self, *args, **kwargs):
        original_state = self.copy()
        super().update(*args, **kwargs)
        if self != original_state:
            self._notify_state_change()

    def _notify_state_change(self):
        if self.on_state_change_callback:
            asyncio.create_task(self.on_state_change_callback(self) )


class StateCallbackMixin:
    exchange: AbstractExchange
    state_routing_key: str
    server_name: str
    server_type: str

    async def on_state_callback(self, state: Dict):
        response = MessageQueueResponse(body=state, headers={"type": "state"})
        logging.info(f"{self.server_type} <{self.server_name}> state prepared")

        await self.exchange.publish(
            Message(
                body=response.body,
                headers=response.headers,
            ),
            routing_key=self.state_routing_key,
        )
        logging.info(f"{self.server_type} <{self.server_name}> send state content")


class BasicStreamServer(BaseControlMixin, StateCallbackMixin):

    channel: AbstractChannel
    exchange: AbstractExchange
    control_routing_key: str
    publish_routing_key: str
    state_routing_key: str

    control_queue: AbstractQueue
    server_name: str

    start_flag: bool
    state: ServerState

    server_type: str = "MQ stream server"

    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        publish_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        """
        No need input routing key
        """
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange

        self.control_routing_key = control_routing_key
        self.publish_routing_key = publish_routing_key
        self.state_routing_key = state_routing_key

        self.server_name = server_name

        self.start_flag = False
        self.state = ServerState(
            {
                "is_running": False,
                "is_streaming": False,
            },
            self.on_state_callback,
        )

        self.reset_queues()

    def update_state(self):
        """ 
        a customizable function that use to update other part of the state the during operation.
        Not implemented is ok, the base function will do nothing.
        """
        pass

    @property
    def succ_ctrl_response(self):
        return MessageQueueResponse("".encode(), {"succ": True})

    def set_start_flag(self, state):
        self.start_flag = state

    def reset_queues(self):
        self.control_queue = None
        self._control_consume_tag = None

    async def create_control_queue(self):
        logging.info(f"{self.server_type} <{self.server_name}> creates control queue")
        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, self.control_routing_key)

    async def create_queues(self):
        await self.create_control_queue()

    async def on_streaming(self):
        raise NotImplementedError()

    async def publish(self):
        while True:
            # update the state at each iteration of the publish loop
            self.update_state()

            if self.start_flag:
                self.state["is_streaming"] = True

                body, headers = await self.on_streaming()
                if body is not None:
                    logging.debug(
                        f"{self.server_type} <{self.server_name}> content prepared"
                    )

                    await self.exchange.publish(
                        Message(
                            body=body,
                            headers=headers,
                        ),
                        routing_key=self.publish_routing_key,
                    )
                    logging.debug(
                        f"{self.server_type} <{self.server_name}> send content"
                    )
                else:
                    logging.debug(
                        f"{self.server_type} <{self.server_name}> fail to prepare content"
                    )
                    await asyncio.sleep(0.01)
            else:
                self.state["is_streaming"] = False

                logging.debug(
                    f"{self.server_type} <{self.server_name}> stream loop is paused"
                )
                await asyncio.sleep(0.1)

        # the example didn't ack back if process() method is used
        # await message.ask()

    @BaseControlMixin.register_control_callback("start")
    async def start_streaming(self, body, headers):
        self.set_start_flag(True)
        return self.succ_ctrl_response

    @BaseControlMixin.register_control_callback("stop")
    async def end_streaming(self, body, headers):
        self.set_start_flag(False)
        return self.succ_ctrl_response

    async def on_config(self, body, headers):
        raise NotImplementedError

    @BaseControlMixin.register_control_callback("config")
    async def config_server(self, body, headers):
        output = self.on_config(self, body, headers)
        self.update_state()
        return MessageQueueResponse(output, {"type": "config"})

    async def on_state(self, body, headers):
        return dict(self.state)

    @BaseControlMixin.register_control_callback("state")
    async def server_state(self, body, headers):
        # force update the state when query is conducted
        self.update_state()

        state = await self.on_state(body, headers)
        return MessageQueueResponse(state, {"type": "state"})

    async def start(self):
        await self.create_queues()
        try:
            self._control_consume_tag = await self.control_queue.consume(
                callback=self.on_control_request, no_ack=False
            )
            self._publish_task = asyncio.create_task(self.publish())

            # is_running means the server is running
            self.state["is_running"] = True
            self.update_state()
        except Exception as e:
            print(e)
        logging.info(f"{self.server_type} <{self.server_name}> starts up")

    @BaseControlMixin.register_control_callback("cancel")
    async def cancel(self):
        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)

        self.reset_queues()
        self.state["is_running"] = False
        return self.succ_ctrl_response()


class BasicServer(BaseControlMixin, StateCallbackMixin):
    channel: AbstractChannel
    exchange: AbstractExchange
    callback_exchange: AbstractExchange
    control_routing_key: str
    routing_key: str
    state_routing_key: str
    queue: AbstractQueue
    server_name: str

    state: ServerState
    server_type: str = "MQ server"

    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key

        self.server_name = server_name
        self.reset_queues()

        # self.reset_control_callbacks()
        # self.register_basic_control_callback()

        self.state = ServerState(
            {
                "is_running": False,
            },
            self.on_state_callback,
        )

    def update_state(self):
        """ 
        update the state at each call, use to update the during operation.
        Not implemented is ok, the base function will do nothing.
        """
        pass

    def reset_queues(self):
        self.queue = None
        self._consume_tag = None
        self.control_queue = None
        self._control_consume_tag = None

    async def create_control_queue(self):
        logging.info(f"{self.server_type} <{self.server_name}> creates control queue")
        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, self.control_routing_key)

    async def create_main_queue(self):
        logging.info(f"{self.server_type} <{self.server_name}> creates main queue")
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)

    async def create_queues(self):
        await self.create_main_queue()
        await self.create_control_queue()

    async def on_message(self, message: AbstractIncomingMessage) -> Tuple[bytes, Dict]:
        """
        Overwrite this base method with the implementation of the server functionality.
        Outputs:
            body : bytes
            headers : Dict
        """
        raise NotImplementedError()

    async def on_request(self, message: AbstractIncomingMessage):
        logging.info(f"{self.server_type} <{self.server_name}> on request")
        async with message.process():
            assert (
                message.reply_to is not None
            ), f"Receive a bad request without .reply_to field"

            body, headers = await self.on_message(message)
            logging.info(f"{self.server_type} <{self.server_name}> content is prepared")

            await self.callback_exchange.publish(
                Message(
                    body=body,
                    correlation_id=message.correlation_id,
                    headers=headers,
                ),
                routing_key=message.reply_to,
            )
            logging.info(
                f"{self.server_type} <{self.server_name}> content delivered to {message.reply_to}"
            )

            # update the state at each call
            self.update_state()

            # the example didn't ack back if process() method is used
            # await message.ask()

    async def on_config(self, body, headers):
        raise NotImplementedError

    @BaseControlMixin.register_control_callback("config")
    async def config_server(self, body, headers):
        output = self.on_config(self, body, headers)
        return MessageQueueResponse(output, {"type": "config"})

    async def on_state(self, body, headers) -> Dict:
        return dict(self.state)

    @BaseControlMixin.register_control_callback("state")
    async def server_state(self, body, headers):
        # force update the state when query is conducted
        self.update_state() 

        state = await self.on_state(body, headers)
        return MessageQueueResponse(state, {"type": "state"})

    async def start(self):
        await self.create_queues()

        try:
            self._control_consume_tag = await self.control_queue.consume(
                callback=self.on_control_request, no_ack=False
            )
            self._consume_tag = await self.queue.consume(self.on_request, no_ack=False)
            self.state["is_running"] = True
            self.update_state()
        except Exception as e:
            print(e)

        logging.info(f"{self.server_type} <{self.server_name}> starts up")

    @BaseControlMixin.register_control_callback("cancel")
    async def cancel(self):
        if self.queue is not None and self._consume_tag is not None:
            await self.queue.cancel(self._consume_tag)
            await self.queue.delete()

        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)
            await self.control_queue.delete()

        self.reset_queues()
        self.state["is_running"] = False
