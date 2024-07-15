from typing import Union
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, Response, JSONResponse
import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage

import logging

from .communication import (
    LiveVideoFragmentsMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    CameraMessageQueueClient,
    LiveDetectionClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient
)


FORMAT = '%(asctime)s %(levelname)s:%(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

connection = None
channel = None
exchange_rheed = None
exchange_chamber = None

image_client = None
video_fragment_client = None
log_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # https://apidog.com/articles/fastapi-multiple-threading-python/#:~:text=In%20the%20context%20of%20FastAPI,network%20requests)%20to%20separate%20threads.
    # https://fastapi.tiangolo.com/advanced/events/
    global connection
    global channel
    global exchange_rheed
    global exchange_chamber

    global image_client
    global video_fragment_client
    global log_client

    # put startup code here
    connection = await connect("amqp://guest:guest@localhost/")
    channel = await connection.channel()

    exchange_rheed = await channel.declare_exchange("RHEED", type=ExchangeType.DIRECT)
    exchange_chamber = await channel.declare_exchange("chamber", type=ExchangeType.DIRECT)

    # image client is for all user that connect to this api node
    image_client = CameraMessageQueueClient(
        channel=channel, exchange=exchange_rheed, routing_key="image", client_name="Camera", time_out=10,
    )
    await image_client.start()

    video_fragment_client = VideoFragmentsMessageQueueClient(
        channel=channel, exchange=exchange_rheed, routing_key="live_video_history", client_name="Fragment", time_out=10
    )
    await video_fragment_client.start()

    log_client = ChamberLogMessageQueueClient(
        channel=channel, exchange=exchange_chamber, routing_key="log", client_name="Chamber Log", time_out=10
    )
    await log_client.start()

    yield
    # put shutdown code here
    await connection.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def read_root():
    with open("index.html", "r") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content, status_code=200)


@app.get("/RHEED/image")
async def read_root():
    global image_client
    content, headers = await image_client.get()
    # print(headers)
    return Response(
        content=content,
        status_code=200,
        media_type="application/octet-stream",
        headers=headers,
    )


@app.get("/chamber/log")
async def read_root():
    global log_client
    content, headers = await log_client.get()

    if content is None:
        return Response(
            content=None,
            status_code=500,
            media_type="application/json",
            headers={"msg":"fail to acquire log"},
        )
    
    else:
        return Response(
            content=content,
            status_code=200,
            media_type="application/json",
            headers=headers,
        )



@app.websocket("/RHEED/cam/live")
async def websocket_endpoint(websocket: WebSocket):
    global video_fragment_client

    async def on_message():
        print("start message")
        async for message in websocket.iter_text():
            print(message)
            await asyncio.sleep(0.1)
        # while True:
        #     message = await websocket.receive()
        #     print(message)
        #     await asyncio.sleep(0.1)

        print("end in")

    async def send_fragment(fragment):
        """
        flag for success or not
        """
        # logging.info(f"send fragment {len(fragment)}")
        try:
            await websocket.send_bytes(fragment)
        except Exception as e:
            print(e)
            return False
        return True
        # await asyncio.sleep(0.003)

    await websocket.accept()
    print("accepted")

    async def on_live_message_callback(message: AbstractIncomingMessage):
        """
        return : succ or not state flag
        """
        # TODO : add condition for finding other streaming option
        headers = message.headers
        # logging.info(
        #     f"publish fragment {headers['frag_idx']} frame start from {headers['frame_start']} to {headers['frame_end']}"
        # )

        return await send_fragment(message.body)

    message = await websocket.receive()
    print(message)

    live_client = LiveVideoFragmentsMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="live_video",
        on_response_callback=on_live_message_callback,
        client_name="Live Fragment",
        time_out=10,
    )

    initial_fragments = await video_fragment_client.get_initial()
    for fragment in initial_fragments:
        await send_fragment(fragment)

    # source = "test.mp4"
    # source = "BigBuckBunny.mp4"
    # source = "frag_bunny.mp4"
    # source = "output.mp4"

    # with open(source, "rb") as f:
    #     content = f.read()
    #     print(len(content))
    #     await websocket.send_bytes(content)

    ws_in_task = asyncio.create_task(on_message(), name="ws_in")
    # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
    ws_out_task = asyncio.create_task(live_client.start(), name="ws_out")

    await ws_in_task
    await ws_out_task

    await asyncio.Future()
    print("live camera websocket exit")


@app.websocket("/RHEED/detection/live")
async def websocket_endpoint(websocket: WebSocket):
    global channel

    async def on_message():
        print("start detection on message loop")
        async for message in websocket.iter_text():
            print(f"detection on message {message}")
            if message == "start":
                await live_detection_client.start_streaming()
            elif message == "stop":
                await live_detection_client.stop_streaming()

            print(message)
            await asyncio.sleep(0.1)

        print("end detection on message loop")

    async def send_json(json_text):
        """
        flag for success or not
        """
        # logging.info(f"send json {len(json_text)}")
        try:
            # await websocket.send_json(json_text, mode='text')
            await websocket.send_text(json_text)
        except Exception as e:
            print(e)
            return False
        return True
        # await asyncio.sleep(0.003)

    await websocket.accept()
    print("accepted")

    async def on_live_message_callback(message: AbstractIncomingMessage):
        """
        return : succ or not state flag
        """
        # TODO : add condition for finding other streaming option
        headers = message.headers
        # print(f"publish detection with headers {headers}")

        return await send_json(message.body.decode())

    message = await websocket.receive()
    print(message)

    live_detection_client = LiveDetectionClient(
        channel = channel,
        exchange = exchange_rheed,
        routing_key = "live_detection",
        control_routing_key = "live_detection_control",
        on_response_callback = on_live_message_callback,
    )

    ws_in_task = asyncio.create_task(on_message(), name = "ws_ai_in")
    # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
    ws_out_task = asyncio.create_task(live_detection_client.start(), name = "ws_ai_out")

    await ws_in_task
    await ws_out_task

    await asyncio.Future()
    print("detection websocket exit")



@app.websocket("/chamber/log/live")
async def websocket_endpoint(websocket: WebSocket):

    async def on_message():
        print("start log on message loop")
        async for message in websocket.iter_text():
            print(f"log on message {message}")
            if message == "start":
                await live_log_client.start_streaming()
            elif message == "stop":
                await live_log_client.stop_streaming()

            print(message)
            await asyncio.sleep(0.1)

        print("end log on message loop")

    async def send_json(json_text):
        """
        flag for success or not
        """
        # logging.info(f"send json {len(json_text)}")
        try:
            # await websocket.send_json(json_text, mode='text')
            await websocket.send_text(json_text)
        except Exception as e:
            print(e)
            return False
        return True
        # await asyncio.sleep(0.003)

    await websocket.accept()
    print("accepted")

    async def on_live_message_callback(message: AbstractIncomingMessage):
        """
        return : succ or not state flag
        """
        # TODO : add condition for finding other streaming option
        headers = message.headers
        # print(f"publish log with headers {headers}")

        return await send_json(message.body.decode())

    message = await websocket.receive()
    print(message)

    live_log_client = LiveChamberLogMessageQueueClient(
        channel = channel,
        exchange = exchange_chamber,
        routing_key = "live_log",
        control_routing_key = "live_log_control",
        on_response_callback = on_live_message_callback,
        client_name = "Live Log",
        time_out = 10,
    )

    ws_in_task = asyncio.create_task(on_message(), name = "ws_log_in")
    # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
    ws_out_task = asyncio.create_task(live_log_client.start(), name = "ws_log_out")

    await ws_in_task
    await ws_out_task

    await asyncio.Future()
    print("log websocket exit")
