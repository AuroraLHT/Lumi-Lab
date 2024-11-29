import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange
from lumi.rheed.communication import CameraMessageQueueClient, LiveCameraMessageQueueClient
from lumi.storage.communication import StorageMessageQueueClient

from lumi.api.lifespan import ConnectionManager
from lumi.config import settings

connection_state = ConnectionManager()

async def start():
    connection = await connect(f"amqp://guest:guest@{settings.rabbitmq.host}/")
    connection_state.connection = connection

    channel = await connection.channel()
    connection_state.channel = channel

    exchange_storage = await channel.declare_exchange(
        "storage", type=ExchangeType.DIRECT
    )
    exchange_rheed = await channel.declare_exchange("RHEED", type=ExchangeType.DIRECT)

    # image client is for all user that connect to this api node
    image_client = CameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key=settings.rheed.mq.image.request,
        control_routing_key=settings.rheed.mq.image.ctrl,
        state_routing_key=settings.rheed.mq.image.state,
        on_state_callback=None,
        client_name=settings.rheed.mq.image.name,
        time_out=10,
    )
    await image_client.start()
    connection_state.image_client = image_client


    live_camera_client = LiveCameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key=settings.rheed.mq.live_image.request,
        control_routing_key=settings.rheed.mq.live_image.ctrl,
        state_routing_key=settings.rheed.mq.live_image.state,
        on_response_callback=lambda x: print(x),
        client_name=settings.rheed.mq.live_image.name,
        time_out=10,
        on_state_callback=lambda x: print(x),
    )
    connection_state.live_camera_client = live_camera_client

async def stop_live_camera():
    print("stop_live_camera")
    await connection_state.live_camera_client.stop()

async def start_live_camera():
    print("start_live_camera")
    await connection_state.live_camera_client.start()
    await connection_state.live_camera_client.start_server_streaming()

async def main():
    await start()
    await start_live_camera()
    await asyncio.sleep(10)
    await stop_live_camera()
    await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())