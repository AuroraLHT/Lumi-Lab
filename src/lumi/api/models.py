from pydantic import BaseModel

class StorageRequest(BaseModel) :
    project_name : str
    save_ai : bool
    save_frame : bool
    save_log : bool