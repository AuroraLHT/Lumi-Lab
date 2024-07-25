from pydantic import BaseModel, Field

class StorageRequest(BaseModel) :
    project_name : str
    save_ai : bool = Field(default=False)
    save_frame : bool = Field(default=False)
    save_log : bool = Field(default=False)