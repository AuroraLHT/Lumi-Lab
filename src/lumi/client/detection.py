import asyncio
from typing import Dict, List
import uuid
from lumi.detection.communication import (
    DetectionMessageQueueClient,
    LiveDetectionMessageQueueClient,
    decode_detections,
)
from lumi.utils.common import decode_json
from lumi.client.base import StateCallbakcMixin

from aio_pika.abc import AbstractIncomingMessage

import logging


class DetectionClient(DetectionMessageQueueClient, StateCallbakcMixin):

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

    async def get_detection(self):
        response = await super().get_detection()

        if response.headers["succ"]:
            det, det_headers = decode_detections(response.body, response.headers)
            return det, det_headers
        else:
            raise Exception(f"get detection failed {response.headers}")


class LiveDetectionClient(LiveDetectionMessageQueueClient, StateCallbakcMixin):
    def __init__(
        self,
        channel,
        exchange,
        publish_routing_key,
        control_routing_key,
        state_routing_key,
        client_name,
        time_out,
        on_response_callback,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            publish_routing_key=publish_routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            client_name=client_name,
            time_out=time_out,
            on_state_callback=self._on_state_callback,
            on_response_callback=on_response_callback,
        )
        self.server_state = {}
