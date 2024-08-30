import asyncio
import logging
from aio_pika import connect, ExchangeType, Message
from aio_pika.abc import AbstractConnection, AbstractChannel, AbstractExchange

logging.basicConfig(level=logging.INFO)
import random

async def publish_messages(channel:AbstractChannel, exchange_name="", routing_key="example_queue"):
    for i in range(100):
        message_body = f"Test message {random.randint(1, 1000)}"
        message = Message(message_body.encode())
        await channel.default_exchange.publish(message, routing_key=routing_key)
        logging.info(f"Published message: {message_body}")
        await asyncio.sleep(0.1)  # Publish a message every second

async def setup_publisher():
    connection = await connect("amqp://guest:guest@localhost/")
    channel = await connection.channel()
    return channel

class ExampleClient:
    def __init__(self):
        self.queue = None
        self._consumer_tag = None

    async def setup(self):
        # Connect to RabbitMQ
        connection = await connect("amqp://guest:guest@localhost/")
        self.channel = await connection.channel()
        
        # Declare a queue
        self.queue = await self.channel.declare_queue("example_queue", exclusive=True)
        
        # Start consuming
        self._consumer_tag = await self.queue.consume(self.on_message, no_ack=True)
        logging.info("Queue setup and consuming started")

    async def on_message(self, message):
        # async with message.process():
            # logging.info(f"Received message: {message.body.decode()}")
        logging.info(f"Received message: {message.body.decode()}")

    async def cancel_queue(self):
        if self.queue and self._consumer_tag:
            await self.queue.cancel(self._consumer_tag)
            await self.queue.delete()

            # try:
            #     await self.queue.cancel(self._consumer_tag)
            #     logging.info("Queue cancelled successfully")
            # except Exception as e:
            #     logging.error(f"Error cancelling queue: {e}")
            self._consumer_tag = None
        else:
            logging.info("Queue is not active, nothing to cancel")

async def main():



    client = ExampleClient()    
    await client.setup()

    publish_task = asyncio.create_task(publish_messages(client.channel, exchange_name="", routing_key="example_queue"))

    await asyncio.sleep(2)  # Wait a bit between cancellations

    # Cancel the queue multiple times
    cancel_tasks = [asyncio.create_task(client.cancel_queue()) for _ in range(10)]
    for i, task in enumerate(cancel_tasks):
        logging.info(f"Attempting to cancel queue (attempt {i+1})")
        
    await asyncio.gather(*cancel_tasks)
        # await asyncio.sleep(1)  # Wait a bit between cancellations

    # Try to cancel again after queue is already cancelled
    logging.info("Attempting to cancel queue after it's already cancelled")
    await client.cancel_queue()
    await publish_task

if __name__ == "__main__":
    asyncio.run(main())