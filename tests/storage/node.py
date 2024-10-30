import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange
from lumi.storage.communication import StorageMessageQueueClient

from lumi.api.lifespan import ConnectionState

connection_state = ConnectionState()

async def start():
    connection = await connect("amqp://guest:guest@localhost/")
    connection_state.connection = connection

    channel = await connection.channel()
    connection_state.channel = channel

    exchange_storage = await channel.declare_exchange(
        "storage", type=ExchangeType.DIRECT
    )

    storage_client = StorageMessageQueueClient(
        channel=channel,
        exchange=exchange_storage,
        routing_key="storage",
        control_routing_key="storage_ctrl",
        state_routing_key="storage_state",
        on_state_callback=None,
        client_name="Storage",
        time_out=10,
    )
    await storage_client.start()
    connection_state.storage_client = storage_client

async def stop_storage():
    print("stop_storage")
    await connection_state.storage_client.end_storage()

async def start_storage(remove_existing_file: bool = True):
    print("start_storage")
    import shutil
    from pathlib import Path

    # Define the path to the datasets folder
    datasets_folder = Path(__file__).parent.parent.parent / "database"
    
    # Define the path to the test_node_0001 folder
    test_node_file = datasets_folder / "test_node_0001.h5py"
    
    # Check if the folder exists
    if test_node_file.exists():
        if remove_existing_file:
            # Remove the file
            test_node_file.unlink()
            print(f"Removed existing {test_node_file}")
    else:
        print(f"{test_node_file} does not exist, no removal needed")

    response = await connection_state.storage_client.start_storage(
            project_name="test_node_0001",
            save_ai=True,
            save_frame=True,
            save_log=True,
            force_rewrite=False,
        )
    print(response.body, response.headers)

async def main():
    await start()
    await start_storage(remove_existing_file=False)
    await asyncio.sleep(5)
    await stop_storage()
    await asyncio.sleep(5)

    print("start_storage without remove_existing_file")
    await start_storage(remove_existing_file=False)
    await asyncio.sleep(5)
    await stop_storage()
    await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())