import asyncio
from typing import Dict, List
import uuid
from lumi.storage.communication import (
    StorageMessageQueueClient,
)
from lumi.utils.common import decode_json
from lumi.client.base import StateCallbakcMixin

from aio_pika.abc import AbstractIncomingMessage


class StorageClient(StorageMessageQueueClient, StateCallbakcMixin):
    def __init__(
        self,
        channel,
        exchange,
        request_routing_key,
        control_routing_key,
        state_routing_key,
        client_name,
        time_out,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            request_routing_key=request_routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            client_name=client_name,
            time_out=time_out,
            on_state_callback=self._on_state_callback,
        )
        self.server_state = {}

