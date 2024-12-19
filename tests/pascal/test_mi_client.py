from lumi.client.pascal import MIModeClient

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

async def test_mi_client():

    # Perform connection
    connection = await connect(f"amqp://guest:guest@{settings.rabbitmq.host}/")

    # Creating a channel
    channel = await connection.channel()

    exchange_pascal = await channel.declare_exchange(
        settings.pascal.exchange,
        ExchangeType(settings.pascal.exchange_type),
    )

    client = MIModeClient.from_config(
        config=settings.pascal.mq.mi_mode,
        channel=channel,
        exchange=exchange_pascal,
        time_out=10.0,
        name_suffix="",
    )
    logging.info(type(client))

    # await client.start_control()
    # await client.start_state()
    await client.start()

    logging.info("Getting state")
    state = await client.get_state()
    logging.info(f"State: {state}")
    await asyncio.sleep(1)
    logging.info("Getting state done")

    scope = pcmd.PascalScope()
    with scope:
        with pcmd.ForLoop(int(np.random.randint(1, 5))) as loop:
            loop.add_child( pcmd.Beep() )
            loop.add_child( pcmd.Wait(int(np.random.randint(1, 5))) )
        scope.add_child(loop)

    logging.info(f"Scope: \n{scope}")
    exec_future= client.execute_command(scope)
    exec_result = await exec_future
    logging.info(f"Execution result: {exec_result}")

    scope = pcmd.PascalScope()
    with scope:
        scope.add_child( pcmd.Beep() )
        scope.add_child( pcmd.Wait(1) )

    logging.info(f"Scope: \n{scope}")
    exec_result= await client.execute_command(scope)
    logging.info(f"Execution result: {exec_result}")

    c = pcmd.Beep()
    logging.info(f"Command: \n{c}")
    exec_result= await client.execute_command(c)
    logging.info(f"Execution result: {exec_result}")


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
    asyncio.run(test_mi_client())
