import unittest
import asyncio
import uuid
import aio_pika
from aio_pika import Message

async def main():
    # Setup
    connection = await aio_pika.connect_robust("amqp://guest:guest@localhost/")
    channel = await connection.channel()
    queue_name = "test_uuid_queue"
    queue = await channel.declare_queue(queue_name, auto_delete=True)

    # Create a UUID
    original_uuid = uuid.uuid4()

    async def publish_task():
        try:
            await channel.default_exchange.publish(
                Message(b"", headers={"...":"...", "uuid": original_uuid}),
                routing_key=queue_name
            )
        except Exception as e:
            print(e)


    # Send the UUID
    task = asyncio.create_task( publish_task() )

    # Receive the UUID
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                print(message.headers)
                break

    # Assert that the received UUID matches the original
    assert str(original_uuid) == str(message.headers["uuid"]), "UUIDs do not match"
    print("UUID transmission test passed successfully")
    await task

if __name__ == '__main__':
    asyncio.run(main())
