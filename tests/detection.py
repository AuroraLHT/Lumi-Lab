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
logging.basicConfig(level=logging.DEBUG, format=FORMAT)

async def main(args):
    # Perform connection
    config = DetectorConfig(
        input_queue_size=10,
        output_queue_size=10,
        model_device=None,
        model_folder=None,
    )

    detector = DetectorServer(config=config, name="detection")
    detector.daemon = True
    detector.start()

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
