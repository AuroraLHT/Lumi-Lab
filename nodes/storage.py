import asyncio

import aio_pika
from aio_pika import ExchangeType, connect
from pathlib import Path

from lumi.storage.communication import (
    StorageMessageQueueServer,
    StorageMessageQueueServerConfig,
)
from lumi.rheed.communitation import (
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

FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)


async def main(args):
    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_rheed = await channel.declare_exchange("RHEED", type=ExchangeType.DIRECT)
    exchange_chamber = await channel.declare_exchange(
        "chamber", type=ExchangeType.DIRECT
    )
    exchange_storage = await channel.declare_exchange(
        "storage", type=ExchangeType.DIRECT
    )

    # image client is for all user that connect to this api node
    camera_client = CameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="image",
        control_routing_key="image_ctrl",
        state_routing_key="image_state",
        client_name="Camera",
        time_out=10,
        on_state_callback=None,
    )
    await camera_client.start()

    log_client = ChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_chamber,
        routing_key="log",
        control_routing_key="log_ctrl",
        state_routing_key="log_state",
        client_name="Chamber Log",
        time_out=10,
        on_state_callback=None,
    )
    await log_client.start()

    detector_client = DetectionMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="detection",
        control_routing_key="detection_ctrl",
        state_routing_key="detection_state",
        client_name="Detection",
        time_out=10,
        on_state_callback=None,
    )
    await detector_client.start()

    live_camera_client = LiveCameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="live_image",
        control_routing_key="live_image_ctrl",
        state_routing_key="live_image_state",
        on_response_callback=None,
        client_name="Live Image",
        time_out=10,
        on_state_callback=None,
    )

    live_detection_client = LiveDetectionMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="live_detection",
        control_routing_key="live_detection_ctrl",
        state_routing_key="live_detection_state",
        on_response_callback=None,
        client_name="Live Detection",
        time_out=10,
        on_state_callback=None,
    )

    live_log_client = LiveChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_chamber,
        routing_key="live_log",
        control_routing_key="live_log_ctrl",
        state_routing_key="live_log_state",
        on_response_callback=None,
        client_name="Live Log",
        time_out=10,
        on_state_callback=None,
    )

    storage_config = StorageMessageQueueServerConfig(
        root_folder= Path(__file__).parent.parent.absolute() / ("database"),
        initial_size=1000,
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
        control_routing_key="storage_ctrl",
        routing_key="storage",
        state_routing_key="storage_state",
        server_name="storage",
    )

    await storage_mq.start()
    logging.info("storage node started")
    await asyncio.Future()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="Storage Node", description="...", epilog="..."
    )
    parser.add_argument("--host", type=str, default="localhost")
    args = parser.parse_args()

    asyncio.run(main(args))
