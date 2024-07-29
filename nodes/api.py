from typing import Union
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage
import json
import struct

import logging
from pathlib import Path

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    LiveVideoFragmentsMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    CameraMessageQueueClient,
    LiveDetectionMessageQueueClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
    StorageMessageQueueClient,
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
storage_client = None

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
    global storage_client

    # put startup code here
    connection = await connect("amqp://guest:guest@localhost/")
    channel = await connection.channel()

    exchange_rheed = await channel.declare_exchange("RHEED", type=ExchangeType.DIRECT)
    exchange_chamber = await channel.declare_exchange("chamber", type=ExchangeType.DIRECT)
    exchange_storage = await channel.declare_exchange("storage", type=ExchangeType.DIRECT)

    # image client is for all user that connect to this api node
    image_client = CameraMessageQueueClient(
        channel=channel, exchange=exchange_rheed, routing_key="image", control_routing_key="image_ctrl", client_name="Camera", time_out=10,
    )
    await image_client.start()

    video_fragment_client = VideoFragmentsMessageQueueClient(
        channel=channel, exchange=exchange_rheed, routing_key="live_video_history", control_routing_key="live_video_history_ctrl", client_name="Fragment", time_out=10
    )
    await video_fragment_client.start()

    log_client = ChamberLogMessageQueueClient(
        channel=channel, exchange=exchange_chamber, routing_key="log", control_routing_key="log_ctrl", client_name="Chamber Log", time_out=10
    )
    await log_client.start()

    storage_client = StorageMessageQueueClient(
        channel=channel, exchange=exchange_storage, routing_key="storage", control_routing_key="storage_ctrl", client_name="Storage", time_out=60
    )
    await storage_client.start()

    yield
    # put shutdown code here
    await connection.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def read_root():
    with open(Path(__file__).parent.parent/"src/lumi/api/index.html", "r") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content, status_code=200)


@app.get("/RHEED/image")
async def read_root():
    global image_client
    content, headers = await image_client.get()
    # logging.info(headers)
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

@app.post("/storage/start")
async def start_storage(request : StorageRequest):
# async def start_storage(request: Request):
    # print(await request.body())

    global storage_client
    # logging.debug(request)

    # raw_body = await request.json()
    
    # # Print or log the raw body
    # print("Raw data received:", raw_body)

    # try:
    #     # Try to parse the data using your Pydantic model
    #     request = StorageRequest(**raw_body)
    # except Exception as e:
    #     # Handle the case where the data does not match the model
    #     print("Error parsing data:", e)
    #     return JSONResponse(
    #         status_code=400,
    #         content={"error": "Invalid data", "details": str(e)},
    #     )
    # print(
    #     dict(
    #     project_name=request.project_name,
    #     save_ai=request.save_ai,
    #     save_frame=request.save_frame,
    #     save_log= request.save_log
    #     )
    # )
    response = await storage_client.start_storage(
        project_name=request.project_name,
        save_ai=request.save_ai,
        save_frame=request.save_frame,
        save_log= request.save_log
    )

    # dirty patch 
    # headers field need all str

    if response.body is None:
        # return Response(
        #     content=None,
        #     status_code=500,
        #     media_type="application/octet-stream",
        #     headers={"msg":"fail to start the storage process. visit server log for more details"},
        # )
        return JSONResponse(
            content={"msg":"fail to start the storage process. visit server log for more details"},
            status_code=500,
            headers={},
        )

    
    else:
        return JSONResponse(
            content={"msg":"succfully start the storage"},
            status_code=200,
            headers= {str(k):str(v) for k, v in response.headers.items()},
        )
        
        # return Response(
        #     # content=response.body,
        #     content="succ",
        #     status_code=200,
        #     media_type="application/octet-stream",
        #     headers= {str(k):str(v) for k, v in response.headers.items()},
        # )


@app.post("/storage/end")
async def end_storage():
    global storage_client
    response = await storage_client.end_storage()
    # print(response.body, type(response.body))
    if response.body is not None:
        return JSONResponse(
            content={"msg":"succfully end the storage process."},
            status_code=200,
            headers={},
        )

        # return Response(
        #     content=None,
        #     status_code=500,
        #     media_type="application/octet-stream",
        #     headers={"msg":"fail to acquire log"},
        # )
    
    else:
        return JSONResponse(
            content={"msg":"fail to end the storage process. visit server log for more details"},
            status_code=500,
            headers={},
        )

        # return Response(
        #     content=response.body,
        #     status_code=200,
        #     media_type="application/octet-stream",
        #     headers= {str(k):str(v) for k, v in response.headers.items()},
        # )


@app.websocket("/RHEED/cam/live")
async def websocket_endpoint(websocket: WebSocket):
    global video_fragment_client

    async def on_message():
        logging.info("start message")
        async for message in websocket.iter_text():
            logging.info(message)
            await asyncio.sleep(0.1)
        # while True:
        #     message = await websocket.receive()
        #     logging.info(message)
        #     await asyncio.sleep(0.1)

        logging.info("end in")

    async def send_fragment(fragment):
        """
        flag for success or not
        """
        # logging.info(f"send fragment {len(fragment)}")
        try:
            await websocket.send_bytes(fragment)
        except Exception as e:
            logging.error(f"Video websocket {e}")
            return False
        return True
        # await asyncio.sleep(0.003)

    await websocket.accept()
    logging.info("live camera websocket accepted")

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
    logging.info(message)

    live_client = LiveVideoFragmentsMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="live_video",
        control_routing_key="live_video_ctrl",
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
    #     logging.info(len(content))
    #     await websocket.send_bytes(content)

    ws_in_task = asyncio.create_task(on_message(), name="ws_in")
    # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
    # ws_out_task = asyncio.create_task(live_client.start(), name="ws_out")
    await live_client.start()
    await live_client.start_streaming()

    await ws_in_task
    # await ws_out_task

    await asyncio.Future()
    logging.info("live camera websocket exit")


@app.websocket("/RHEED/detection/live")
async def websocket_endpoint(websocket: WebSocket):
    global channel

    async def on_message():
        logging.info("start detection on message loop")
        async for message in websocket.iter_text():
            logging.info(f"detection on message {message}")
            if message == "start":
                await live_detection_client.start_streaming()
            elif message == "stop":
                await live_detection_client.stop_streaming()

            logging.debug(message)
            await asyncio.sleep(0.1)

        logging.info("end detection on message loop")

    async def send_payload(payload, header):
        """
        flag for success or not
        """
        # logging.info(f"send json {len(json_text)}")
        try:
            header_json = json.dumps(header)
            # Combine header and binary data
            header_length = struct.pack('>I', len(header_json))
            data = header_length + header_json.encode('utf-8') + payload            
            await websocket.send_bytes(data)
            # await websocket.send_json(json_text, mode='text')
            # await websocket.send_text(json_text)
        except Exception as e:
            # print(e)
            logging.error(e)
            # raise e
            return False
        return True
        # await asyncio.sleep(0.003)

    await websocket.accept()
    logging.info("detect websocket accepted")

    async def on_live_message_callback(message: AbstractIncomingMessage):
        """
        return : succ or not state flag
        """
        # TODO : add condition for finding other streaming option
        return await send_payload(message.body, message.headers)
        # logging.info(f"publish detection with headers {headers}")

    # message = await websocket.receive()
    # logging.info(message)

    live_detection_client = LiveDetectionMessageQueueClient(
        channel = channel,
        exchange = exchange_rheed,
        routing_key = "live_detection",
        control_routing_key = "live_detection_ctrl",
        on_response_callback = on_live_message_callback,
        client_name="Live Detection",
        time_out=10
    )
    await live_detection_client.start()
    ws_in_task = asyncio.create_task(on_message(), name = "ws_ai_in")
    # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
    # ws_out_task = asyncio.create_task(live_detection_client.start(), name = "ws_ai_out")

    await ws_in_task
    # await ws_out_task

    await asyncio.Future()
    logging.info("detection websocket exit")



@app.websocket("/chamber/log/live")
async def websocket_endpoint(websocket: WebSocket):

    async def on_message():
        logging.info("start log on message loop")
        async for message in websocket.iter_text():
            logging.info(f"log on message {message}")
            if message == "start":
                await live_log_client.start_streaming()
            elif message == "stop":
                await live_log_client.stop_streaming()

            logging.debug(message)
            await asyncio.sleep(0.1)

        logging.info("end log on message loop")

    async def send_json(json_text):
        """
        flag for success or not
        """
        # logging.info(f"send json {len(json_text)}")
        try:
            # await websocket.send_json(json_text, mode='text')
            await websocket.send_text(json_text)
        except Exception as e:
            logging.error(e)
            return False
        return True
        # await asyncio.sleep(0.003)

    await websocket.accept()
    logging.info("log web socket accepted")

    async def on_live_message_callback(message: AbstractIncomingMessage):
        """
        return : succ or not state flag
        """
        # TODO : add condition for finding other streaming option
        headers = message.headers
        # logging.debug(f"publish log with headers {headers}")

        return await send_json(message.body.decode())

    # message = await websocket.receive()
    # logging.info(message)

    live_log_client = LiveChamberLogMessageQueueClient(
        channel = channel,
        exchange = exchange_chamber,
        routing_key = "live_log",
        control_routing_key = "live_log_ctrl",
        on_response_callback = on_live_message_callback,
        client_name = "Live Log",
        time_out = 10,
    )
    await live_log_client.start()

    ws_in_task = asyncio.create_task(on_message(), name = "ws_log_in")
    # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
    # ws_out_task = asyncio.create_task(, name = "ws_log_out")

    await ws_in_task
    # await ws_out_task

    await asyncio.Future()
    logging.info("log websocket exit")
