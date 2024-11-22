import websockets
import requests
import json
import asyncio


class ChamberClient:
    def __init__(self, host):
        self.host = host
        self.ws = None

    async def connect_ws(self):
        self.ws = await websockets.connect(f'ws://{self.host}/chamber/live')

    async def start_ws(self):
        await self.connect_ws()
        while True:
            message = await self.ws.recv()
  
    async def request_log(self):
        r = requests.get(f'http://{self.host}/chamber/log')
        chamber_log = json.loads( r.content )
        return chamber_log

    async def run(self):
        await self.connect()
        while True:
            message = await self.ws.recv()
            print(message)
