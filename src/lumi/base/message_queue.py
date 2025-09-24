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

from .models import (
    BaseControlRequestMessageHeader,
    BaseControlResponseMessageHeader,
    BaseMessageHeader,
    BaseRequestMessageHeader,
    BaseResponseMessageHeader,
    BaseStateMessageHeader,
    BaseMessageQueueMessage,
    BaseStreamMessageHeader,
    BaseUpdateMessageHeader,
    ControlRequestMessageQueueMessage,
    ControlResponseMessageQueueMessage,
    RequestMessageQueueMessage,
    ResponseMessageQueueMessage,
    StateMessageQueueMessage,
    StreamMessageQueueMessage,
    UpdateMessageQueueMessage,
)

from ..utils.error import get_error_info
from ..utils.common import decode_json, get_current_timestamp

"""
TODO: current the queue is deleleted without checking if the queue is empty.
Need to implement the mechanism to empty the queue before deleting it.
purge kinda work but the doc said there is still a chance that the message would be delivered after the purge.
"""

class BaseMessageHeaderMixin:
    log_prefix: str

    def create_base_headers(
        self,
        message_type: str,
        message_source: str = None,
        message_timestamp: str = None,
    ):
        return {
            "message_source": message_source or self.log_prefix,
            "message_timestamp": message_timestamp or get_current_timestamp(),
            "message_type": message_type,
        }


class BaseControlMessageMixin(BaseMessageHeaderMixin):
    def create_control_request_message(
        self, body: bytes | str | dict, headers: dict, control_request_type: str
    ) -> ControlRequestMessageQueueMessage:
        control_request_headers: BaseControlRequestMessageHeader = {
            "control_request_type": control_request_type,
            **self.create_base_headers(
                message_type=ControlRequestMessageQueueMessage.message_type,
            ),
        }
        return ControlRequestMessageQueueMessage(
            body,
            headers={
                **headers,
                **control_request_headers,
            },
        )

    def create_control_response_message(
        self,
        body: bytes | str | dict,
        headers: dict,
        control_request_type: str,
        control_response_type: str,
        succ: bool = True,
        error_type: str = "",
        error_message: str = "",
    ) -> ControlResponseMessageQueueMessage:
        control_response_headers: BaseControlResponseMessageHeader = {
            "control_request_type": control_request_type,
            "control_response_type": control_response_type,
            "succ": succ,
            "error_type": error_type,
            "error_message": error_message,
            **self.create_base_headers(
                message_type=ControlResponseMessageQueueMessage.message_type,
            ),
        }

        return ControlResponseMessageQueueMessage(
            body,
            headers={
                **headers,
                **control_response_headers,
            },
        )


class BaseRequestMessageMixin(BaseMessageHeaderMixin):
    def create_update_message(
        self, body: bytes | str | dict, headers: dict, update_type: str
    ):
        update_headers: BaseUpdateMessageHeader = {
            "update_type": update_type,
            **self.create_base_headers(
                message_type=UpdateMessageQueueMessage.message_type,
            ),
        }
        return UpdateMessageQueueMessage(
            body,
            {
                **headers,
                **update_headers,
            },
        )

    def create_request_message(
        self, body: bytes | str | dict, headers: dict, request_type: str
    ):
        request_headers: BaseRequestMessageHeader = {
            "request_type": request_type,
            **self.create_base_headers(
                message_type=RequestMessageQueueMessage.message_type,
            ),
        }
        return RequestMessageQueueMessage(
            body,
            headers={
                **headers,
                **request_headers,
            },
        )

    def create_response_message(
        self,
        body: bytes | str | dict,
        headers: dict,
        request_type: str,
        response_type: str,
        succ: bool = True,
        error_type: str = "",
        error_message: str = "",
    ):
        request_response_headers: BaseResponseMessageHeader = {
            "request_type": request_type,
            "response_type": response_type,
            "succ": succ,
            "error_type": error_type,
            "error_message": error_message,
            **self.create_base_headers(
                message_type=ResponseMessageQueueMessage.message_type,
            ),
        }
        return ResponseMessageQueueMessage(
            body,
            headers={
                **headers,
                **request_response_headers,
            },
        )


class BaseStreamMessageMixin(BaseMessageHeaderMixin):
    def create_stream_message(
        self, body: bytes | str | dict, headers: dict, stream_type: str
    ):
        stream_headers: BaseStreamMessageHeader = {
            "stream_type": stream_type,
            **self.create_base_headers(
                message_type=StreamMessageQueueMessage.message_type,
            ),
        }
        return StreamMessageQueueMessage(
            body,
            headers={
                **headers,
                **stream_headers,
            },
        )


class BaseStateMessageMixin(BaseMessageHeaderMixin):
    def create_state_message(
        self, body: bytes | str | dict, headers: dict, state_type: str
    ):
        state_headers: BaseStateMessageHeader = {
            "state_type": state_type,
            **self.create_base_headers(
                message_type=StateMessageQueueMessage.message_type,
            ),
        }
        return StateMessageQueueMessage(
            body,
            headers={
                **headers,
                **state_headers,
            },
        )


class BaseControlMixin:
    callback_exchange: AbstractExchange
    log_prefix: str
    _control_callbacks: Dict[
        str, Callable[[ByteString, Dict], Awaitable[BaseMessageQueueMessage]]
    ] = {}

    @classmethod
    def register_control_callback(cls, name: str):
        def _register_callback(
            callback: Callable[[ByteString, Dict], Awaitable[BaseMessageQueueMessage]]
        ):
            cls._control_callbacks[name] = callback
            return callback

        return _register_callback

    async def control_callback(
        self, name, body: bytes, headers: Dict
    ) -> ControlResponseMessageQueueMessage:
        return await self._control_callbacks[name](self, body, headers)

    async def on_control_request(self, message: AbstractIncomingMessage):
        async with message.process():

            if "control_request_type" not in message.headers:
                logging.error(
                    f"{self.log_prefix} received a bad control message without control_request_type in its headers:{message.headers}"
                )
                return

            else:
                body = message.body
                headers: BaseControlRequestMessageHeader = message.headers

            ctrl = headers["control_request_type"]
            logging.info(f"{self.log_prefix} on control message '{ctrl}'.")

            if ctrl in self._control_callbacks:
                response = await self.control_callback(ctrl, body, headers)
                try:
                    await self.callback_exchange.publish(
                        Message(
                            body=response.body,
                            correlation_id=message.correlation_id,
                            headers=response.headers,
                        ),
                        routing_key=message.reply_to,
                    )
                    logging.info(
                        f"{self.log_prefix} control response delivered to {message.reply_to}"
                    )
                except Exception as e:
                    logging.error(
                        f"{self.log_prefix} fail to deliver control response: {e}"
                    )
            else:
                logging.info(
                    f"{self.log_prefix} received an unknown control text '{ctrl}'"
                )

            assert (
                message.reply_to is not None
            ), f"Receive a bad control request without .reply_to field"

            # the example didn't ack back if process() method is used
            # await message.ask()


"""
TODO:
    - refactor the BasicClient and BasicStreamClient to BaseClient 
    - Both BasicClient and BasicStreamClient should inherit from BaseClient
    - refactor PubSubClient like BasicClient and BasicStreamClient

    The goal is to group the client and server into three types:
    Publish/Subscribe, Request/Response, and Streaming

    The base client and server should only have state and control queue
"""


class PubSubClient(BaseControlMessageMixin, BaseRequestMessageMixin):
    channel: Optional[AbstractChannel]
    exchange: Optional[AbstractExchange]
    request_routing_key: str
    response_routing_key: str
    control_routing_key: str
    update_routing_key: str
    state_routing_key: str
    response_queue: Optional[AbstractQueue]
    client_name: str
    time_out: float
    response_futures: Dict[str, asyncio.Future]

    on_state_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]
    on_update_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]

    client_type: str = "MQ PubSub client"

    def __init__(
        self,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        request_routing_key: str,
        response_routing_key: str,
        control_routing_key: str,
        update_routing_key: str,
        state_routing_key: str,
        client_name: str,
        time_out: float,
        on_state_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        on_update_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
    ) -> None:
        self.channel = channel
        self.exchange = exchange

        self.request_routing_key = request_routing_key
        self.response_routing_key = response_routing_key
        self.update_routing_key = update_routing_key

        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key
        self.client_name = client_name
        self.time_out = time_out
        self.on_state_callback = on_state_callback
        self.on_update_callback = on_update_callback
        self._reset_queues()

    @classmethod
    def from_config(
        cls,
        config: dict,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        time_out: float,
        on_state_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        on_update_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        name_suffix: str = "",
    ):
        """
        Create a PubSubClient from a config dictionary, the config dictionary should contain the following fields:
        - request_key: the routing key to request from
        - response_key: the routing key to response to
        - ctrl_key: the routing key to control the client
        - update_key: the routing key to update the client
        - state_key: the routing key to state the client
        - name: the name of the client
        """
        return cls(
            channel=channel,
            exchange=exchange,
            request_routing_key=config["request_key"],
            response_routing_key=config["response_key"],
            control_routing_key=config["ctrl_key"],
            update_routing_key=config["update_key"],
            state_routing_key=config["state_key"],
            client_name=config["name"] + name_suffix,
            time_out=time_out,
            on_state_callback=on_state_callback,
            on_update_callback=on_update_callback,
        )

    def update_on_update_callback(
        self, on_update_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]]
    ):
        """
        Update the on response callback function. No check for background process.

        Args:
            on_update_callback (Callable[[AbstractIncomingMessage], Awaitable[bool]]):
                The callback function for handling responses, return True to stop the streaming.

        Raises:
            Exception: If the current job is not stopped, raise an exception.
        """
        self.on_update_callback = on_update_callback

    def _reset_response_queues(self):
        self.response_queue = None
        self._response_queue_consume_tag = None
        self.response_futures = {}

    def _reset_control_queues(self):
        self.control_callback_queue = None
        self._control_callback_queue_consume_tag = None
        self.control_futures = {}

    def _reset_update_queues(self):
        self.update_queue = None
        self._update_queue_consume_tag = None

    def _reset_state_queues(self):
        self.state_queue = None
        self._state_consume_tag = None

    def _reset_queues(self):
        self._reset_response_queues()
        self._reset_control_queues()
        self._reset_state_queues()
        self._reset_update_queues()

    async def _create_state_queue(self):
        self.state_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.state_queue.bind(self.exchange, self.state_routing_key)
        logging.info(f"{self.log_prefix} creates state queue: {self.state_queue.name}")

    async def _create_response_queues(self):
        self.response_futures = {}
        self.response_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.response_queue.bind(self.exchange, self.response_routing_key)
        logging.info(
            f"{self.log_prefix} creates response queue: {self.response_queue.name} -> binded to {self.response_routing_key} ex:{self.exchange}"
        )

    async def _create_update_queue(self):
        self.update_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.update_queue.bind(self.exchange, self.update_routing_key)
        logging.info(
            f"{self.log_prefix} creates update queue: {self.update_queue.name} -> binded to {self.update_routing_key} ex:{self.exchange}"
        )

    async def _create_control_callback_queue(self):
        self.control_callback_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        self.control_futures = {}
        logging.info(
            f"{self.log_prefix} creates control callback queue: {self.control_callback_queue.name}"
        )

    async def _create_queues(self):
        await self._create_control_callback_queue()
        await self._create_response_queues()
        await self._create_state_queue()
        await self._create_update_queue()

    async def start_state(self):
        await self._create_state_queue()
        self._state_consume_tag = await self.state_queue.consume(
            self.on_state_response, no_ack=True
        )

    async def start_control(self):
        await self._create_control_callback_queue()
        self._control_callback_queue_consume_tag = (
            await self.control_callback_queue.consume(
                self.on_control_response, no_ack=True
            )
        )

    async def start_response(self):
        await self._create_response_queues()
        # the consume here is not blocking
        self._response_queue_consume_tag = await self.response_queue.consume(
            self.on_response, no_ack=True
        )

    async def start_update(self):
        await self._create_update_queue()
        self._update_queue_consume_tag = await self.update_queue.consume(
            self.on_update, no_ack=True
        )

    async def start(
        self,
        start_response: bool = True,
        start_control: bool = True,
        start_state: bool = True,
        start_update: bool = True,
    ):
        if start_response:
            await self.start_response()
        if start_control:
            await self.start_control()
        if start_state:
            await self.start_state()
        if start_update:
            await self.start_update()

        logging.info(f"{self.log_prefix} starts up")

        return self

    async def stop(
        self,
        stop_response: bool = True,
        stop_control: bool = True,
        stop_state: bool = True,
        stop_update: bool = True,
    ):
        if stop_response:
            if (
                self.response_queue is not None
                and self._response_queue_consume_tag is not None
            ):
                # await self.response_queue.unbind(self.exchange, self.response_routing_key)
                await self.response_queue.cancel(self._response_queue_consume_tag)
                await self.response_queue.delete(if_empty=False)
                self._reset_response_queues()
        if stop_control:
            if (
                self.control_callback_queue is not None
                and self._control_callback_queue_consume_tag is not None
            ):
                await self.control_callback_queue.cancel(
                    self._control_callback_queue_consume_tag
                )
                await self.control_callback_queue.purge()
                # await asyncio.sleep(0.05)
                await self.control_callback_queue.delete(if_empty=False)
                self._reset_control_queues()
        if stop_update:
            if (
                self.update_queue is not None
                and self._update_queue_consume_tag is not None
            ):
                # await self.update_queue.unbind(self.exchange, self.update_routing_key)
                await self.update_queue.cancel(self._update_queue_consume_tag)
                await self.update_queue.purge()
                # await asyncio.sleep(0.05)
                await self.update_queue.delete(if_empty=False)
                self._reset_update_queues()
        if stop_state:
            if self.state_queue is not None and self._state_consume_tag is not None:
                # await self.state_queue.unbind(self.exchange, self.state_routing_key)
                await self.state_queue.cancel(self._state_consume_tag)
                await self.state_queue.purge()
                # await asyncio.sleep(0.05)
                await self.state_queue.delete(if_empty=False)
                self._reset_state_queues()

    async def on_update(self, message: AbstractIncomingMessage) -> None:
        logging.info(f"{self.log_prefix} on update response")
        if self.on_update_callback is not None:
            await self.on_update_callback(message)

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id in self.response_futures:
            response_future = self.response_futures.pop(message.correlation_id)
            response_future.set_result(
                ResponseMessageQueueMessage(message.body, message.headers)
            )
        # elif self.on_response_callback is not None:
        #     await self.on_response_callback(message)
        else:
            pass

    async def on_control_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"{self.log_prefix} receive bad control message {message!r}")
        else:
            logging.info(
                f"{self.log_prefix} receives an control response ({message.correlation_id})"
            )
            future: asyncio.Future = self.control_futures.pop(message.correlation_id)
            future.set_result(
                ControlResponseMessageQueueMessage(message.body, message.headers)
            )

    async def on_state_response(self, message: AbstractIncomingMessage) -> None:
        logging.info(f"{self.log_prefix} on state response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"{self.log_prefix} received a bad message {message!r}")
            return

        if self.on_state_callback is not None:
            stop_flag = await self.on_state_callback(message)
            if stop_flag:
                logging.info(
                    f"{self.log_prefix} state callback stop_flag: {stop_flag} raises, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    async def request(
        self, message: RequestMessageQueueMessage
    ) -> ResponseMessageQueueMessage:

        # logging.info(f"{self.log_prefix} requests an item ({correlation_id})")
        logging.info(f"{self.log_prefix} requests an item")

        try:
            correlation_id = str(uuid.uuid4())
            loop = asyncio.get_running_loop()
            future = loop.create_future()

            await self.exchange.publish(
                Message(
                    message.body,
                    content_type="text/plain",
                    correlation_id=correlation_id,
                    reply_to=None,
                    headers=message.headers,
                ),
                routing_key=self.request_routing_key,
            )
            self.response_futures[correlation_id] = future
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to publish content: {e}")
            raise e

        return await future

    async def request_control(
        self, control_request_message: ControlRequestMessageQueueMessage
    ):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(f"{self.log_prefix} requests an control item ({correlation_id})")

        self.control_futures[correlation_id] = future

        try:
            await self.exchange.publish(
                Message(
                    control_request_message.body,
                    content_type="text/plain",
                    correlation_id=correlation_id,
                    reply_to=self.control_callback_queue.name,
                    headers=control_request_message.headers,
                ),
                routing_key=self.control_routing_key,
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to publish control content: {e}")
            raise e

        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info(f"{self.log_prefix} requests control times out")
            return self.create_control_response_message(
                b"",
                headers={},
                control_request_type=control_request_message.headers[
                    "control_request_type"
                ],
                control_response_type=control_request_message.headers[
                    "control_request_type"
                ],
                succ=False,
                error_type="timeout",
                error_message="request control times out",
            )

    async def shutdown_server(self):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="shutdown"
        )

        response = await self.request_control(message)
        return response

    async def config_server(self, config):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="config"
        )

        response = await self.request_control(message)

        return response

    async def get_state(self, return_bytes=False):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="state"
        )

        response = await self.request_control(message)
        if return_bytes:
            return response.body
        else:
            return decode_json(response.body)

    @property
    def log_prefix(self):
        return f"{self.client_type} <{self.client_name}>"


class BasicClient(BaseControlMessageMixin, BaseRequestMessageMixin):
    channel: Optional[AbstractChannel]
    exchange: Optional[AbstractExchange]
    request_routing_key: str
    control_routing_key: str
    state_routing_key: str
    callback_queue: Optional[AbstractQueue]
    client_name: str
    time_out: float
    on_state_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]

    client_type: str = "MQ client"

    def __init__(
        self,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        request_routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        client_name: str,
        time_out: float,
        on_state_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
    ) -> None:
        self.channel = channel
        self.exchange = exchange
        self.request_routing_key = request_routing_key
        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key
        self.client_name = client_name
        self.time_out = time_out
        self.on_state_callback = on_state_callback
        self._reset_queues()

    @classmethod
    def from_config(
        cls,
        config: dict,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        time_out: float,
        on_state_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        name_suffix: str = "",
    ):
        return cls(
            channel=channel,
            exchange=exchange,
            request_routing_key=config["request_key"],
            control_routing_key=config["ctrl_key"],
            state_routing_key=config["state_key"],
            client_name=config["name"] + name_suffix,
            time_out=time_out,
            on_state_callback=on_state_callback,
        )

    def _reset_response_queues(self):
        self.callback_queue = None
        self._callback_queue_consume_tag = None

    def _reset_control_queues(self):
        self.control_callback_queue = None
        self._control_callback_queue_consume_tag = None

    def _reset_state_queues(self):
        self.state_queue = None
        self._state_consume_tag = None

    def _reset_queues(self):
        self._reset_response_queues()
        self._reset_control_queues()
        self._reset_state_queues()

    async def _create_state_queue(self):
        logging.info(f"{self.log_prefix} creates state queue")
        self.state_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.state_queue.bind(self.exchange, self.state_routing_key)

    async def _create_response_queue(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)

        self.futures = {}
        logging.info(
            f"{self.log_prefix} creates callback queue: {self.callback_queue.name}"
        )

    async def _create_control_callback_queue(self):
        self.control_callback_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)

        self.control_futures = {}
        logging.info(
            f"{self.log_prefix} creates control callback queue: {self.control_callback_queue.name}"
        )

    async def create_queues(self):
        await self._create_control_callback_queue()
        await self._create_response_queue()
        await self._create_state_queue()

    async def start_response(self):
        await self._create_response_queue()
        self._callback_queue_consume_tag = await self.callback_queue.consume(
            self.on_response, no_ack=True
        )

    async def start_control(self):
        await self._create_control_callback_queue()
        self._control_callback_queue_consume_tag = (
            await self.control_callback_queue.consume(
                self.on_control_response, no_ack=True
            )
        )

    async def start_state(self):
        await self._create_state_queue()
        self._state_consume_tag = await self.state_queue.consume(
            self.on_state_response, no_ack=True
        )

    async def start(
        self,
        start_response: bool = True,
        start_control: bool = True,
        start_state: bool = True,
    ):

        if start_response:
            await self.start_response()
        if start_control:
            await self.start_control()
        if start_state:
            await self.start_state()

        logging.info(f"{self.log_prefix} starts up")

        return self

    async def stop(
        self,
        stop_response: bool = True,
        stop_control: bool = True,
        stop_state: bool = True,
    ):
        if stop_response:
            if (
                self.callback_queue is not None
                and self._callback_queue_consume_tag is not None
            ):
                await self.callback_queue.cancel(self._callback_queue_consume_tag)
                await self.callback_queue.purge()
                await self.callback_queue.delete(if_empty=False)
                self._reset_response_queues()

        if stop_control:
            if (
                self.control_callback_queue is not None
                and self._control_callback_queue_consume_tag is not None
            ):
                await self.control_callback_queue.cancel(
                    self._control_callback_queue_consume_tag
                )
                await self.control_callback_queue.purge()
                await self.control_callback_queue.delete(if_empty=False)
                self._reset_control_queues()

        if stop_state:
            if self.state_queue is not None and self._state_consume_tag is not None:
                # await self.state_queue.unbind(self.exchange, self.state_routing_key)
                await self.state_queue.cancel(self._state_consume_tag)
                await self.state_queue.purge()
                await self.state_queue.delete(if_empty=False)
                self._reset_state_queues()

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"{self.log_prefix} receives bad message {message!r}")
            return

        logging.info(f"{self.log_prefix} receives an item ({message.correlation_id})")
        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result(BaseMessageQueueMessage(message.body, message.headers))

    async def on_control_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"{self.log_prefix} receive bad control message {message!r}")
            return

        logging.info(
            f"{self.log_prefix} receives an control response ({message.correlation_id})"
        )
        future: asyncio.Future = self.control_futures.pop(message.correlation_id)
        future.set_result(BaseMessageQueueMessage(message.body, message.headers))

    async def on_state_response(self, message: AbstractIncomingMessage) -> None:
        logging.info(f"{self.log_prefix} on state response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"{self.log_prefix} received a bad message {message!r}")
            return

        if self.on_state_callback is not None:
            stop_flag = await self.on_state_callback(message)
            if stop_flag:
                logging.info(
                    f"{self.log_prefix} state callback stop_flag: {stop_flag} raises, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    @property
    def empty_response(self):
        return BaseMessageQueueMessage(b"", headers={})

    async def request(
        self, message: RequestMessageQueueMessage
    ) -> ResponseMessageQueueMessage:
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(f"{self.log_prefix} requests an item ({correlation_id})")

        self.futures[correlation_id] = future

        try:
            await self.exchange.publish(
                Message(
                    message.body,
                    content_type="text/plain",
                    correlation_id=correlation_id,
                    reply_to=self.callback_queue.name,
                    headers=message.headers,
                ),
                routing_key=self.request_routing_key,
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to publish content: {e}")
            raise e

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
                f"{self.log_prefix} waits for response with timeout {self.time_out}s"
            )
            return await asyncio.wait_for(future, timeout=self.time_out)

        except asyncio.TimeoutError:
            logging.info(f"{self.log_prefix} resquests time out")
            return self.empty_response

    async def request_control(
        self, control_request_message: ControlRequestMessageQueueMessage
    ):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(f"{self.log_prefix} requests an control item ({correlation_id})")

        self.control_futures[correlation_id] = future

        try:
            await self.exchange.publish(
                Message(
                    control_request_message.body,
                    content_type="text/plain",
                    correlation_id=correlation_id,
                    reply_to=self.control_callback_queue.name,
                    headers=control_request_message.headers,
                ),
                routing_key=self.control_routing_key,
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to publish control content: {e}")
            raise e

        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info(f"{self.log_prefix} requests control times out")
            return self.empty_response

    async def shutdown_server(self):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="shutdown"
        )
        response = await self.request_control(message)
        return response

    async def config_server(self, config):
        message = self.create_control_request_message(
            config, headers={}, control_request_type="config"
        )
        response = await self.request_control(message)

        return response

    async def get_state(self, return_bytes=False):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="state"
        )
        response = await self.request_control(message)

        if return_bytes:
            return response.body
        else:
            return decode_json(response.body)

    @property
    def log_prefix(self):
        return f"{self.client_type} <{self.client_name}>"


class BasicStreamClient(BaseControlMessageMixin, BaseStreamMessageMixin):
    channel: Optional[AbstractChannel]
    exchange: Optional[AbstractExchange]
    publish_routing_key: str
    control_routing_key: str
    state_routing_key: str
    client_name: str
    time_out: float
    on_response_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]
    on_state_callback: Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]

    queue: Optional[AbstractQueue]
    control_callback_queue: Optional[AbstractQueue]
    client_type: str = "MQ stream client"

    def __init__(
        self,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        publish_routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        client_name: str,
        time_out: float,
        on_response_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        on_state_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
    ) -> None:
        """
        Initialize a BasicStreamClient.

        Args:
            channel (Optional[AbstractChannel]): The channel for communication.
            exchange (Optional[AbstractExchange]): The exchange for routing messages.
            routing_key (str): The routing key for main messages.
            control_routing_key (str): The routing key for control messages.
            state_routing_key (str): The routing key for state messages.
            on_response_callback (Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]):
                Callback function for handling responses, return True to stop the streaming.
            on_state_callback (Optional[Callable[[AbstractIncomingMessage], Awaitable[bool]]]):
                Callback function for handling state changes, return True to stop the streaming.
            client_name (str): The name of the client.
            time_out (float): The timeout duration for operations.

        This class sets up a basic streaming client with capabilities for main, control,
        and state message handling. It provides methods for starting and stopping
        message consumption, as well as sending requests and handling responses.
        """

        self.channel = channel
        self.exchange = exchange
        self.publish_routing_key = publish_routing_key
        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key
        self.on_response_callback = on_response_callback
        self.on_state_callback = on_state_callback
        self.client_name = client_name
        self.time_out = time_out

        self.reset_queues()

    @classmethod
    def from_config(
        cls,
        config: dict,
        channel: Optional[AbstractChannel],
        exchange: Optional[AbstractExchange],
        time_out: float,
        on_response_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        on_state_callback: Optional[
            Callable[[AbstractIncomingMessage], Awaitable[bool]]
        ] = None,
        name_suffix: str = "",
    ):
        return cls(
            channel=channel,
            exchange=exchange,
            publish_routing_key=config["publish_key"],
            control_routing_key=config["ctrl_key"],
            state_routing_key=config["state_key"],
            client_name=config["name"] + name_suffix,
            time_out=time_out,
            on_response_callback=on_response_callback,
            on_state_callback=on_state_callback,
        )

    @property
    def empty_response(self):
        return BaseMessageQueueMessage(b"", {})

    def update_on_reponse_callback(
        self, on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]]
    ):
        """
        Update the response callback function.

        Args:
            on_response_callback (Callable[[AbstractIncomingMessage], Awaitable[bool]]):
                The callback function for handling responses, return True to stop the streaming.

        Raises:
            Exception: If the current job is not stopped, raise an exception.
        """
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

    def _reset_main_queue(self):
        self._consumer_tag = None
        self.queue = None

    def _reset_control_queue(self):
        self.control_callback_queue = None
        self._control_callback_consumer_tag = None

    def _reset_state_queue(self):
        self._state_consumer_tag = None
        self.state_queue = None

    def reset_queues(self):
        self._reset_control_queue()
        self._reset_main_queue()
        self._reset_state_queue()

    async def create_main_queue(self):
        # this queue is for listening to stream
        self.queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.queue.bind(self.exchange, routing_key=self.publish_routing_key)
        logging.info(f"{self.log_prefix} creates queue: {self.queue.name}")

    async def create_control_queue(self):
        self.control_callback_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        self.control_futures = {}
        logging.info(
            f"{self.log_prefix} creates control callback queue: {self.control_callback_queue.name}"
        )

    async def create_state_queue(self):
        logging.info(f"{self.log_prefix} creates state queue")
        self.state_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.state_queue.bind(self.exchange, self.state_routing_key)

    async def create_queues(self):
        await self.create_main_queue()
        await self.create_control_queue()
        await self.create_state_queue()

    async def start_main(self, start_consume_loop=True):
        await self.create_main_queue()
        if start_consume_loop:
            logging.info(f"{self.log_prefix} starts live streaming consume loop")

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

    async def start(
        self,
        start_main: bool = True,
        start_control: bool = True,
        start_state: bool = True,
        start_consume_loop: bool = True,
    ):
        if start_main:
            await self.start_main(start_consume_loop=start_consume_loop)
        if start_control:
            await self.start_control(start_consume_loop=start_consume_loop)
        if start_state:
            await self.start_state(start_consume_loop=start_consume_loop)

        logging.info(f"{self.log_prefix} starts live streaming")

    async def stop_main(self) -> Tuple[bool, str]:
        succ, error = True, ""
        
        if self.is_main_running():
            # await self.queue.unbind(exchange=self.exchange, routing_key=self.publish_routing_key)
            try:
                await self.queue.cancel(
                    self._consumer_tag,
                )
                await self.queue.purge()

                # await asyncio.sleep(0.05)
                await self.queue.delete(if_empty=False)
            except Exception as e:
                logging.error(f"{self.log_prefix} Fail to delete main queue: {get_error_info(e)}. May have to restart the server")
            
            self._reset_main_queue()

        return succ, error

    async def stop_control(self) -> Tuple[bool, str]:
        succ, error = True, ""

        if self.is_control_running():
            try:
                await self.control_callback_queue.cancel(
                    self._control_callback_consumer_tag,
                )
                await self.control_callback_queue.purge()

                # await asyncio.sleep(0.05)
                await self.control_callback_queue.delete(if_empty=False)
            except Exception as e:
                logging.error(f"{self.log_prefix} Fail to delete control queue: {get_error_info(e)}. May have to restart the server")
                succ, error = False, get_error_info(e)

            self._reset_control_queue()
        return succ, error

    async def stop_state(self) -> Tuple[bool, str]:
        succ, error = True, ""
        
        if self.is_state_running():
            # await self.state_queue.unbind(exchange=self.exchange, routing_key=self.state_routing_key)

            try:
                await self.state_queue.cancel(
                    self._state_consumer_tag,
                )
                await self.state_queue.purge()

                await asyncio.sleep(0.05)
                await self.state_queue.delete(if_empty=False)
            except Exception as e:
                logging.error(f"{self.log_prefix} Fail to delete state queue: {get_error_info(e)}. May have to restart the server")
                succ, error = False, get_error_info(e)

            self._reset_state_queue()
        return succ, error

    async def stop(
        self, stop_main: bool = True, stop_control: bool = True, stop_state: bool = True
    ) -> Tuple[bool, Dict[str, str]]:
        succ, errors = True, {}
        logging.info(f"{self.log_prefix} terminate consume")
        if stop_main:
            succ_main, error_main = await self.stop_main()
        if stop_control:
            succ_control, error_control = await self.stop_control()
        if stop_state:
            succ_state, error_state = await self.stop_state()

        succ = succ_main and succ_control and succ_state
        errors = { "error_main": error_main, "error_control": error_control, "error_state": error_state }
        return succ, errors

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        logging.debug(f"{self.log_prefix} on live response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"{self.log_prefix} received a bad message {message!r}")
            return

        if self.on_response_callback is not None:
            stop_flag = await self.on_response_callback(message)
            if stop_flag:
                logging.info(
                    f"{self.log_prefix} response callback stop_flag {stop_flag} raises, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    async def on_control_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"{self.log_prefix} receive bad control message {message!r}")
            return

        logging.info(
            f"{self.log_prefix} receives an control response ({message.correlation_id})"
        )
        future: asyncio.Future = self.control_futures.pop(message.correlation_id)
        future.set_result(BaseMessageQueueMessage(message.body, message.headers))

    async def on_state_response(self, message: AbstractIncomingMessage) -> None:
        logging.info(f"{self.log_prefix} on state response")

        # TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"{self.log_prefix} received a bad message {message!r}")
            return

        if self.on_state_callback is not None:
            stop_flag = await self.on_state_callback(message)
            if stop_flag:
                logging.info(
                    f"{self.log_prefix} state callback stop_flag: {stop_flag}, termiante the streaming process"
                )
                await self.stop()
        else:
            pass

    async def request_control(
        self, control_request_message: ControlRequestMessageQueueMessage
    ):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(f"{self.log_prefix} request an control item ({correlation_id})")

        self.control_futures[correlation_id] = future

        try:
            await self.exchange.publish(
                Message(
                    control_request_message.body,
                    content_type="text/plain",
                    correlation_id=correlation_id,
                    reply_to=self.control_callback_queue.name,
                    headers=control_request_message.headers,
                ),
                routing_key=self.control_routing_key,
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to publish control content: {e}")
            raise e

        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info(f"{self.log_prefix} requests control times out")
            return self.empty_response

    async def start_server_streaming(self):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="start"
        )
        return await self.request_control(message)

    async def stop_server_streaming(self):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="stop"
        )
        return await self.request_control(message)

    async def shutdown_server(self):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="shutdown"
        )
        response = await self.request_control(message)

    async def config_server(self, config):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="config"
        )
        return await self.request_control(message)

    async def get_state(self, return_bytes=False):
        message = self.create_control_request_message(
            "", headers={}, control_request_type="state"
        )
        response = await self.request_control(message)
        if return_bytes:
            return response.body
        else:
            return decode_json(response.body)

    @property
    def log_prefix(self):
        return f"{self.client_type} <{self.client_name}>"


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
            asyncio.create_task(self.on_state_change_callback(self))


class StateCallbackMixin(BaseStateMessageMixin):
    exchange: AbstractExchange
    state_routing_key: str
    log_prefix: str

    async def on_state_callback(self, state: Dict):
        response = self.create_state_message(
            body=state,
            headers={},
            state_type="mono",
        )
        logging.info(f"{self.log_prefix} state prepared")

        try:
            await self.exchange.publish(
                Message(
                    body=response.body,
                    headers=response.headers,
                ),
                routing_key=self.state_routing_key,
            )
            logging.info(f"{self.log_prefix} send state content")
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to publish state content: {e}")
            raise e


class BasicStreamServer(
    BaseControlMixin,
    StateCallbackMixin,
    BaseControlMessageMixin,
    BaseStreamMessageMixin,
):

    channel: AbstractChannel
    exchange: AbstractExchange
    control_routing_key: str
    publish_routing_key: str
    state_routing_key: str

    control_queue: AbstractQueue
    server_name: str

    stream_flag: bool
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

        self.stream_flag = False
        self.state = ServerState(
            {
                "is_running": False,
                "is_streaming": False,
            },
            self.on_state_callback,
        )

        self.reset_queues()

    @property
    def log_prefix(self):
        return f"{self.server_type} <{self.server_name}>"

    def update_state(self):
        """
        a customizable function that use to update other part of the state the during operation.
        Not implemented is ok, the base function will do nothing.
        """
        pass

    def set_stream_flag(self, state):
        self.stream_flag = state

    def reset_queues(self):
        self.control_queue = None
        self._control_consume_tag = None

    async def create_control_queue(self):
        logging.info(f"{self.log_prefix} creates control queue")
        self.control_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.control_queue.bind(self.exchange, self.control_routing_key)

    async def create_queues(self):
        await self.create_control_queue()

    async def on_streaming(self) -> StreamMessageQueueMessage | None:
        """
        return None if no content is prepared else return a StreamMessageQueueMessage
        """
        raise NotImplementedError()

    async def streaming(self):
        while True:
            # update the state at each iteration of the publish loop
            self.update_state()

            if self.stream_flag:
                self.state["is_streaming"] = True

                try:
                    stream_message = await self.on_streaming()
                    logging.debug(f"{self.log_prefix} content prepared")
                except Exception as e:
                    logging.error(f"{self.log_prefix} content preparation failed: {e}")
                    await asyncio.sleep(0.1)

                try:
                    # print(f"{self.log_prefix} try to publish content")
                    if stream_message is not None:
                        await self.exchange.publish(
                            Message(
                                body=stream_message.body,
                                headers=stream_message.headers,
                            ),
                            routing_key=self.publish_routing_key,
                        )
                        logging.debug(f"{self.log_prefix} send content")
                    else:
                        # 0.01 too fast for some computer
                        # TODO: need to optimize the server in some way
                        await asyncio.sleep(0.05)

                except Exception as e:
                    logging.error(f"{self.log_prefix} fail to publish content: {e}")
                    raise e
                # stream_message = self.create_stream_message(
                #     f"fail to prepare content, error_message {e}", {}, "error"
                # )

                # logging.debug(f"{self.log_prefix} fail to prepare content")

            else:
                self.state["is_streaming"] = False

                logging.debug(f"{self.log_prefix} stream loop is paused")
                await asyncio.sleep(0.1)

        # the example didn't ack back if process() method is used
        # await message.ask()

    @BaseControlMixin.register_control_callback("start")
    async def start_streaming(self, body, headers):
        self.set_stream_flag(True)
        return self.create_control_response_message(
            "",
            {},
            control_request_type="start",
            control_response_type="start",
            succ=True,
            error_type="",
            error_message="",
        )

    @BaseControlMixin.register_control_callback("stop")
    async def end_streaming(self, body, headers):
        self.set_stream_flag(False)
        return self.create_control_response_message(
            "",
            {},
            control_request_type="stop",
            control_response_type="stop",
            succ=True,
            error_type="",
            error_message="",
        )

    async def on_config(self, body, headers) -> Tuple[Union[Dict, str, bytes], Dict]:
        raise NotImplementedError

    @BaseControlMixin.register_control_callback("config")
    async def config_server(self, body, headers):
        body, headers = await self.on_config(self, body, headers)
        self.update_state()
        return self.create_control_response_message(
            body,
            headers,
            control_request_type="config",
            control_response_type="config",
            succ=True,
            error_type="",
            error_message="",
        )

    @BaseControlMixin.register_control_callback("state")
    async def server_state(self, body, headers):
        # force update the state when query is conducted
        self.update_state()

        state = dict(self.state)
        return self.create_control_response_message(
            state,
            {},
            control_request_type="state",
            control_response_type="state",
            succ=True,
            error_type="",
            error_message="",
        )

    async def start(self):
        await self.create_queues()
        try:
            self._control_consume_tag = await self.control_queue.consume(
                callback=self.on_control_request, no_ack=False
            )
            self._streaming_task = asyncio.create_task(self.streaming())

            # is_running means the server is running
            self.state["is_running"] = True
            self.update_state()
        except Exception as e:
            print(e)
        logging.info(f"{self.log_prefix} starts up")

    async def cancel(self):
        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)
            await self.control_queue.purge()
            # await asyncio.sleep(0.05)
            await self.control_queue.delete(if_empty=False)

        # this is to stop the streaming task
        self.set_stream_flag(False)

        self.reset_queues()

    @BaseControlMixin.register_control_callback("terminate")
    async def terminate_server(self):
        await self.cancel()
        return self.create_control_response_message(
            "",
            {},
            control_request_type="terminate",
            control_response_type="terminate",
            succ=True,
            error_type="",
            error_message="",
        )


class BasicServer(
    BaseControlMixin,
    BaseRequestMessageMixin,
    StateCallbackMixin,
    BaseControlMessageMixin,
):
    channel: AbstractChannel
    exchange: AbstractExchange
    callback_exchange: AbstractExchange
    control_routing_key: str
    request_routing_key: str
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
        request_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.request_routing_key = request_routing_key
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

    @property
    def log_prefix(self):
        return f"{self.server_type} <{self.server_name}>"

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
        logging.info(f"{self.log_prefix} creates control queue")
        self.control_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.control_queue.bind(self.exchange, self.control_routing_key)

    async def create_main_queue(self):
        logging.info(f"{self.log_prefix} creates main queue")
        self.queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.queue.bind(self.exchange, routing_key=self.request_routing_key)

    async def create_queues(self):
        await self.create_main_queue()
        await self.create_control_queue()

    async def on_message(
        self, message: AbstractIncomingMessage
    ) -> ResponseMessageQueueMessage:
        """
        Overwrite this base method with the implementation of the server functionality.
        Returns:
            RequestMessageQueueMessage: Message containing body and headers to send back
        """
        raise NotImplementedError()

    async def publish_callback(
        self,
        response_message: ResponseMessageQueueMessage,
        received_message: AbstractIncomingMessage,
    ):
        """
        publish the content to the callback queue
        raise exception if fail to publish

        Args:
            body: The message body as bytes to publish
            headers: Dict of message headers to include
            message: The original incoming message containing correlation_id and reply_to

        Raises:
            Exception: If publishing fails
        """
        try:
            await self.callback_exchange.publish(
                Message(
                    body=response_message.body,
                    headers=response_message.headers,
                    correlation_id=received_message.correlation_id,
                ),
                routing_key=received_message.reply_to,
            )
            logging.debug(
                f"{self.log_prefix} content delivered to {received_message.reply_to}"
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to deliver content: {e}")
            raise e

        # update the state at each call
        self.update_state()

    async def on_request(self, message: AbstractIncomingMessage):
        logging.info(f"{self.log_prefix} on request")
        async with message.process():
            assert (
                message.reply_to is not None
            ), f"Receive a bad request without .reply_to field"

            response = await self.on_message(message)
            logging.info(f"{self.log_prefix} content is prepared")

            await self.publish_callback(response, message)
            # the example didn't ack back if process() method is used
            # await message.ask()

    async def on_config(self, body, headers):
        raise NotImplementedError

    @BaseControlMixin.register_control_callback("config")
    async def config_server(self, body, headers):
        body, headers = await self.on_config(self, body, headers)
        return self.create_control_response_message(
            body,
            headers,
            control_request_type="config",
            control_response_type="config",
            succ=True,
            error_type="",
            error_message="",
        )

    @BaseControlMixin.register_control_callback("state")
    async def server_state(self, body, headers):
        # force update the state when query is conducted
        self.update_state()

        state = dict(self.state)
        return self.create_control_response_message(
            state,
            {},
            control_request_type="state",
            control_response_type="state",
            succ=True,
            error_type="",
            error_message="",
        )

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
            logging.error(f"{self.log_prefix} experience an error: {e}")

        logging.info(f"{self.log_prefix} starts up")

    async def cancel(self):
        if self.queue is not None and self._consume_tag is not None:
            await self.queue.cancel(self._consume_tag)
            await self.queue.purge()
            # await asyncio.sleep(0.05)
            await self.queue.delete(if_empty=False)

        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)
            await self.control_queue.purge()
            # await asyncio.sleep(0.05)
            await self.control_queue.delete(if_empty=False)

        self.reset_queues()
        self.state["is_running"] = False

    @BaseControlMixin.register_control_callback("terminate")
    async def terminate_server(self):
        await self.cancel()
        return self.create_control_response_message(
            "",
            {},
            control_request_type="terminate",
            control_response_type="terminate",
            succ=True,
            error_type="",
            error_message="",
        )


class PubSubServer(
    BaseControlMixin,
    StateCallbackMixin,
    BaseRequestMessageMixin,
    BaseControlMessageMixin,
):
    channel: AbstractChannel
    exchange: AbstractExchange
    callback_exchange: AbstractExchange
    control_routing_key: str
    request_routing_key: str
    response_routing_key: str

    state_routing_key: str
    queue: AbstractQueue
    server_name: str

    update_flag: bool
    state: ServerState
    server_type: str = "MQ PubSub server"

    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        request_routing_key: str,
        response_routing_key: str,
        update_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.request_routing_key = request_routing_key
        self.response_routing_key = response_routing_key
        self.control_routing_key = control_routing_key
        self.state_routing_key = state_routing_key
        self.update_routing_key = update_routing_key
        self.server_name = server_name
        self.reset_queues()

        self.update_flag = True

        self.state = ServerState(
            {
                "is_running": False,
            },
            self.on_state_callback,
        )

    @property
    def log_prefix(self):
        return f"{self.server_type} <{self.server_name}>"

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
        logging.info(f"{self.log_prefix} creates control queue")
        self.control_queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.control_queue.bind(self.exchange, self.control_routing_key)

    async def create_main_queue(self):
        logging.info(f"{self.log_prefix} creates main queue")
        self.queue = await self.channel.declare_queue(exclusive=True, auto_delete=False)
        await self.queue.bind(self.exchange, routing_key=self.request_routing_key)

    async def start_control(self):
        await self.create_control_queue()
        try:
            self._control_consume_tag = await self.control_queue.consume(
                callback=self.on_control_request, no_ack=False
            )
            self.state["is_control_running"] = True
            self.update_state()
        except Exception as e:
            logging.error(f"{self.log_prefix} experience an error: {e}")

    async def start_main(self):
        await self.create_main_queue()
        try:
            self._consume_tag = await self.queue.consume(self.on_request, no_ack=False)
            self.state["is_main_running"] = True
            self.update_state()
        except Exception as e:
            logging.error(f"{self.log_prefix} experience an error: {e}")

    async def create_queues(self):
        await self.create_main_queue()
        await self.create_control_queue()

    async def on_message(
        self, message: AbstractIncomingMessage
    ) -> ResponseMessageQueueMessage:
        """
        Overwrite this base method with the implementation of the server functionality.
        Returns:
            RequestMessageQueueMessage: Message containing:
                - body (bytes): The response message body
                - headers (Dict): Headers for the response message
        """
        raise NotImplementedError()

    async def publish_response(
        self,
        response_message: ResponseMessageQueueMessage,
        received_message: AbstractIncomingMessage,
    ):
        """
        publish the content to the callback queue
        raise exception if fail to publish

        Args:
            body: The message body as bytes to publish
            headers: Dict of message headers to include
            message: The original incoming message containing correlation_id and reply_to

        Raises:
            Exception: If publishing fails
        """
        try:
            await self.exchange.publish(
                Message(
                    body=response_message.body,
                    headers=response_message.headers,
                    correlation_id=received_message.correlation_id,
                ),
                routing_key=self.response_routing_key,
            )
            logging.info(
                f"{self.log_prefix} content delivered to {self.response_routing_key} ex:{self.exchange}"
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to deliver content: {e}")
            raise e

        # update the state at each call
        self.update_state()

    async def on_update(self) -> UpdateMessageQueueMessage | None:
        """
        return None if no content is prepared else return a UpdateMessageQueueMessage
        """
        raise NotImplementedError()

    async def updating(self):
        while True:
            if self.update_flag:
                self.state["is_updating"] = True

                try:
                    update_message = await self.on_update()
                    logging.debug(f"{self.log_prefix} content prepared")
                except Exception as e:
                    logging.error(f"{self.log_prefix} content preparation failed: {e}")
                    await asyncio.sleep(0.01)

                try:
                    # print(f"{self.log_prefix} try to publish content")
                    if update_message is not None:
                        logging.debug(f"{self.log_prefix} publish content")
                        await self.publish_update(update_message)
                    else:
                        # logging.info(f"{self.log_prefix} no content to publish, pause 0.01s")
                        await asyncio.sleep(0.01)

                except Exception as e:
                    logging.error(f"{self.log_prefix} fail to publish content: {e}")
                    await asyncio.sleep(0.01)
                    raise e

            else:
                self.state["is_updating"] = False

                logging.debug(f"{self.log_prefix} update loop is paused")
                await asyncio.sleep(0.1)

    async def publish_update(
        self,
        update_message: UpdateMessageQueueMessage,
    ):
        """
        publish the content to the callback queue
        raise exception if fail to publish

        Args:
            body: The message body as bytes to publish
            headers: Dict of message headers to include
            message: The original incoming message containing correlation_id and reply_to

        Raises:
            Exception: If publishing fails
        """
        try:
            await self.exchange.publish(
                Message(
                    body=update_message.body,
                    headers=update_message.headers,
                ),
                routing_key=self.update_routing_key,
            )
            logging.info(
                f"{self.log_prefix} content delivered to {self.update_routing_key} ex:{self.exchange}"
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} fail to deliver content: {e}")
            raise e

    async def on_request(self, message: AbstractIncomingMessage):
        logging.info(f"{self.log_prefix} on request")
        async with message.process():
            response = await self.on_message(message)
            logging.info(f"{self.log_prefix} content is prepared")

            await self.publish_response(response, message)
            # the example didn't ack back if process() method is used
            # await message.ask()

    async def on_config(self, body, headers) -> Tuple[Union[Dict, str, bytes], Dict]:
        raise NotImplementedError

    @BaseControlMixin.register_control_callback("config")
    async def config_server(self, body, headers):
        body, headers = await self.on_config(self, body, headers)
        return self.create_control_response_message(
            body,
            headers,
            control_request_type="config",
            control_response_type="config",
            succ=True,
            error_type="",
            error_message="",
        )

    @BaseControlMixin.register_control_callback("state")
    async def server_state(self, body, headers):
        # force update the state when query is conducted
        self.update_state()

        state = dict(self.state)
        return self.create_control_response_message(
            state,
            {},
            control_request_type="state",
            control_response_type="state",
            succ=True,
            error_type="",
            error_message="",
        )

    async def start(self):
        # await self.create_queues()

        # try:
        #     self._control_consume_tag = await self.control_queue.consume(
        #         callback=self.on_control_request, no_ack=False
        #     )
        #     self._consume_tag = await self.queue.consume(self.on_request, no_ack=False)
        #     self.state["is_running"] = True
        #     self.update_state()
        # except Exception as e:
        #     logging.error(f"{self.log_prefix} experience an error: {e}")

        await self.start_control()
        await self.start_main()
        self._updating_task = asyncio.create_task(self.updating())
        self.state["is_running"] = True

        logging.info(f"{self.log_prefix} starts up")

    async def cancel(self):
        if self.queue is not None and self._consume_tag is not None:
            # await self.queue.unbind(self.exchange, self.request_routing_key)
            await self.queue.cancel(self._consume_tag)
            await self.queue.purge()
            # await asyncio.sleep(0.05)
            await self.queue.delete(if_empty=False)
            self.state["is_main_running"] = False

        if self.control_queue is not None and self._control_consume_tag is not None:
            await self.control_queue.cancel(self._control_consume_tag)
            await self.control_queue.purge()
            # await asyncio.sleep(0.05)
            await self.control_queue.delete(if_empty=False)
            self.state["is_control_running"] = False

        if self._updating_task is not None:
            self.update_flag = False

        self.reset_queues()
        self.state["is_running"] = False
        self.update_state()

    @BaseControlMixin.register_control_callback("terminate")
    async def terminate_server(self):
        await self.cancel()
        return self.create_control_response_message(
            "",
            {},
            control_request_type="terminate",
            control_response_type="terminate",
            succ=True,
            error_type="",
            error_message="",
        )
