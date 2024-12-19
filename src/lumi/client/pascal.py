import asyncio
from typing import Dict, List
import uuid
from lumi.pascal.communication import (
    MIModeMessageQueueClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
)
from lumi.utils.common import decode_json
from lumi.client.base import StateCallbakcMixin

from aio_pika.abc import AbstractIncomingMessage

import logging

"""
This a notebook or interactive client that use message queue client to do the communication and store and cache the comm result
"""


class MIModeClient(MIModeMessageQueueClient, StateCallbakcMixin):
    all_executions: List[Dict]
    current_execution: Dict
    _commands_exectuion_futures: Dict[str, asyncio.Future]
    server_state: Dict

    def __init__(
        self,
        channel,
        exchange,
        request_routing_key,
        response_routing_key,
        update_routing_key,
        control_routing_key,
        state_routing_key,
        client_name,
        time_out,
        on_update_callback=None,
        on_state_callback=None,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            request_routing_key=request_routing_key,
            response_routing_key=response_routing_key,
            update_routing_key=update_routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            client_name=client_name,
            time_out=time_out,
            on_update_callback=self._on_update_callback if on_update_callback is None else on_update_callback,
            on_state_callback=self._on_state_callback if on_state_callback is None else on_state_callback,
        )
        self.all_executions = []
        self.current_execution = {}
        self._commands_exectuion_futures = {}
        self.server_state = {}

    @classmethod
    def from_config(cls, config, channel, exchange, time_out, on_state_callback=None, on_update_callback=None, name_suffix = ""):
        return super().from_config(
            config=config, 
            channel=channel, 
            exchange=exchange, 
            time_out=time_out, 
            on_state_callback=on_state_callback, 
            on_update_callback=on_update_callback, 
            name_suffix=name_suffix
        )

    async def _on_update_callback(self, message: AbstractIncomingMessage):
        body = message.body
        headers = message.headers

        # print("on_update_callback", body, "headers", headers)
        # print("--------------------------------------------")

        if headers["update_type"] == "all_executions":
            self.all_executions = decode_json(body)

        elif headers["update_type"] == "current_execution":
            self.current_execution = decode_json(body)

        elif headers["update_type"] == "execution_commands":
            finished_execution = decode_json(body)
            if finished_execution["commands_uuid"] in self._commands_exectuion_futures:
                future: asyncio.Future = self._commands_exectuion_futures.pop(
                    finished_execution["commands_uuid"]
                )
                future.set_result(finished_execution)
        else:
            raise Exception(f"Unknown update type -> {headers['update_type']}")

    async def execute_command(self, commands):
        commands_uuid = str(uuid.uuid4())

        register_response = await super().register_commands(
            commands_uuid=str(commands_uuid), commands=str(commands)
        )
        # print(register_response.headers)
        if register_response.headers["succ"]:
            command_future = asyncio.Future()
            self._commands_exectuion_futures[commands_uuid] = command_future
            # print("waiting for the response")
            return await command_future
        else:
            raise Exception(f"command register failed {register_response.headers}")


class LiveChamberLogClient(LiveChamberLogMessageQueueClient, StateCallbakcMixin):
    def __init__(
        self,
        channel,
        exchange,
        publish_routing_key,
        control_routing_key,
        state_routing_key,
        client_name,
        time_out,
        on_response_callback = None,
        on_state_callback = None
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            publish_routing_key=publish_routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            client_name=client_name,
            time_out=time_out,
            on_response_callback=on_response_callback,
            on_state_callback=self._on_state_callback if on_state_callback is None else on_state_callback,
        )
        self.server_state = {}


class ChamberLogClient(ChamberLogMessageQueueClient, StateCallbakcMixin):

    server_state: Dict

    def __init__(
        self,
        channel,
        exchange,
        request_routing_key,
        control_routing_key,
        state_routing_key,
        client_name,
        time_out,
        on_state_callback = None
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            request_routing_key=request_routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            client_name=client_name,
            time_out=time_out,
            on_state_callback=self._on_state_callback if on_state_callback is None else on_state_callback,
        )
        self.server_state = {}

    async def get_log(self):
        response = await super().get_log()

        if response.headers["succ"]:
            return decode_json(response.body)
        else:
            raise Exception(f"get log failed {response.headers}")
