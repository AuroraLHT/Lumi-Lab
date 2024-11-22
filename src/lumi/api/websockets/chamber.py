from lumi.api.websockets.base import BaseClientMessageMapper, BaseStreamClientMessageMapper, BasePubSubClientMessageMapper
from lumi.api.communication import MIModeMessageQueueClient
from typing import Union
import logging
from lumi.pascal.communication import LiveChamberLogMessageQueueClient
from lumi.utils.common import encode_json, decode_json

class LiveChamberLogClientMessageMapper(BaseStreamClientMessageMapper):
    client : LiveChamberLogMessageQueueClient

class MIModePubSubClientMessageMapper(BasePubSubClientMessageMapper):
    client : MIModeMessageQueueClient

    async def map(
        self,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ):
        # response = await super().map(operation, headers, parsed_payload)
        client = self.client
        try:
            if operation == "request":
                if headers["request_type"] == "register_commands":
                    response =await client.register_commands(commands=parsed_payload["commands"], commands_uuid=parsed_payload["commands_uuid"])

                elif headers["request_type"] == "list_commands":
                    response = await client.list_execution()
            else:
                logging.warning(f"Invalid operation in MIModeMessageQueueClient: {operation}. Payload: {parsed_payload}, headers: {headers}")
        except Exception as e:
            logging.error(f"Error in MIModeMessageQueueClient: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response
        
    async def response_map(self, target: str, headers: dict, body: bytes):
        return await super().response_map(target, headers, body)    
