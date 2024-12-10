# from lumi.utils.image import decode_img
# import requests

# def get_image(host):
#     r = requests.get(f'http://{host}/RHEED/image')

#     image, image_header = decode_img(r.content, r.headers)
#     return image, image_header

import asyncio
from typing import Dict, List
import uuid
from lumi.rheed.communication import (
    IntegratorMessageQueueClient,
    STFTMessageQueueClient,
    CameraMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    LiveVideoFragmentsMessageQueueClient,
    LiveCameraMessageQueueClient,
    LiveIntegratorMessageQueueClient,
    LiveSTFTMessageQueueClient,
)
from lumi.utils.common import decode_json
from lumi.client.base import StateCallbakcMixin

from aio_pika.abc import AbstractIncomingMessage
from lumi.utils.image import decode_img


class CameraClient(CameraMessageQueueClient, StateCallbakcMixin):

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

    async def get_image(self):
        response = await super().get_live_image()
        return decode_img(response.body, response.headers)


class LiveCameraClient(LiveCameraMessageQueueClient, StateCallbakcMixin):
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
            on_response_callback=on_response_callback,
            on_state_callback=self._on_state_callback,
        )
        self.server_state = {}

class STFTClient(STFTMessageQueueClient, StateCallbakcMixin):
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

class LiveSTFTClient(LiveSTFTMessageQueueClient, StateCallbakcMixin):
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


class IntegratorClient(IntegratorMessageQueueClient, StateCallbakcMixin):
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


class LiveIntegratorClient(LiveIntegratorMessageQueueClient, StateCallbakcMixin):
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


class VideoFragmentsClient(VideoFragmentsMessageQueueClient, StateCallbakcMixin):
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


class LiveVideoFragmentsClient(
    LiveVideoFragmentsMessageQueueClient, StateCallbakcMixin
):
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
