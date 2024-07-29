import logging

import uuid
import asyncio
import aio_pika

from aio_pika import Message, connect
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)

from collections.abc import Callable, Awaitable

from ..rheed.communitation import LiveVideoFragmentsMessageQueueClient, CameraMessageQueueClient, VideoFragmentsMessageQueueClient
from ..detection.communication import LiveDetectionMessageQueueClient
from ..pascal.communication import LiveChamberLogMessageQueueClient, ChamberLogMessageQueueClient
from ..storage.communication import StorageMessageQueueClient

