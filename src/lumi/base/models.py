import json
from typing import TypedDict, Dict

from typing import (
    Callable,
    Tuple,
    List,
    Dict,
    Union,
    Optional,
    ByteString,
    Any,
    Awaitable,
)

"""
TODO: Add Error Types and Error Messages Templates
"""

class BaseMessageHeader(TypedDict):
    """Base class for message queue headers.

    This class defines the required fields that should be present in message headers:
    - message_type: The type/category of the message
    - message_timestamp: When the message was created/sent
    - message_source: The origin/sender of the message

    """
    message_type: str
    message_timestamp: str
    message_source: str


class BaseRequestMessageHeader(BaseMessageHeader):
    """Base class for request message headers.
    """
    request_type: str

class BaseResponseMessageHeader(BaseMessageHeader):
    """Base class for response message headers.
    """
    response_type: str
    request_type: str
    succ: bool
    error_type: str
    error_message: str


class BaseUpdateMessageHeader(BaseMessageHeader):
    """Base class for update message headers.
    """
    update_type: str


class BaseControlRequestMessageHeader(BaseMessageHeader):
    """Base class for control request message headers.
    """
    control_request_type: str

class BaseControlResponseMessageHeader(BaseMessageHeader):
    """Base class for control response message headers.
    """
    control_response_type: str
    control_request_type: str
    succ: bool
    error_type: str
    error_message: str

class BaseStateMessageHeader(BaseMessageHeader):
    """Base class for control message headers.
    """
    state_type: str

class BaseStreamMessageHeader(BaseMessageHeader):
    stream_type: str
    succ: bool
    error_type: str
    error_message: str

class BaseMessageQueueMessage:
    body: bytes
    headers: BaseMessageHeader

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseMessageHeader):
        self.body = self.encode_body(body)
        self.headers = headers

    def encode_body(self, body: Union[bytes, Dict, str]):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        return body

class StreamMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseStreamMessageHeader
    message_type: str = "stream"
    def __init__(self, body: Union[bytes, Dict, str], headers: BaseStreamMessageHeader):
        super().__init__(body, headers)

class RequestMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseRequestMessageHeader
    message_type: str = "request"

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseRequestMessageHeader):
        super().__init__(body, headers)

class ResponseMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseResponseMessageHeader
    message_type: str = "response"

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseResponseMessageHeader):
        super().__init__(body, headers)


class UpdateMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseUpdateMessageHeader
    message_type: str = "update"

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseUpdateMessageHeader):
        super().__init__(body, headers)

class StateMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseStateMessageHeader
    message_type: str = "state"

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseStateMessageHeader):
        super().__init__(body, headers)    

class ControlRequestMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseControlRequestMessageHeader
    message_type: str = "control_request"

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseControlRequestMessageHeader):
        super().__init__(body, headers)

class ControlResponseMessageQueueMessage(BaseMessageQueueMessage):
    headers: BaseControlResponseMessageHeader
    message_type: str = "control_response"

    def __init__(self, body: Union[bytes, Dict, str], headers: BaseControlResponseMessageHeader):
        super().__init__(body, headers)