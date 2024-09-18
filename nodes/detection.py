from lumi.detection.communication import (
    DetectionMessageQueueServer,
    LiveDetectionMessageQueueServer,
    CameraMessageQueueClient,
)
from lumi.detection.model import DetectorServer, DetectorConfig

import time
import datetime
import asyncio
import logging

import cv2

import aio_pika
from aio_pika import ExchangeType, connect

FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

async def main(args):
    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    rheed_exchange = await channel.declare_exchange(
        "RHEED",
        ExchangeType.DIRECT,
    )

    config = DetectorConfig(
        input_queue_size=10,
        output_queue_size=10,
        model_device=None,
        model_folder=None,
    )

    detector = DetectorServer(config=config, name="detection")
    detector.daemon = True
    detector.start()

    camera_client = CameraMessageQueueClient(
        channel=channel, 
        exchange=rheed_exchange, 
        routing_key="image",
        control_routing_key="image_ctrl",
        state_routing_key="image_state",
        client_name="image",
        time_out=10,
        on_state_callback=None,
    )

    await camera_client.start()

    detection_mq = DetectionMessageQueueServer(
        detector=detector,
        camera_client= camera_client,
        channel=channel,
        exchange=rheed_exchange,
        routing_key="detection",
        control_routing_key="detection_ctrl",
        state_routing_key="detection_state",
        server_name="detection",
    )
    live_detection_mq = LiveDetectionMessageQueueServer(
        detector=detector,
        camera_client=camera_client,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key="live_detection_ctrl",
        publish_routing_key="live_detection",
        state_routing_key="live_detection_state",
        server_name="live detection"
    )
    await detection_mq.start()
    await live_detection_mq.start()

    print("message queue started")
    await asyncio.Future()

    detector.join()
    print("program exit")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
                        prog='Detection Node',
                        description='...',
                        epilog='...')
    parser.add_argument("--host", type=str, default="localhost")
    args= parser.parse_args()

    asyncio.run(main(args))
