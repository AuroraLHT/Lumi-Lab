from lumi.pascal.communication import (
    ChamberLogMessageQueueServer,
    LiveChamberLogMessageQueueServer,
)
from lumi.pascal.communication import MIModeMessageQueueServer
from lumi.pascal.log_reader import (
    LogReader,
    LogReaderConfig,
    TestLogReader,
    TestLogReaderConfig,
)
from lumi.pascal.mi_mode import MIModeServer, MIModeServerConfig, MIModeBackendSimulator

import aio_pika
from aio_pika import ExchangeType, connect, Message
import asyncio
import logging
from pathlib import Path
from lumi.config import settings

FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)


async def main(args):

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_pascal = await channel.declare_exchange(
        settings.pascal.exchange,
        ExchangeType(settings.pascal.exchange_type),
    )

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

    log_reader.start()

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

    chamber_mq = ChamberLogMessageQueueServer(
        log_reader=log_reader,
        channel=channel,
        exchange=exchange_pascal,
        routing_key=settings.pascal.mq.chamber_log.request,
        control_routing_key=settings.pascal.mq.chamber_log.ctrl,
        state_routing_key=settings.pascal.mq.chamber_log.state,
        server_name=settings.pascal.mq.chamber_log.name,
    )

    live_chamber_mq = LiveChamberLogMessageQueueServer(
        log_reader=log_reader,
        log_queue=log_reader.queue,
        channel=channel,
        exchange=exchange_pascal,
        control_routing_key=settings.pascal.mq.live_chamber_log.ctrl,
        publish_routing_key=settings.pascal.mq.live_chamber_log.publish,
        state_routing_key=settings.pascal.mq.live_chamber_log.state,
        server_name=settings.pascal.mq.live_chamber_log.name,
    )

    mi_mode_mq = MIModeMessageQueueServer(
        mi_mode_server=mi_mode_server,
        channel=channel,
        exchange=exchange_pascal,
        request_routing_key=settings.pascal.mq.mi_mode.request,
        response_routing_key=settings.pascal.mq.mi_mode.response,
        update_routing_key=settings.pascal.mq.mi_mode.update,
        control_routing_key=settings.pascal.mq.mi_mode.ctrl,
        state_routing_key=settings.pascal.mq.mi_mode.state,
        server_name=settings.pascal.mq.mi_mode.name,
    )

    await chamber_mq.start()
    await live_chamber_mq.start()
    await mi_mode_mq.start()

    await asyncio.Future()

    await chamber_mq.cancel()
    await live_chamber_mq.cancel()
    await mi_mode_mq.cancel()

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

    args = parser.parse_args()

    asyncio.run(main(args))
