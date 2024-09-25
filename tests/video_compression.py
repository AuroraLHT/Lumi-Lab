"""
This is to test the video compression under edge cases
"""

from lumi.rheed.video_stream import (
    VideoCompressorConfig,
    VideoCompressor,
    VideoRecorderConfig,
    VideoRecorder,
)
from lumi.rheed.test_camera import TestCameraConfig, TestCamera


import time
import datetime

import cv2

import aio_pika
from aio_pika import ExchangeType, connect
import asyncio
import logging
from queue import Queue
from pathlib import Path

from tqdm import tqdm

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

    # live_video_camera_queue = camera.register_queue("live_video")
    # live_image_camera_queue = camera.register_queue("live_image")
    # live_integration_camera_queue = camera.register_queue("live_integration")

    fake_video_queue = Queue(maxsize=2000)

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
        camera_queue=fake_video_queue,
        config=live_video_compressor_config,
        frame_processing=frame_processing,
        name="video",
    )


    camera.daemon = True
    camera.start()
    live_video_compressor.daemon = True
    live_video_compressor.start()

    # wait until the camera is ready
    while True:
        frame, header = camera.get_frame()
        if header is not None:
            break
        else:
            time.sleep(0.1)

    fps =  10
    # collect 10 frames
    n_total = 10
    for i in tqdm(range(n_total), total=n_total, desc="Collecting video frames"):
        frame, header = camera.get_frame()
        print(header["time"])
        fake_video_queue.put((frame, header))
        while not live_video_compressor.fragments.empty():
            live_video_compressor.fragments.get()        
        time.sleep(1/fps)

    print("+"*10)

    n_total = 20
    for i in tqdm(range(n_total), total=n_total, desc="Collecting video frames with time shift"):
        print("-"*10)
        frame, header = camera.get_frame()
        print(header["time"])
        print(header["time_stamp"])

        t = float(header["time"])

        # t += 2 * 60 * 60 # add 2 hours
        # t = t + 2 * 60 * 60 # add 2 hours

        # t = t + 1 * 1
        # t = t + 10 

        # t = t + 60 # work

        # t = t + 30 * 60 # work

        # t = t + 35 * 60 # work
        # pts 2102907195 dts 2102907195
        # packet duration 0 packet dts 2102839414 av_frame dts 2102907195

        # t = t + 40 * 60 # not work

        # t = t + 45 * 60 # not work


        t = t + 60 * 60 # not work


        # print(t)

        header["time"] = t
        header["time_stamp"] = str(datetime.datetime.fromtimestamp(t).isoformat())

        fake_video_queue.put((frame, header))

        while not live_video_compressor.fragments.empty():
            live_video_compressor.fragments.get()

        time.sleep(1/fps)

    # logging.info("image and video started")

    # logging.info("message queue started")
    # await asyncio.Future()
    print("waiting for camera and compressor to join")
    camera.stop()
    live_video_compressor.stop()

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
