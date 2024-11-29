import logging

import uuid
import asyncio
import aio_pika

from aio_pika import Message, connect
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)

from collections.abc import Callable, Awaitable

from lumi.base.message_queue import BasicStreamClient
from lumi.rheed.communication import LiveVideoFragmentsMessageQueueClient, CameraMessageQueueClient, VideoFragmentsMessageQueueClient
from lumi.detection.communication import LiveDetectionMessageQueueClient
from lumi.pascal.communication import LiveChamberLogMessageQueueClient, ChamberLogMessageQueueClient, MIModeMessageQueueClient
from lumi.storage.communication import StorageMessageQueueClient
from lumi.rheed.communication import LiveSTFTMessageQueueClient, STFTMessageQueueClient, LiveIntegratorMessageQueueClient, IntegratorMessageQueueClient
from lumi.rheed.communication import LiveCameraMessageQueueClient