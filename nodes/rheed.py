from lumi.path import PROJECT_ROOT
from lumi.rheed.video_stream import (
    VideoCompressorConfig,
    VideoCompressor,
    VideoRecorderConfig,
    VideoRecorder,
)
from lumi.rheed.pylon_camera import PylonCamera, PylonCameraConfig, list_devices
from lumi.rheed.web_camera import WebCamera, WebCameraConfig, list_devices as webcam_list_devices
from lumi.rheed.test_camera import TestCameraConfig, TestCamera

from lumi.rheed.integrator import MultiBoxIntegratorConfig, MultiBoxIntegrator
from lumi.rheed.livefft import STFTCalculator, STFTCalculatorConfig

from lumi.rheed.communication import (
    LiveVideoFragmentsMessageQueueServer,
    VideoFragmentsMessageQueueServer,
    CameraMessageQueueServer,
    LiveCameraMessageQueueServer,
    IntegratorMessageQueueServer,
    LiveIntegratorMessageQueueServer,
    STFTMessageQueueServer,
    LiveSTFTMessageQueueServer,
)
import time
import datetime

import cv2

import aio_pika
from aio_pika import ExchangeType, connect
import asyncio
import logging

from pathlib import Path
from lumi.config import settings

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
    if args.src == "pylon":
        frame_processing = frame_processing_pylon  
        height = settings.rheed.pylon.height
        width = settings.rheed.pylon.width
        devices = list_devices()
        if len(devices) == 0:
            raise ValueError("No pylon camera found, check network or physical connection")
        
        pylon_camera_config = PylonCameraConfig(
            device=devices[0],
            fps=settings.rheed.pylon.fps,
            frame_dims=(height, width),
        )
        camera = PylonCamera(config=pylon_camera_config, name=settings.rheed.pylon.name)

    elif args.src == "webcam":
        frame_processing = frame_processing_webcam
        height = settings.rheed.webcam.height
        width = settings.rheed.webcam.width
        web_camera_config = WebCameraConfig(
            fps=settings.rheed.webcam.fps,  # the maximum is 30 for this webcam
            queue_size=60,
        )
        camera = WebCamera(config=web_camera_config, name=settings.rheed.webcam.name)

    else:
        frame_processing = frame_processing_testcam
        height = settings.rheed.testcam.height
        width = settings.rheed.testcam.width
        source = settings.rheed.testcam.source if Path(settings.rheed.testcam.source).is_absolute() else PROJECT_ROOT / settings.rheed.testcam.source
        test_camera_config = TestCameraConfig(
            frame_dims=(height, width),
            source= source,
            fps = settings.rheed.testcam.fps,
            queue_size=2,
        )
        camera = TestCamera(config=test_camera_config, name=settings.rheed.testcam.name)

    # record_camera_queue = camera.register_queue("record")
    live_video_camera_queue = camera.register_queue(settings.rheed.video_compressor.name)
    live_image_camera_queue = camera.register_queue(settings.rheed.mq.live_camera.name)
    live_integration_camera_queue = camera.register_queue(settings.rheed.mq.live_integrator.name)

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    rheed_exchange = await channel.declare_exchange(
        settings.rheed.exchange,
        ExchangeType(settings.rheed.exchange_type),
    )

    # video_compressor = VideoCompressor(camera_queue=pylon_camera.queue, config=video_compressor_config, frame_processing=lambda cv_frame: cv2.cvtColor(cv_frame, cv2.COLOR_GRAY2RGB))

    live_video_compressor_config = VideoCompressorConfig(
        fps=settings.rheed.video_compressor.fps,  # the minimum is 30 fps
        frames_per_keyframe=settings.rheed.video_compressor.frames_per_keyframe,
        fragment_queue_size=settings.rheed.video_compressor.fragment_queue_size,
        cached_startup_fragments=settings.rheed.video_compressor.cached_startup_fragments,
        bit_rate=settings.rheed.video_compressor.bit_rate,
        height=height,
        width=width,
    )

    live_video_compressor = VideoCompressor(
        camera=camera,
        camera_queue=live_video_camera_queue,
        config=live_video_compressor_config,
        frame_processing=frame_processing,
        name=settings.rheed.video_compressor.name,
    )

    video_recorder_config = VideoRecorderConfig(
        fps=settings.rheed.video_recorder.fps,  # the minimum is 30 fps
        frames_per_keyframe=settings.rheed.video_recorder.frames_per_keyframe,
        bit_rate=settings.rheed.video_recorder.bit_rate,
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


    stft_config = STFTCalculatorConfig(
        idle_time=settings.rheed.stft_calculator.idle_time,
        output_queue_size=settings.rheed.stft_calculator.output_queue_size,
        window_size=settings.rheed.stft_calculator.window_size,
        hop_size=settings.rheed.stft_calculator.hop_size,
        time_resolution= settings.rheed.stft_calculator.time_resolution
    )

    integrator_config = MultiBoxIntegratorConfig(
        idle_time=settings.rheed.integrator.idle_time,
        output_queue_size=settings.rheed.integrator.output_queue_size,
    )

    integrator = MultiBoxIntegrator(camera=None, camera_queue=live_integration_camera_queue, config=integrator_config)
    stft_calculator = STFTCalculator(integrator=integrator, config=stft_config)

    live_video_mq = LiveVideoFragmentsMessageQueueServer(
        video_compressor=live_video_compressor,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key=settings.rheed.mq.live_video.ctrl_key,
        publish_routing_key=settings.rheed.mq.live_video.publish_key,
        state_routing_key=settings.rheed.mq.live_video.state_key,
        server_name=settings.rheed.mq.live_video.name
    )
    # publish to callback queue, not need publish routing key
    video_mq = VideoFragmentsMessageQueueServer(
        video_compressor=live_video_compressor,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key=settings.rheed.mq.video.ctrl_key,
        request_routing_key=settings.rheed.mq.video.request_key,
        state_routing_key=settings.rheed.mq.video.state_key,
        server_name=settings.rheed.mq.video.name
    )
    # publish to callback queue, not need publish routing key
    image_mq = CameraMessageQueueServer(
        camera=camera, 
        channel=channel, 
        exchange=rheed_exchange, 
        request_routing_key=settings.rheed.mq.camera.request_key,
        control_routing_key=settings.rheed.mq.camera.ctrl_key,
        state_routing_key=settings.rheed.mq.camera.state_key,
        server_name=settings.rheed.mq.camera.name
    )

    live_image_mq = LiveCameraMessageQueueServer(
        camera=camera,
        camera_queue=live_image_camera_queue,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key=settings.rheed.mq.live_camera.ctrl_key,
        publish_routing_key=settings.rheed.mq.live_camera.publish_key,
        state_routing_key=settings.rheed.mq.live_camera.state_key,
        server_name=settings.rheed.mq.live_camera.name
    )

    live_integrator_mq = LiveIntegratorMessageQueueServer(
        integrator=integrator,
        integrator_queue=integrator.output_queue,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key=settings.rheed.mq.live_integrator.ctrl_key,
        publish_routing_key=settings.rheed.mq.live_integrator.publish_key,
        state_routing_key=settings.rheed.mq.live_integrator.state_key,
        server_name=settings.rheed.mq.live_integrator.name
    )

    integrator_mq = IntegratorMessageQueueServer(
        integrator=integrator,
        channel=channel,
        exchange=rheed_exchange,
        request_routing_key=settings.rheed.mq.integrator.request_key,
        control_routing_key=settings.rheed.mq.integrator.ctrl_key,
        state_routing_key=settings.rheed.mq.integrator.state_key,
        server_name=settings.rheed.mq.integrator.name
    )

    live_stft_mq = LiveSTFTMessageQueueServer(
        stft_calculator=stft_calculator,
        stft_calculator_queue=stft_calculator.output_queue,
        channel=channel,
        exchange=rheed_exchange,
        control_routing_key=settings.rheed.mq.live_stft.ctrl_key,
        publish_routing_key=settings.rheed.mq.live_stft.publish_key,
        state_routing_key=settings.rheed.mq.live_stft.state_key,
        server_name=settings.rheed.mq.live_stft.name
    )

    stft_mq = STFTMessageQueueServer(
        stft_calculator=stft_calculator,
        channel=channel,
        exchange=rheed_exchange,
        request_routing_key=settings.rheed.mq.stft.request_key,
        control_routing_key=settings.rheed.mq.stft.ctrl_key,
        state_routing_key=settings.rheed.mq.stft.state_key,
        server_name=settings.rheed.mq.stft.name
    )

    camera.daemon = True
    camera.start()

    live_video_compressor.daemon = True
    live_video_compressor.start()

    integrator.daemon = True
    integrator.start()

    stft_calculator.daemon = True
    stft_calculator.start()

    logging.info("image and video started")
    await image_mq.start()
    await live_image_mq.start()
    await live_video_mq.start()
    await video_mq.start()

    await live_integrator_mq.start()
    await integrator_mq.start()
    await live_stft_mq.start()
    await stft_mq.start()

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
        prog="RHEED Node", description="...", epilog="..."
    )
    parser.add_argument("--host", type=str, default=settings.rabbitmq.host)
    parser.add_argument("--src", type=str, default="pylon", help="Could be [pylon, webcam, or test]")    
    args = parser.parse_args()

    asyncio.run(_main(args))
