import asyncio

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
import time
from pathlib import Path
import csv
import json

from tqdm import tqdm

import datetime
from chamber_log import PARSE_DICT, parse_bit_field

import argparse

log_filename = None
csv_reader = None
csv_reader_line = 0
pbar = tqdm()

async def receive_filename(message: AbstractIncomingMessage) -> None:
    global log_filename
    global csv_reader
    global csv_reader_line

    """
    on_message doesn't necessarily have to be defined as async.
    Here it is to show that it's possible.
    """
    print(" [x] Received message %r" % message)
    print("Message body is: %r" % message.body)
    log_filename = message.body.decode()
    csv_reader = await open_csv(log_filename)
    header = next(csv_reader, None)
    csv_reader_line = 0

    # we need to ack back to the rabbitmq server otherwise it would timeout this channel in 30 minute (by default)
    await message.ack()

    # print("Before sleep!")
    # await asyncio.sleep(5)  # Represents async I/O operations
    # print("After sleep!")


async def open_csv(filename, timeout=10):
    filename = Path(filename)
    start = time.time()

    while True:
        if filename.exists():
            csv_file = filename.open(mode='r')
            csv_reader = csv.DictReader(csv_file)
            if csv_reader is not None:
                header = next(csv_reader, None)
                break
            else:
                csv_file.close()
    
        if time.time() - start < timeout:
            await asyncio.sleep(delay=0.1)
        else:
            raise TimeoutError("Cannot read the log csv file")
    return csv_reader


def process_row(row):
    today = datetime.date.today()
    row['Time'] = f"{today.isoformat()} {row['Time']}"
    for k, v in PARSE_DICT.items():
        row.update( parse_bit_field( row[k], v) )
    return row


async def auto_log_publish(exchange) -> None:    
    global log_filename
    global csv_reader
    global csv_reader_line

    while True:
        if csv_reader is None: 
            await asyncio.sleep(1)
            continue

        for row in csv_reader:
            row = process_row(row)

            message_body = json.dumps(row).encode()

            message = aio_pika.Message(
                message_body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )

            await exchange.publish(message, routing_key="chamber")                
            csv_reader_line += 1
            # print("row", row['index'])
            pbar.update(1)
        # print(f'Processed {csv_reader_line} lines.')
        await asyncio.sleep(1)


async def main(args) -> None:
    # Perform connection
    url = f"amqp://guest:guest@{args.host}/"
    connection = await aio_pika.connect(url)
    # connection = await aio_pika.connect("localhost")

    async with connection:
        # Creating a channel
        channel = await connection.channel()

        logs_exchange = await channel.declare_exchange(
            "logs", aio_pika.ExchangeType.DIRECT,
        )

        operation_exchange = await channel.declare_exchange(
            "operation", aio_pika.ExchangeType.TOPIC
        )
        
        # Declaring queue
        logfile_queue = await channel.declare_queue(None, exclusive=True)
        # binding the queue to the exchange
        await logfile_queue.bind(operation_exchange, routing_key="log.filename")

        # Start listening the queue with name 'hello'
        consumer = asyncio.create_task( logfile_queue.consume(receive_filename, no_ack=False) )
        publisher = asyncio.create_task( auto_log_publish(logs_exchange) )

        await asyncio.gather(consumer, publisher)

        # print(" [*] Waiting for messages. To exit press CTRL+C")
        # await asyncio.Future()


if __name__ == "__main__":
    with open("config.json", "r") as f:
        config = json.load(f)

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="rabbitmq host", default=config['RabbitmqHost'])
    args = parser.parse_args()
    asyncio.run(main(args))