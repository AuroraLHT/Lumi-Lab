from pydantic import BaseModel, Field
from typing import Optional, Literal

class StorageRequest(BaseModel) :
    project_name : str
    save_ai : bool = Field(default=False)
    save_frame : bool = Field(default=False)
    save_log : bool = Field(default=False)

class WebsocketMessageHeaders(BaseModel):
    target: str
    operation: str
    payload_type: Literal["json", "text", "bytes"]

    def to_dict(self) -> dict:
        return self.model_dump()
