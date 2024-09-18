import logging

import uuid
import asyncio
import aio_pika

from aio_pika import Message, connect
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)

from collections.abc import Callable, Awaitable

from ..base.message_queue import BasicStreamClient
from ..rheed.communication import LiveVideoFragmentsMessageQueueClient, CameraMessageQueueClient, VideoFragmentsMessageQueueClient
from ..detection.communication import LiveDetectionMessageQueueClient
from ..pascal.communication import LiveChamberLogMessageQueueClient, ChamberLogMessageQueueClient
from ..storage.communication import StorageMessageQueueClient
from ..rheed.communication import LiveSTFTMessageQueueClient, STFTMessageQueueClient, LiveIntegratorMessageQueueServer, IntegratorMessageQueueClient
