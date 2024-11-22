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

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange

from .lifespan import lifespan
from .routes import rheed, chamber, storage

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
    "*" # this should allow all origins
    
    # "http://127.0.0.1:8000",
    # "http://localhost:8000",
    # "http://[::1]:8000",  # IPv6 localhost

    # "http://127.0.0.1:5173",
    # "http://localhost:5173",
    # "http://[::1]:5173",  # IPv6 localhost
    # "http://0.0.0.0:5173",  # Any IPv4 address
    # # Add other origins if needed

    # "http://127.0.0.1:5174",
    # "http://localhost:5174",
    # "http://[::1]:5174",  # IPv6 localhost
    # "http://0.0.0.0:5174",  # Any IPv4 address
]

FORMAT = "%(asctime)s %(levelname)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # You can also set to ["*"] to allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Or specify allowed methods like ["GET", "POST"]
    allow_headers=["*"],  # Or specify allowed headers
)


app.include_router(rheed.router)
app.include_router(chamber.router)
app.include_router(storage.router)


@app.get("/")
async def read_root():
    with open(Path(__file__).parent / "index.html", "r") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content, status_code=200)

