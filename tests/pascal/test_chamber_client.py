from lumi.client.pascal import ChamberLogClient
import aio_pika
from aio_pika import ExchangeType, connect, Message
import asyncio
import logging
from pathlib import Path
from lumi.config import settings
import lumi.pascal.command as pcmd
import numpy as np
import logging


FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

async def test_chamber_client():

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{settings.rabbitmq.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_pascal = await channel.declare_exchange(
        settings.pascal.exchange,
        ExchangeType(settings.pascal.exchange_type),
    )

    client = ChamberLogClient.from_config(
        config=settings.pascal.mq.chamber_log,
        channel=channel,
        exchange=exchange_pascal,
        time_out=10.0,
        name_suffix="",
    )
    logging.info(type(client))

    await client.start()

    logging.info("Getting state")
    state = await client.get_state()
    logging.info(f"State: {state}")
    await asyncio.sleep(1)
    logging.info("Getting state done")

    log = await client.get_log()
    logging.info(f"Log: {log}")

    logging.info("Stopping client")
    await client.stop()

    await asyncio.sleep(1)
    logging.info("Closing channel")
    await channel.close()
    logging.info("Closed channel")

    # get the RuntimeError: Event loop is closed if not closing the connection
    logging.info("Closing connection")
    await connection.close()
    logging.info("Closed connection")
    logging.info("Done")



if __name__ == "__main__":
    asyncio.run(test_chamber_client())