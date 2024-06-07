"""
This server would run on the pascal machine side
Receive command update and execute command.
"""

from typing import Union

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from models import Command, CommandStatus

from pathlib import Path
# import numpy as np
    
app = FastAPI()

MI_FOLDER = Path("")
MI_FILE_NAME = MI_FOLDER / Path("")

current_cmd = None

@app.get("/")
async def read_root():
    return {"Hello": "World"}


def is_command_finished():
    if (MI_FOLDER / "finished").exists:
        return True
    else:
        return False

def get_current_command():
    global current_cmd
    if current_cmd is None:
        with open(MI_FILE_NAME, "r") as f:
            current_cmd = f.read()
    return current_cmd


@app.get("/commands/status")
async def get_server_status():
    return CommandStatus(
        is_command_finished= is_command_finished(),
        current_command=get_current_command(),
        command_executing=""
    )


@app.post("/commands/update")
async def update_command(command:Command):
    """
        logic:
            update the file in location server if allowed 
    """
    
    with open(MI_FILE_NAME, "w") as f:
        f.write(command.cmd)
    return {"is_command_write" : True}


