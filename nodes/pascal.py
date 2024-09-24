from lumi.pascal.communication import ChamberLogMessageQueueServer, LiveChamberLogMessageQueueServer
from lumi.pascal.log_reader import LogReader, LogReaderConfig, TestLogReader, TestLogReaderConfig

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

    await chamber_mq.start()
    await live_chamber_mq.start()

    await asyncio.Future()

    await chamber_mq.cancel()
    await live_chamber_mq.cancel()

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

    parser.add_argument("-p", "--path", type=str, default="chamber_log")

    args= parser.parse_args()

    asyncio.run(main(args))
