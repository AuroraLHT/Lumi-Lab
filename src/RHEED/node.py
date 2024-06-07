from video_stream import VideoCompressorConfig, VideoCompressor
from pylon_camera import PylonCamera, PylonCameraConfig, list_devices
from web_camera import WebCamera, WebCameraConfig, list_devices as webcam_list_devices
from communitation import (
    VideoMessageQueue,
    VideoFragmentsMessageQueue,
    ImageMessageQueue,
    LiveImageMessageQueue
)
import time
import datetime

import cv2

import aio_pika
from aio_pika import ExchangeType, connect
import asyncio
import logging

logging.basicConfig(level=logging.INFO)


def frame_processing(frame, timestamp=None):
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    # Put current DateTime on each frame
    if timestamp is not None:
        font = cv2.FONT_HERSHEY_PLAIN
        frame = cv2.putText(
            frame,
            str(datetime.datetime.fromtimestamp(timestamp)),
            (20, 40),
            font,
            2,
            (255, 0, 0),
            2,
            cv2.LINE_AA,
        )
    return frame


async def _main():
    # Perform connection
    connection = await connect("amqp://guest:guest@localhost/")

    # Creating a channel
    channel = await connection.channel()

    rheed_exchange = await channel.declare_exchange(
        "RHEED",
        ExchangeType.DIRECT,
    )

    video_compressor_config = VideoCompressorConfig(
        fps=60,  # the minimum is 30 fps
        frames_per_keyframe=2,
        fragment_queue_size=60,
        cached_startup_fragments=5,
        bit_rate=6_000_000,
    )
    # devices = list_devices()
    # pylon_camera_config = PylonCameraConfig(device = devices[0] )
    web_camera_config = WebCameraConfig(
        fps=30,  # the maximum is 30 for this webcam
        queue_size=60,
    )

    # pylon_camera = PylonCamera(device=devices[0], config=pylon_camera_config, ident=0)
    # video_compressor = VideoCompressor(camera_queue=pylon_camera.queue, config=video_compressor_config, frame_processing=lambda cv_frame: cv2.cvtColor(cv_frame, cv2.COLOR_GRAY2RGB))

    web_camera = WebCamera(config=web_camera_config, name="web_cam")

    video_compressor = VideoCompressor(
        camera=web_camera,
        camera_queue=web_camera.queue,
        config=video_compressor_config,
        frame_processing=frame_processing,
        name="video",
    )

    video_mq = VideoMessageQueue(
        video_compressor=video_compressor,
        channel=channel,
        exchange=rheed_exchange,
        routing_key=None, # Not need a input routing key yet. 
        publish_routing_key="video"
    )
    # publish to callback queue, not need publish routing key
    video_history_mq = VideoFragmentsMessageQueue(
        video_compressor=video_compressor,
        channel=channel,
        exchange=rheed_exchange,
        routing_key="video_history",
    )
    # publish to callback queue, not need publish routing key    
    image_mq = ImageMessageQueue(
        camera=web_camera, 
        channel=channel, 
        exchange=rheed_exchange, 
        routing_key="image"
    )

    # live_image_mq = LiveImageMessageQueue(
    #     camera=web_camera,
    #     channel=channel,
    #     exchange=rheed_exchange,
    #     routing_key="",
    #     control_routing_key="live_image_control",
    #     publish_routing_key="live_image",
    # )

    web_camera.daemon = True
    web_camera.start()

    video_compressor.daemon = True
    video_compressor.start()

    print("image and video started")
    await image_mq.start()
    # await live_image_mq.start()
    await video_mq.start()
    await video_history_mq.start()

    print("message queue started")
    await asyncio.Future()
    # await asyncio.gather(image_mq_task, video_mq_task, video_history_mq_task)

    # while True:
    #     try:
    #         time.sleep(1)
    #     except Exception as e:
    #         print(e)
    #         web_camera.stop()
    #         video_compressor.stop()
    #         image_mq_task.cancel()
    #         video_mq_task.cancel()
    #         video_history_mq_task.cancel()
    #         break

    # wait for signal to terminal all threads
    web_camera.join()
    video_compressor.join()


if __name__ == "__main__":
    asyncio.run(_main())
