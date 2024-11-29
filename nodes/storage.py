import asyncio

import aio_pika
from aio_pika import ExchangeType, connect
from pathlib import Path

from lumi.storage.communication import (
    StorageMessageQueueServer,
    StorageMessageQueueServerConfig,
)
from lumi.rheed.communication import (
    LiveCameraMessageQueueClient,
    CameraMessageQueueClient,
)
from lumi.detection.communication import (
    DetectionMessageQueueClient,
    LiveDetectionMessageQueueClient,
)
from lumi.pascal.communication import (
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
)

import logging

from lumi.config import settings
from lumi.path import PROJECT_ROOT

FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)


async def main(args):
    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_rheed = await channel.declare_exchange(
        settings.rheed.exchange, type=ExchangeType(settings.rheed.exchange_type)
    )
    exchange_pascal = await channel.declare_exchange(
        settings.pascal.exchange, type=ExchangeType(settings.pascal.exchange_type)
    )
    exchange_storage = await channel.declare_exchange(
        settings.storage.exchange, type=ExchangeType(settings.storage.exchange_type)
    )

    # image client is for all user that connect to this api node
    camera_client = CameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key=settings.rheed.mq.image.request,
        control_routing_key=settings.rheed.mq.image.ctrl,
        state_routing_key=settings.rheed.mq.image.state,
        client_name=settings.rheed.mq.image.name,
        time_out=10,
        on_state_callback=None,
    )
    await camera_client.start()

    log_client = ChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_pascal,
        routing_key=settings.pascal.mq.chamber_log.request,
        control_routing_key=settings.pascal.mq.chamber_log.ctrl,
        state_routing_key=settings.pascal.mq.chamber_log.state,
        client_name=settings.pascal.mq.chamber_log.name,
        time_out=10,
        on_state_callback=None,
    )
    await log_client.start()

    detector_client = DetectionMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key=settings.detection.mq.detection.request,
        control_routing_key=settings.detection.mq.detection.ctrl,
        state_routing_key=settings.detection.mq.detection.state,
        client_name=settings.detection.mq.detection.name,
        time_out=10,
        on_state_callback=None,
    )
    await detector_client.start()

    live_camera_client = LiveCameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key=settings.rheed.mq.live_image.publish,
        control_routing_key=settings.rheed.mq.live_image.ctrl,
        state_routing_key=settings.rheed.mq.live_image.state,
        on_response_callback=None,
        client_name=settings.rheed.mq.live_image.name,
        time_out=10,
        on_state_callback=None,
    )

    live_detection_client = LiveDetectionMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key=settings.detection.mq.live_detection.publish,
        control_routing_key=settings.detection.mq.live_detection.ctrl,
        state_routing_key=settings.detection.mq.live_detection.state,
        on_response_callback=None,
        client_name=settings.detection.mq.live_detection.name,
        time_out=10,
        on_state_callback=None,
    )

    live_log_client = LiveChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_pascal,
        routing_key=settings.pascal.mq.live_chamber_log.publish,
        control_routing_key=settings.pascal.mq.live_chamber_log.ctrl,
        state_routing_key=settings.pascal.mq.live_chamber_log.state,
        on_response_callback=None,
        client_name=settings.pascal.mq.live_chamber_log.name,
        time_out=10,
        on_state_callback=None,
    )

    root_folder = (
        PROJECT_ROOT / settings.storage.hdf5_recorder.database_path
        if Path(settings.storage.hdf5_recorder.database_path).is_absolute()
        else settings.storage.hdf5_recorder.database_path
    )
    storage_config = StorageMessageQueueServerConfig(
        root_folder=root_folder,
        initial_size=settings.storage.hdf5_recorder.initial_size,
    )

    storage_mq = StorageMessageQueueServer(
        camera_client=camera_client,
        log_client=log_client,
        detector_client=detector_client,
        live_camera_client=live_camera_client,
        live_detection_client=live_detection_client,
        live_log_client=live_log_client,
        config=storage_config,
        channel=channel,
        exchange=exchange_storage,
        control_routing_key=settings.storage.mq.storage.ctrl,
        routing_key=settings.storage.mq.storage.request,
        state_routing_key=settings.storage.mq.storage.state,
        server_name=settings.storage.mq.storage.name,
    )

    await storage_mq.start()
    logging.info("storage node started")
    await asyncio.Future()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="Storage Node", description="...", epilog="..."
    )
    parser.add_argument("--host", type=str, default=settings.rabbitmq.host)
    args = parser.parse_args()

    asyncio.run(main(args))
