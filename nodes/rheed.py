from lumi.rheed.video_stream import (
    VideoCompressorConfig,
    VideoCompressor,
    VideoRecorderConfig,
    VideoRecorder,
)
from lumi.rheed.pylon_camera import PylonCamera, PylonCameraConfig, list_devices
from lumi.rheed.web_camera import WebCamera, WebCameraConfig, list_devices as webcam_list_devices
from lumi.rheed.test_camera import TestCameraConfig, TestCamera

from lumi.rheed.communitation import (
    LiveVideoFragmentsMessageQueueServer,
    VideoFragmentsMessageQueueServer,
    CameraMessageQueueServer,
    LiveCameraMessageQueueServer,
)
import time
import datetime

import cv2

import aio_pika
from aio_pika import ExchangeType, connect
import asyncio
import logging

from pathlib import Path

FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)


def add_time_stamp(frame, frame_header=None):
    if frame_header is not None:
        timestamp = frame_header["time_stamp"] if "time_stamp" in frame_header else None
        if timestamp is not None:
            font = cv2.FONT_HERSHEY_PLAIN
            frame = cv2.putText(
                frame,
                timestamp,
                (20, 40),
                font,
                2,
                (255, 0, 0),
                2,
                cv2.LINE_AA,
            )
    return frame

def frame_processing_testcam(frame, frame_header=None):
    # frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    frame = cv2.convertScaleAbs(frame, alpha=(255.0 / 4095.0))
    # print(frame.max())
    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)

    frame = add_time_stamp(frame, frame_header)
    return frame


def frame_processing_webcam(frame, frame_header=None):
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    frame = add_time_stamp(frame, frame_header)
    return frame


def frame_processing_pylon(frame, frame_header=None):
    # this scale the uint16 image
    # print(frame.max())
    frame = cv2.convertScaleAbs(frame, alpha=(255.0 / 4095.0))
    # print(frame.max())
    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
    # print(frame.dtype, frame.shape)

    # Put current DateTime on each frame
    frame = add_time_stamp(frame, frame_header)
    return frame




async def _main(args):
    # TODO turn it into a argument
    if args.src == "pylon":
        frame_processing = frame_processing_pylon  
        height = 540
        width = 720
        devices = list_devices()
        pylon_camera_config = PylonCameraConfig(
            device=devices[0],
            fps=30,
            frame_dims=(height, width),
        )
        camera = PylonCamera(config=pylon_camera_config, name="pylon_cam")

    elif args.src == "webcam":
        frame_processing = frame_processing_webcam
        height = 480
        width = 640
        web_camera_config = WebCameraConfig(
            fps=30,  # the maximum is 30 for this webcam
            queue_size=60,
        )
        camera = WebCamera(config=web_camera_config, name="web_cam")

    else:
        frame_processing = frame_processing_testcam
        height = 540
        width = 720
        test_camera_config = TestCameraConfig(
            frame_dims=(height, width),
            source= Path(__file__).parent.parent / "src/lumi/rheed/assets/test_frame.npy",
            fps=30,
            queue_size=2,
        )
        camera = TestCamera(config=test_camera_config, name="test_cam")

    # record_camera_queue = camera.register_queue("record")
    live_video_camera_queue = camera.register_queue("live_video")
    live_image_camera_queue = camera.register_queue("live_image")

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    rheed_exchange = await channel.declare_exchange(
        "RHEED",
        ExchangeType.DIRECT,
    )

    # video_compressor = VideoCompressor(camera_queue=pylon_camera.queue, config=video_compressor_config, frame_processing=lambda cv_frame: cv2.cvtColor(cv_frame, cv2.COLOR_GRAY2RGB))

    live_video_compressor_config = VideoCompressorConfig(
        fps=60,  # the minimum is 30 fps
        frames_per_keyframe=1,
        fragment_queue_size=60,
        cached_startup_fragments=5,
        bit_rate=12_000_000,
        height=height,
        width=width,
    )

    live_video_compressor = VideoCompressor(
        camera=camera,
        camera_queue=live_video_camera_queue,
        config=live_video_compressor_config,
        frame_processing=frame_processing,
        name="video",
    )

    video_recorder_config = VideoRecorderConfig(
        fps=60,  # the minimum is 30 fps
        frames_per_keyframe=10,
        bit_rate=12_000_000,
        height=height,
        width=width,
    )

    # video_recorder = VideoRecorder(
    #     camera=camera,
    #     camera_queue=record_camera_queue,
    #     config=video_recorder_config,
    #     frame_processing=frame_processing,
    #     name="video_record",
    # )

    live_video_mq = LiveVideoFragmentsMessageQueueServer(
        video_compressor=live_video_compressor,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key="live_video_ctrl",
        publish_routing_key="live_video",
        server_name="live_video"
    )
    # publish to callback queue, not need publish routing key
    live_video_history_mq = VideoFragmentsMessageQueueServer(
        video_compressor=live_video_compressor,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key="live_video_history_ctrl",
        routing_key="live_video_history",
        server_name="live_video_history"
    )
    # publish to callback queue, not need publish routing key
    image_mq = CameraMessageQueueServer(
        camera=camera, 
        channel=channel, 
        exchange=rheed_exchange, 
        routing_key="image",
        control_routing_key="image_ctrl",
        server_name="image"
    )

    live_image_mq = LiveCameraMessageQueueServer(
        camera=camera,
        camera_queue=live_image_camera_queue,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key="live_image_ctrl",
        publish_routing_key="live_image",
        server_name="live_image"
    )

    camera.daemon = True
    camera.start()

    live_video_compressor.daemon = True
    live_video_compressor.start()

    logging.info("image and video started")
    await image_mq.start()
    await live_image_mq.start()
    await live_video_mq.start()
    await live_video_history_mq.start()

    logging.info("message queue started")
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
    camera.join()
    live_video_compressor.join()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="Detection Node", description="...", epilog="..."
    )
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--src", type=str, default="pylon", help="Could be [pylon, webcam, or test]")    
    args = parser.parse_args()

    asyncio.run(_main(args))
