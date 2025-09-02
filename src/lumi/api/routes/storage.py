from collections.abc import Awaitable, Callable
from typing import Any, Union, Optional
from contextlib import asynccontextmanager
import asyncio
import json
import struct
import logging
from pathlib import Path
from dataclasses import dataclass
import traceback

from fastapi import FastAPI, WebSocket, Request, APIRouter
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse

from aio_pika.abc import AbstractIncomingMessage

from ..models import StorageRequest

from ..websockets.base import generic_websocket_handler
from ..utils import update_state, pack_payload, decode_json
from ..connection import ConnectionManager

from lumi.config import settings

router = APIRouter()

@router.post("/storage/start")
async def start_storage(request: StorageRequest, resquest_obj: Request):
    connection_state : ConnectionManager = resquest_obj.app.state.connection_state
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
        save_integration=request.save_integration,
    )

    content = {
        "body": response.body.decode(),
        "headers": response.headers
    }

    return JSONResponse(
        content=content,
        status_code=200,
    )        


@router.post("/storage/end")
async def end_storage(request: Request):
    connection_state : ConnectionManager = request.app.state.connection_state
    response = await connection_state.storage_client.end_storage()
    # print(response.body, type(response.body))
    return JSONResponse(
        content={"body": response.body.decode(), "headers": response.headers},
        status_code=200,
    )