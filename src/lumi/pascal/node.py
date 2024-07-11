from .communication import ChamberLogMessageQueue, LiveChamberLogMessageQueue
from .log_reader import LogReader, LogReaderConfig

import aio_pika
from aio_pika import ExchangeType, connect, Message
import asyncio
import logging

FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

if __name__ == "__main__":

    pass

async def main(args):

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{args.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_chamber = await channel.declare_exchange(
        "chamber",
        ExchangeType.DIRECT,
    )

    log_config = LogReaderConfig(queue_size=10, idle_time=0.01, log_path=args.path)

    log_reader = LogReader(config=log_config, name="log_reader", daemon=True)

    log_reader.start()

    chamber_mq = ChamberLogMessageQueue(
        log_reader=log_reader, 
        channel=channel, 
        exchange=exchange_chamber, 
        routing_key="log"
    )

    live_chamber_mq = LiveChamberLogMessageQueue(
        log_reader=log_reader, 
        log_queue=log_reader.queue, 
        channel=channel, 
        exchange=exchange_chamber, 
        routing_key=None,
        control_routing_key="live_log_control",
        publish_routing_key="live_log"
    )

    await chamber_mq.start()
    await live_chamber_mq.start()

    # await asyncio.sleep(3)
    # print("send test messsage")
    # await exchange_chamber.publish(message=Message(b''), routing_key="log")

    await asyncio.Future()

    await chamber_mq.cancel()
    await live_chamber_mq.cancel()

    log_reader.stop()
    log_reader.join()
    # await asyncio.sleep(100000)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
                        prog='Detection Node',
                        description='...',
                        epilog='...')
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("-p", "--path", type=str, default="")

    args= parser.parse_args()

    asyncio.run(main(args))
