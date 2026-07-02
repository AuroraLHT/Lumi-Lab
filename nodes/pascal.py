from lumi.path import PROJECT_ROOT

from lumi.pascal.communication import (
    ChamberLogMessageQueueServer,
    LiveChamberLogMessageQueueServer,
)
from lumi.pascal.communication import MIModeMessageQueueServer
from lumi.pascal.communication import ChamberConfigMessageQueueServer
from lumi.pascal.communication import CameraMessageQueueServer, LiveCameraMessageQueueServer
from lumi.pascal.log_reader import (
    LogReader,
    LogReaderConfig,
    TestLogReader,
    TestLogReaderConfig,
)
from lumi.pascal.mi_mode import MIModeServer, MIModeServerConfig, MIModeBackendSimulator
from lumi.pascal.config_reader import ConfigReader, ConfigReaderConfig

import aio_pika
from aio_pika import ExchangeType, connect, Message
import asyncio
import logging
from pathlib import Path
from lumi.config import settings

# from lumi.base.camera.video_stream import (
#     VideoCompressorConfig,
#     VideoCompressor,
#     VideoRecorderConfig,
#     VideoRecorder,
# )
from lumi.base.camera.display import DisplayServer, DisplayConfig
from lumi.base.camera.web_camera import WebCamera, WebCameraConfig, list_devices as webcam_list_devices
from lumi.base.camera.sim_camera import SimCamera, SimCameraConfig


FORMAT = "%(asctime)s %(levelname)s:%(message)s"


async def main(args):
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format=FORMAT)
    else:
        logging.basicConfig(level=logging.INFO, format=FORMAT)

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_pascal = await channel.declare_exchange(
        settings.pascal.exchange,
        ExchangeType(settings.pascal.exchange_type),
    )

    # Log reader
    if args.src == "path":
        assert Path(
            args.log
        ).exists(), (
            f"Log path: {args.log} ({Path(args.log).absolute()}) does not exist"
        )
        log_config = LogReaderConfig(
            queue_size=settings.pascal.log_reader.queue_size,
            idle_time=settings.pascal.log_reader.idle_time,
            log_path=args.log,
        )
        log_reader = LogReader(
            config=log_config, name=settings.pascal.log_reader.name, daemon=True
        )
    else:
        log_config = TestLogReaderConfig(
            queue_size=settings.pascal.log_reader.test.queue_size,
            idle_time=settings.pascal.log_reader.test.idle_time,
            publish_interval=settings.pascal.log_reader.test.publish_interval,
            log_path=Path(__file__).parent.parent
            / "src/lumi/pascal/assets"
            / settings.pascal.log_reader.test.log_file,
        )
        log_reader = TestLogReader(
            config=log_config, name="test_log_reader", daemon=True
        )

    if args.src == "test":
        config_reader_config = ConfigReaderConfig(
            idle_time=settings.pascal.config_reader.test.idle_time,
            config_path=settings.pascal.config_reader.test.config_path,
        )
        config_reader = ConfigReader(
            config=config_reader_config, name=settings.pascal.config_reader.test.name, daemon=True
        )
    else:
        config_reader_config = ConfigReaderConfig(
            idle_time=settings.pascal.config_reader.idle_time,
            config_path=settings.pascal.config_reader.config_path,
        )
        config_reader = ConfigReader(
            config=config_reader_config, name=settings.pascal.config_reader.name, daemon=True
        )


    log_reader.start()
    config_reader.start()

    # MI mode server
    if args.src == "path":
        mi_config = MIModeServerConfig(
            queue_size=settings.pascal.mi_mode_server.queue_size,
            idle_time=settings.pascal.mi_mode_server.idle_time,
            mi_folder=args.mi,
            assist_file_name=settings.pascal.mi_mode_server.assist_file_name,
        )
        mi_mode_server = MIModeServer(
            config=mi_config, name=settings.pascal.mi_mode_server.name, daemon=True
        )
        mi_mode_server.start()

    else:
        mi_folder = Path(__file__).parent.parent / "src/lumi/pascal/assets/mi_mode"
        logging.info(f"use MI folder for simulation: {mi_folder.absolute()}")
        mi_folder.mkdir(exist_ok=True)
        mi_config = MIModeServerConfig(
            queue_size=settings.pascal.mi_mode_server.queue_size,
            idle_time=settings.pascal.mi_mode_server.idle_time,
            mi_folder=mi_folder,
            assist_file_name=settings.pascal.mi_mode_server.assist_file_name,
        )
        mi_mode_server = MIModeServer(
            config=mi_config, name=settings.pascal.mi_mode_server.name, daemon=True
        )
        mi_mode_simulator = MIModeBackendSimulator(mi_config)
        mi_mode_server.start()
        mi_mode_simulator.start()

    if args.src == "path":
        height = settings.pascal.webcam.height
        width = settings.pascal.webcam.width
        web_camera_config = WebCameraConfig(            
            fps = settings.pascal.webcam.fps,  # the maximum is 30 for this webcam
            queue_size = settings.pascal.webcam.queue_size,
            idle_time = settings.pascal.webcam.idle_time,
            frame_dims = (height, width),
            device=settings.pascal.webcam.device
        )
        camera = WebCamera(config=web_camera_config, name=settings.pascal.webcam.name, daemon=True)

    else:
        height = settings.pascal.simcam.height
        width = settings.pascal.simcam.width
        source = settings.pascal.simcam.source if Path(settings.pascal.simcam.source).is_absolute() else PROJECT_ROOT / settings.pascal.simcam.source
        sim_camera_config = SimCameraConfig(
            frame_dims = (height, width),
            source = source,
            fps = settings.pascal.simcam.fps,
            queue_size = settings.pascal.simcam.queue_size,
            idle_time = settings.pascal.simcam.idle_time,
            is_base_oscillation = settings.pascal.simcam.is_base_oscillation,
            base_oscillation_frequency = settings.pascal.simcam.base_oscillation_frequency,
            base_oscillation_amplitude = settings.pascal.simcam.base_oscillation_amplitude,
            is_feature_oscillation = settings.pascal.simcam.is_feature_oscillation,
            feature_bbox = settings.pascal.simcam.feature_bbox,
            feature_oscillation_frequency = settings.pascal.simcam.feature_oscillation_frequency,
            feature_oscillation_amplitude = settings.pascal.simcam.feature_oscillation_amplitude,
            exposure_time = settings.pascal.simcam.exposure_time,
            gain = settings.pascal.simcam.gain,
            gamma = settings.pascal.simcam.gamma,
            max_intensity = settings.pascal.simcam.max_intensity
        )
        camera = SimCamera(config=sim_camera_config, name=settings.pascal.simcam.name, daemon=True)


    # live_video_camera_queue = camera.register_queue(settings.pascal.video_compressor.name)
    live_image_camera_queue = camera.register_queue(settings.pascal.mq.live_camera.name)

    if settings.pascal.display.enable:
        display_queue = camera.register_queue("display")
        display_config = DisplayConfig(title="Bottom Cam (press q to quit)", add_timestamp=settings.pascal.display.add_timestamp)
        display_server = DisplayServer(config=display_config, queue=display_queue, name="Display", daemon=True)
        display_server.start()

    camera.start()

    chamber_mq = ChamberLogMessageQueueServer(
        log_reader=log_reader,
        channel=channel,
        exchange=exchange_pascal,
        request_routing_key=settings.pascal.mq.chamber_log.request_key,
        control_routing_key=settings.pascal.mq.chamber_log.ctrl_key,
        state_routing_key=settings.pascal.mq.chamber_log.state_key,
        server_name=settings.pascal.mq.chamber_log.name,
    )

    chamber_config_mq = ChamberConfigMessageQueueServer(
        config_reader=config_reader,
        channel=channel,
        exchange=exchange_pascal,
        request_routing_key=settings.pascal.mq.chamber_config.request_key,
        control_routing_key=settings.pascal.mq.chamber_config.ctrl_key,
        state_routing_key=settings.pascal.mq.chamber_config.state_key,
        server_name=settings.pascal.mq.chamber_config.name,
    )

    live_chamber_mq = LiveChamberLogMessageQueueServer(
        log_reader=log_reader,
        log_queue=log_reader.queue,
        channel=channel,
        exchange=exchange_pascal,
        control_routing_key=settings.pascal.mq.live_chamber_log.ctrl_key,
        publish_routing_key=settings.pascal.mq.live_chamber_log.publish_key,
        state_routing_key=settings.pascal.mq.live_chamber_log.state_key,
        server_name=settings.pascal.mq.live_chamber_log.name,
    )

    mi_mode_mq = MIModeMessageQueueServer(
        mi_mode_server=mi_mode_server,
        channel=channel,
        exchange=exchange_pascal,
        request_routing_key=settings.pascal.mq.mi_mode.request_key,
        response_routing_key=settings.pascal.mq.mi_mode.response_key,
        update_routing_key=settings.pascal.mq.mi_mode.update_key,
        control_routing_key=settings.pascal.mq.mi_mode.ctrl_key,
        state_routing_key=settings.pascal.mq.mi_mode.state_key,
        server_name=settings.pascal.mq.mi_mode.name,
    )

    image_mq = CameraMessageQueueServer(
        camera=camera, 
        channel=channel, 
        exchange=exchange_pascal, 
        request_routing_key=settings.pascal.mq.camera.request_key,
        control_routing_key=settings.pascal.mq.camera.ctrl_key,
        state_routing_key=settings.pascal.mq.camera.state_key,
        server_name=settings.pascal.mq.camera.name
    )

    live_image_mq = LiveCameraMessageQueueServer(
        camera=camera,
        camera_queue=live_image_camera_queue,
        channel=channel,
        exchange=exchange_pascal,
        control_routing_key=settings.pascal.mq.live_camera.ctrl_key,
        publish_routing_key=settings.pascal.mq.live_camera.publish_key,
        state_routing_key=settings.pascal.mq.live_camera.state_key,
        server_name=settings.pascal.mq.live_camera.name
    )

    await chamber_mq.start()
    await chamber_config_mq.start()
    await live_chamber_mq.start()
    await mi_mode_mq.start()
    await image_mq.start()
    # await live_image_mq.start()

    await asyncio.Future()

    await chamber_mq.cancel()
    await chamber_config_mq.cancel()
    await live_chamber_mq.cancel()
    await mi_mode_mq.cancel()
    await image_mq.cancel()
    # await live_image_mq.cancel()

    if args.src == "path":
        mi_mode_server.stop()
        mi_mode_server.join()
    else:
        mi_mode_server.stop()
        mi_mode_simulator.stop()
        mi_mode_server.join()
        mi_mode_simulator.join()

    log_reader.stop()
    log_reader.join()
    config_reader.stop()
    config_reader.join()

    camera.join()
    if settings.pascal.display.enable:
        display_server.join()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="Pascal PLD Chamber Node", description="...", epilog="..."
    )
    parser.add_argument("--host", type=str, default=settings.rabbitmq.host)
    parser.add_argument("--src", type=str, default="path", help="Options [path | test]")
    parser.add_argument(
        "-m",
        "--mi",
        type=str,
        default=settings.pascal.mi_mode_folder,
        help="mi mode folder",
    )
    parser.add_argument(
        "-l",
        "--log",
        type=str,
        default=settings.pascal.chamber_log_folder,
        help="chamber log path",
    )

    parser.add_argument(
        "-d",
        "--debug",
        type=bool,
        default=False,
        help="Enter debug mode",
    )


    args = parser.parse_args()

    asyncio.run(main(args))
