#!/usr/bin/env python

import asyncio
import websockets
import numpy as np

input_config = {
    'batch' : 4,
    'shape' : [600, 800],
    'dtype' : np.uint16,
}

async def handler(websocket):
    # async for message in websocket:
    #     await websocket.send(message)
    batch = []
    while True:
        binary_data = await websocket.recv()
        # print(len(binary_data))

        batch.append(binary_data)
        assert input_config['batch'] >= len(batch)
        if input_config['batch'] == len(batch):
            for i in range(input_config['batch']):
                await websocket.send(batch[i])
            batch = []

async def main():
    async with websockets.serve(handler, "localhost", 8000):
        await asyncio.Future()  # run forever

asyncio.run(main())