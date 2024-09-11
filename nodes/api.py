from collections.abc import Awaitable, Callable
from typing import Any, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import struct
import logging
from pathlib import Path
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange

from lumi.api.models import StorageRequest
from lumi.api.communication import (
    BasicStreamClient,
    LiveVideoFragmentsMessageQueueClient,
    VideoFragmentsMessageQueueClient,
    CameraMessageQueueClient,
    LiveDetectionMessageQueueClient,
    ChamberLogMessageQueueClient,
    LiveChamberLogMessageQueueClient,
    StorageMessageQueueClient,
)

import traceback

"""
TODO: refactor into this structure
api/
├── __init__.py
├── main.py
├── connection_state.py
├── websocket_handlers.py
├── routes.py
├── lifespan.py
└── utils.py
"""

import sys
sys.setrecursionlimit(10000) 

# Allow all origins, or specify a list of allowed origins
origins = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://[::1]:8000",  # IPv6 localhost

    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://[::1]:5173",  # IPv6 localhost
    "http://0.0.0.0:5173",  # Any IPv4 address
    # Add other origins if needed
]



FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

def package_payload(payload : bytes, headers : dict) -> bytes:
    header_json = json.dumps(headers)
    header_length = struct.pack(">I", len(header_json))
    return header_length + header_json.encode("utf-8") + payload

@dataclass
class ConnectionStateManager:
    connection: Optional[AbstractConnection] = None
    channel: Optional[AbstractChannel] = None
    exchange_rheed: Optional[AbstractExchange] = None
    exchange_chamber: Optional[AbstractExchange] = None
    exchange_storage: Optional[AbstractExchange] = None

    image_client: Optional[CameraMessageQueueClient] = None
    video_fragment_client: Optional[VideoFragmentsMessageQueueClient] = None
    log_client: Optional[ChamberLogMessageQueueClient] = None
    storage_client: Optional[StorageMessageQueueClient] = None

    live_video_client: Optional[LiveVideoFragmentsMessageQueueClient] = None
    live_log_client: Optional[LiveChamberLogMessageQueueClient] = None
    live_detection_client: Optional[LiveDetectionMessageQueueClient] = None


connection_state = ConnectionStateManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # https://apidog.com/articles/fastapi-multiple-threading-python/#:~:text=In%20the%20context%20of%20FastAPI,network%20requests)%20to%20separate%20threads.
    # https://fastapi.tiangolo.com/advanced/events/
    global connection_state

    # put startup code here
    connection = await connect("amqp://guest:guest@localhost/")
    connection_state.connection = connection

    channel = await connection.channel()
    connection_state.channel = channel

    exchange_rheed = await channel.declare_exchange("RHEED", type=ExchangeType.DIRECT)
    exchange_chamber = await channel.declare_exchange(
        "chamber", type=ExchangeType.DIRECT
    )
    exchange_storage = await channel.declare_exchange(
        "storage", type=ExchangeType.DIRECT
    )
    connection_state.exchange_chamber = exchange_chamber
    connection_state.exchange_rheed = exchange_rheed
    connection_state.exchange_storage = exchange_storage

    # image client is for all user that connect to this api node
    image_client = CameraMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="image",
        control_routing_key="image_ctrl",
        state_routing_key="image_state",
        on_state_callback=None,
        client_name="Camera",
        time_out=10,
    )
    await image_client.start()
    connection_state.image_client = image_client

    video_fragment_client = VideoFragmentsMessageQueueClient(
        channel=channel,
        exchange=exchange_rheed,
        routing_key="live_video_history",
        control_routing_key="live_video_history_ctrl",
        state_routing_key="live_video_history_state",
        on_state_callback=None,
        client_name="Fragment",
        time_out=10,
    )
    await video_fragment_client.start()
    connection_state.video_fragment_client = video_fragment_client

    log_client = ChamberLogMessageQueueClient(
        channel=channel,
        exchange=exchange_chamber,
        routing_key="log",
        control_routing_key="log_ctrl",
        state_routing_key="log_state",
        on_state_callback=None,
        client_name="Chamber Log",
        time_out=10,
    )
    await log_client.start()
    connection_state.log_client = log_client

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

    # this globle client is only open for status checking
    live_video_client = LiveVideoFragmentsMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_rheed,
        routing_key="live_video",
        control_routing_key="live_video_ctrl",
        state_routing_key="live_video_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live Fragment Monitor",
        time_out=10,
    )
    await live_video_client.start_control()
    connection_state.live_video_client = live_video_client



    live_log_client = LiveChamberLogMessageQueueClient(
        channel=connection_state.channel,
        exchange=connection_state.exchange_chamber,
        routing_key="live_log",
        control_routing_key="live_log_ctrl",
        state_routing_key="live_log_state",
        on_response_callback=None,
        on_state_callback=None,
        client_name="Live Log Monitor",
        time_out=10,
    )
    await live_log_client.start_control()
    connection_state.live_log_client = live_log_client

    yield
    # put shutdown code here
    await connection.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # You can also set to ["*"] to allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Or specify allowed methods like ["GET", "POST"]
    allow_headers=["*"],  # Or specify allowed headers
)


@app.get("/")
async def read_root():
    with open(Path(__file__).parent.parent / "src/lumi/api/index.html", "r") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content, status_code=200)

def update_state(body : bytes, state : dict) -> str:
    content : dict = json.loads(body)
    content.update(state)
    return json.dumps(content)
    
@app.get("/RHEED/cam/live/state")
async def get_rheed_cam_state():
    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"RHEED cam state put {message.body.decode()}")
            await queue.put(message.body)

        live_video_client = LiveVideoFragmentsMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_rheed,
            routing_key="live_video",
            control_routing_key="live_video_ctrl",
            state_routing_key="live_video_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live RHEED Cam Monitor",
            time_out=10,
        )
        await live_video_client.start_state()
        await live_video_client.start_control()

        try:
            state = await asyncio.wait_for(live_video_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"

        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED cam state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_video_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")



@app.get("/RHEED/detection/live/state")
async def get_rheed_detection_state():
    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"detection state put {message.body.decode()}")

            await queue.put(message.body)

        live_detection_client = LiveDetectionMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_rheed,
            routing_key="live_detection",
            control_routing_key="live_detection_ctrl",
            state_routing_key="live_detection_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live Detection Monitor",
            time_out=10,
        )
        await live_detection_client.start_state()
        await live_detection_client.start_control()
        try:
            state = await asyncio.wait_for(live_detection_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"
        # print(state)
        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED detection state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_detection_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")

@app.get("/chamber/log/live/state")
async def get_chamber_log_state():
    async def state_generator():
        queue = asyncio.Queue()

        async def on_state_callback(message: AbstractIncomingMessage):
            # logging.info(f"chamber log state put {message.body.decode()}")
            await queue.put(message.body)

        live_log_client = LiveChamberLogMessageQueueClient(
            channel=connection_state.channel,
            exchange=connection_state.exchange_chamber,
            routing_key="live_log",
            control_routing_key="live_log_ctrl",
            state_routing_key="live_log_state",
            on_response_callback=None,
            on_state_callback=on_state_callback,
            client_name="Live Log Monitor",
            time_out=10,
        )
        await live_log_client.start_state()
        await live_log_client.start_control()
        try:
            state = await asyncio.wait_for(live_log_client.get_state(return_bytes=True), timeout=5.0)
            state = update_state(state, {"is_available": True})
        except asyncio.TimeoutError:
            state = json.dumps({"is_available": False})

        # print(state)

        yield f"data: {state}\n\n"

        try:
            while True:
                body : bytes = await queue.get()
                state = update_state(body, {"is_available": True})

                logging.debug(f"RHEED cam state yield {state}")
                yield f"data: {state}\n\n"
        finally:
            await live_log_client.stop()

    return StreamingResponse(state_generator(), media_type="text/event-stream")
            

@app.get("/RHEED/image")
async def read_root():
    global connection_state
    response = await connection_state.image_client.request()
    # logging.info(headers)
    return Response(
        content=response.body,
        status_code=200,
        media_type="application/octet-stream",
        headers={str(k): str(v) for k, v in response.headers.items()},
    )


@app.get("/chamber/log")
async def get_chamber_log():
    global connection_state
    response = await connection_state.log_client.request()

    if response.body is None:
        return Response(
            content=response.body,
            status_code=500,
            media_type="application/json",
            headers={"msg": "fail to acquire log"},
        )

    else:
        return Response(
            content=response.body,
            status_code=200,
            media_type="application/json",
            headers={str(k): str(v) for k, v in response.headers.items()},
        )



@app.post("/storage/start")
async def start_storage(request: StorageRequest):
    # async def start_storage(request: Request):
    # print(await request.body())

    global connection_state
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
    response = await connection_state.storage_client.start_storage(
        project_name=request.project_name,
        save_ai=request.save_ai,
        save_frame=request.save_frame,
        save_log=request.save_log,
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
            content={
                "msg": "fail to start the storage process. visit server log for more details"
            },
            status_code=500,
            headers={},
        )

    else:
        return JSONResponse(
            content={"msg": response.body.decode()},
            status_code=200,
            headers={str(k): str(v) for k, v in response.headers.items()},
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
    global connection_state
    response = await connection_state.storage_client.end_storage()
    # print(response.body, type(response.body))
    if response.body is not None:
        return JSONResponse(
            content={"msg": response.body.decode()},
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
            content={
                "msg": "fail to end the storage process. visit server log for more details"
            },
            status_code=500,
            headers={},
        )

        # return Response(
        #     content=response.body,
        #     status_code=200,
        #     media_type="application/octet-stream",
        #     headers= {str(k):str(v) for k, v in response.headers.items()},
        # )



async def generic_websocket_handler(
    websocket: WebSocket,
    client_class: type[BasicStreamClient],
    client_params: dict,
    send_function: Callable[[Any, dict], Awaitable[bool]],
    endpoint_name: str,
    initial_data_function: Optional[Callable[[], Awaitable[list[Any]]]] = None,
):
    async def on_live_message_callback(message: AbstractIncomingMessage):
        return await send_function(message.body, message.headers)

    # client = None
    client = client_class(
        **client_params,
        on_response_callback=on_live_message_callback,
        on_state_callback=None,
        client_name=f"Live {endpoint_name}",
        time_out=10,
    )
    await client.start_control()
    # client.start_state()

    async def on_message():
        nonlocal client
        try:
            logging.info(f"start {endpoint_name} on_message loop")
            async for message in websocket.iter_text():
                logging.info(f"{endpoint_name} on message {message}")
                if message == "start_server":
                    if client is not None:
                        await client.start_server_streaming()
                elif message == "stop_server":
                    if client is not None:
                        await client.stop_server_streaming()
                # elif message == "init":
                elif message == "start_streaming":
                    if not client.is_main_running():
                        if initial_data_function is not None:
                            initial_data = await initial_data_function()
                            for data, headers in initial_data:
                                await send_function(data, headers)
                                logging.info(f"{endpoint_name} send initialization data {headers}")

                        await client.start_main()

                elif message == "end_streaming":
                    if client is not None:                        
                        await client.stop_main()
                        client = None

                logging.debug(message)
                await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"{endpoint_name} on_message error: {e}")

            error_info = traceback.extract_tb(e.__traceback__)[-1]
            file_name = error_info.filename
            line_number = error_info.lineno
            logging.error(f"{endpoint_name} Error in {file_name} at line {line_number}: {str(e)}")

        logging.info(f"end {endpoint_name} on message loop")

    await websocket.accept()
    logging.info(f"{endpoint_name} websocket accepted")


    ws_in_task = asyncio.create_task(on_message(), name=f"ws_{endpoint_name}_in")
    try:
        await ws_in_task
    except Exception as e:
        logging.info(f"{endpoint_name} websocket received an exception: {e}")

    # await asyncio.Future()
    if client is not None:
        await client.stop()
    logging.info(f"{endpoint_name} websocket exit")

@app.websocket("/RHEED/cam/live")
async def rheed_cam_live(websocket: WebSocket):
    async def send_fragment(fragment, headers):

        try:
            # header_json = json.dumps(headers)
            # header_length = struct.pack(">I", len(header_json))
            # data = header_length + header_json.encode("utf-8") + fragment
            data = package_payload(fragment, headers)
            await websocket.send_bytes(data)
        except Exception as e:
            logging.error(f"/RHEED/detection/live send_fragment error: {e}")
            return True
        return False

    client_params = {
        "channel": connection_state.channel,
        "exchange": connection_state.exchange_rheed,
        "routing_key": "live_video",
        "control_routing_key": "live_video_ctrl",
        "state_routing_key": "live_video_state",
    }

    await generic_websocket_handler(
        websocket,
        LiveVideoFragmentsMessageQueueClient,
        client_params,
        send_fragment,
        "RHEED cam",
        connection_state.video_fragment_client.get_initial,
    )

@app.websocket("/RHEED/detection/live")
async def rheed_detection_live(websocket: WebSocket):
    async def send_payload(payload, header):
        try:
            # header_json = json.dumps(header)
            # header_length = struct.pack(">I", len(header_json))
            # data = header_length + header_json.encode("utf-8") + payload
            data = package_payload(payload, header)
            await websocket.send_bytes(data)
        except Exception as e:
            logging.error(f"/RHEED/detection/live send_payload error: {e}")
            return True
        return False

    client_params = {
        "channel": connection_state.channel,
        "exchange": connection_state.exchange_rheed,
        "routing_key": "live_detection",
        "control_routing_key": "live_detection_ctrl",
        "state_routing_key": "live_detection_state",
    }

    await generic_websocket_handler(
        websocket,
        LiveDetectionMessageQueueClient,
        client_params,
        send_payload,
        "RHEED detection"
    )

@app.websocket("/chamber/log/live")
async def chamber_log_live(websocket: WebSocket):
    async def send_json(body, headers):
        try:
            json_text = body.decode()
            await websocket.send_text(json_text)
        except Exception as e:
            logging.error(f"/chamber/log/live send_json error: {e}")
            return True
        return False

    client_params = {
        "channel": connection_state.channel,
        "exchange": connection_state.exchange_chamber,
        "routing_key": "live_log",
        "control_routing_key": "live_log_ctrl",
        "state_routing_key": "live_log_state",
    }

    await generic_websocket_handler(
        websocket,
        LiveChamberLogMessageQueueClient,
        client_params,
        send_json,
        "chamber log"
    )


# @app.websocket("/RHEED/cam/live")
# async def websocket_endpoint(websocket: WebSocket):
#     global connection_state

#     async def on_message():

#         try:
#             logging.info("start rheed video on_message loop")
#             async for message in websocket.iter_text():
#                 logging.info(f"rheed video on message {message}")
#                 if message == "start":
#                     await live_client.start_streaming()
#                 elif message == "stop":
#                     await live_client.stop_streaming()

#                 logging.debug(message)
#                 await asyncio.sleep(0.1)

#             logging.info("end rheed video on message loop")
#         except Exception as e:
#             logging.error(f"/RHEED/cam/live on_message error: {e}")


#     async def send_fragment(fragment):
#         """
#         flag for stop or not
#         """
#         # logging.info(f"send fragment {len(fragment)}")
#         try:
#             await websocket.send_bytes(fragment)
#         except Exception as e:
#             logging.error(f"/RHEED/cam/live send_fragment error: {e}")
#             return True
#         return False
#         # await asyncio.sleep(0.003)

#     await websocket.accept()
#     logging.info("live camera websocket accepted")

#     async def on_live_message_callback(message: AbstractIncomingMessage):
#         """
#         return : succ or not state flag
#         """
#         # TODO : add condition for finding other streaming option
#         headers = message.headers
#         # logging.info(
#         #     f"publish fragment {headers['frag_idx']} frame start from {headers['frame_start']} to {headers['frame_end']}"
#         # )

#         return await send_fragment(message.body)

#     # message = await websocket.receive()
#     # logging.info(message)

#     live_client = LiveVideoFragmentsMessageQueueClient(
#         channel=connection_state.channel,
#         exchange=connection_state.exchange_rheed,
#         routing_key="live_video",
#         control_routing_key="live_video_ctrl",
#         state_routing_key="live_video_state",
#         on_response_callback=on_live_message_callback,
#         on_state_callback=None, 
#         client_name="Live Fragment",
#         time_out=10,
#     )

#     initial_fragments = await connection_state.video_fragment_client.get_initial()
#     for fragment in initial_fragments:
#         await send_fragment(fragment)

#     # source = "test.mp4"
#     # source = "BigBuckBunny.mp4"
#     # source = "frag_bunny.mp4"
#     # source = "output.mp4"

#     # with open(source, "rb") as f:
#     #     content = f.read()
#     #     logging.info(len(content))
#     #     await websocket.send_bytes(content)

#     ws_in_task = asyncio.create_task(on_message(), name="ws_in")
#     await live_client.start()
#     await live_client.start_streaming()

#     await ws_in_task

#     # await asyncio.Future()
#     logging.info("live camera websocket exit")


# @app.websocket("/RHEED/detection/live")
# async def websocket_endpoint(websocket: WebSocket):
#     global connection_state

#     async def on_message():
#         try:
#             logging.info("start detection on message loop")
#             async for message in websocket.iter_text():
#                 logging.info(f"detection on message {message}")
#                 if message == "start":
#                     await live_detection_client.start_streaming()
#                 elif message == "stop":
#                     await live_detection_client.stop_streaming()

#                 logging.debug(message)
#                 await asyncio.sleep(0.1)
#         except Exception as e:
#             logging.error(f"/RHEED/detection/live on_message error: {e}")

#         logging.info("end detection on message loop")

#     async def send_payload(payload, header):
#         """
#         flag for stop or not
#         """
#         # logging.info(f"send json {len(json_text)}")
#         try:
#             header_json = json.dumps(header)
#             # Combine header and binary data
#             header_length = struct.pack(">I", len(header_json))
#             data = header_length + header_json.encode("utf-8") + payload
#             await websocket.send_bytes(data)
#             # await websocket.send_json(json_text, mode='text')
#             # await websocket.send_text(json_text)
#         except Exception as e:
#             # print(e)
#             logging.error(f"/RHEED/detection/live send_payload error: {e}")
#             # raise e
#             return True
#         return False
#         # await asyncio.sleep(0.003)

#     await websocket.accept()
#     logging.info("detect websocket accepted")

#     async def on_live_message_callback(message: AbstractIncomingMessage):
#         """
#         return : succ or not state flag
#         """
#         # TODO : add condition for finding other streaming option
#         return await send_payload(message.body, message.headers)
#         # logging.info(f"publish detection with headers {headers}")

#     # message = await websocket.receive()
#     # logging.info(message)

#     live_detection_client = LiveDetectionMessageQueueClient(
#         channel=connection_state.channel,
#         exchange=connection_state.exchange_rheed,
#         routing_key="live_detection",
#         control_routing_key="live_detection_ctrl",
#         state_routing_key="live_detection_state",
#         on_response_callback=on_live_message_callback,
#         on_state_callback=None, 
#         client_name="Live Detection",
#         time_out=10,
#     )
#     await live_detection_client.start()
#     ws_in_task = asyncio.create_task(on_message(), name="ws_ai_in")
#     # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
#     # ws_out_task = asyncio.create_task(live_detection_client.start(), name = "ws_ai_out")

#     await ws_in_task
#     # await ws_out_task

#     # await asyncio.Future()
#     logging.info("detection websocket exit")


# @app.websocket("/chamber/log/live")
# async def websocket_endpoint(websocket: WebSocket):

#     async def on_message():
#         try:
#             logging.info("start log on message loop")
#             async for message in websocket.iter_text():
#                 logging.info(f"log on message {message}")
#                 if message == "start":
#                     await live_log_client.start_streaming()
#                 elif message == "stop":
#                     await live_log_client.stop_streaming()

#                 logging.debug(message)
#                 await asyncio.sleep(0.1)
#         except Exception as e:
#             logging.error(f"/chamber/log/live on_message error: {e}")

#         logging.info("end log on message loop")

#     async def send_json(json_text):
#         """
#         flag for stop or not
#         """
#         # logging.info(f"send json {len(json_text)}")
#         try:
#             # await websocket.send_json(json_text, mode='text')
#             await websocket.send_text(json_text)
#         except Exception as e:
#             logging.error(f"/chamber/log/live send_json error: {e}")
#             return True
#         return False
#         # await asyncio.sleep(0.003)

#     await websocket.accept()
#     logging.info("log web socket accepted")

#     async def on_live_message_callback(message: AbstractIncomingMessage):
#         """
#         return : succ or not state flag
#         """
#         # TODO : add condition for finding other streaming option
#         headers = message.headers
#         # logging.debug(f"publish log with headers {headers}")

#         return await send_json(message.body.decode())

#     # message = await websocket.receive()
#     # logging.info(message)

#     live_log_client = LiveChamberLogMessageQueueClient(
#         channel=connection_state.channel,
#         exchange=connection_state.exchange_chamber,
#         routing_key="live_log",
#         control_routing_key="live_log_ctrl",
#         state_routing_key="live_log_state",
#         on_response_callback=on_live_message_callback,
#         on_state_callback=None, 
#         client_name="Live Log",
#         time_out=10,
#     )
#     await live_log_client.start()

#     ws_in_task = asyncio.create_task(on_message(), name="ws_log_in")
#     # ws_out_task = asyncio.create_task( send_fragment(websocket=websocket ), name="ws_out" )
#     # ws_out_task = asyncio.create_task(, name = "ws_log_out")

#     await ws_in_task
#     # await ws_out_task

#     # await asyncio.Future()
#     logging.info("log websocket exit")
