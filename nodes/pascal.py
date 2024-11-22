from lumi.pascal.communication import ChamberLogMessageQueueServer, LiveChamberLogMessageQueueServer
from lumi.pascal.communication import MIModeMessageQueueServer
from lumi.pascal.log_reader import LogReader, LogReaderConfig, TestLogReader, TestLogReaderConfig
from lumi.pascal.mi_mode import MIModeServer, MIModeServerConfig, MIModeBackendSimulator

import aio_pika
from aio_pika import ExchangeType, connect, Message
import asyncio
import logging
from pathlib import Path

FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

async def main(args):

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_chamber = await channel.declare_exchange(
        "chamber",
        ExchangeType.DIRECT,
    )

    if args.src == "path":
        assert Path(args.path).exists(), f"Log path: {args.path} ({Path(args.path).absolute()}) does not exist"
        log_config = LogReaderConfig(queue_size=10, idle_time=0.01, log_path=args.path)
        log_reader = LogReader(config=log_config, name="log_reader", daemon=True)
    else:
        log_config = TestLogReaderConfig(queue_size=10, idle_time=0.01, publish_interval=1, log_path=Path(__file__).parent.parent / "src/lumi/pascal/assets/chamber_log_test.csv" )
        log_reader = TestLogReader(config=log_config, name="test_log_reader", daemon=True)

    log_reader.start()

    if args.src == "path":
        mi_config = MIModeServerConfig(queue_size=1000, idle_time=0.01, mi_folder=args.mi, assist_file_name="assist.txt")
        mi_mode_server = MIModeServer(config=mi_config, name="mi_server", daemon=True)
        mi_mode_server.start()

    else:
        mi_folder = Path(__file__).parent.parent / "src/lumi/pascal/assets/mi_mode"
        logging.info(f"use MI folder for simulation: {mi_folder.absolute()}")
        mi_folder.mkdir(exist_ok=True)
        mi_config = MIModeServerConfig(queue_size=1000, idle_time=0.01, mi_folder=mi_folder, assist_file_name="assist.txt")
        mi_mode_server = MIModeServer(config=mi_config, name="mi_server", daemon=True)
        mi_mode_simulator= MIModeBackendSimulator(mi_config)
        mi_mode_server.start()
        mi_mode_simulator.start()


    chamber_mq = ChamberLogMessageQueueServer(
        log_reader=log_reader, 
        channel=channel, 
        exchange=exchange_chamber, 
        routing_key="log",
        control_routing_key="log_ctrl",
        state_routing_key="log_state",
        server_name="ChamberLog"
    )

    live_chamber_mq = LiveChamberLogMessageQueueServer(
        log_reader=log_reader, 
        log_queue=log_reader.queue, 
        channel=channel, 
        exchange=exchange_chamber, 
        control_routing_key="live_log_ctrl",
        publish_routing_key="live_log",
        state_routing_key="live_log_state",
        server_name="LiveChamberLog"
    )

    mi_mode_mq = MIModeMessageQueueServer(
        mi_mode_server=mi_mode_server,
        channel=channel,
        exchange=exchange_chamber,
        request_routing_key="mi_mode_request",
        response_routing_key="mi_mode_response",
        update_routing_key="mi_mode_update",
        control_routing_key="mi_mode_ctrl",
        state_routing_key="mi_mode_state",
        server_name="MIMode",
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
                        prog='Detection Node',
                        description='...',
                        epilog='...')
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--src", type=str, default="path", help="Options [path | test]")
    parser.add_argument("-m","--mi", type=str, default="mi_mode", help="mi mode folder")
    parser.add_argument("-l", "--log", type=str, default="chamber_log", help="chamber log path")

    args= parser.parse_args()

    asyncio.run(main(args))
