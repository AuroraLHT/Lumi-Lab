from lumi.api.websockets.base import BaseClientMessageMapper, BaseStreamClientMessageMapper
from lumi.api.communication import MIModeMessageQueueClient
from typing import Union
import logging
from lumi.utils.common import encode_json, decode_json


class MIModePubSubClientMessageMapper(BaseClientMessageMapper):
    client : MIModeMessageQueueClient

    async def map(
        self,
        operation: str,
        headers: dict,
        parsed_payload: Union[dict, str, bytes],
    ):
        response = await super().map(operation, headers, parsed_payload)

        client = self.client
        try:
            if operation == "command":
                if headers["type"] == "register_commands":
                    response = await client.register_commands(commands=parsed_payload["commands"], commands_uuid=parsed_payload["commands_uuid"])

                if headers["type"] == "list_commands":
                    response = await client.register_commands(commands=parsed_payload["commands"], commands_uuid=parsed_payload["commands_uuid"])

                elif headers["type"] == "remove":
                    response = await client.remove_bbox(int(parsed_payload["id"]))

                elif headers["type"] == "cache":
                    response = await client.get_cache(int(parsed_payload["id"]))

                elif headers["type"] == "bboxes":
                    response = await client.get_bboxes()
            else:
                logging.warning(f"Invalid operation in IntegratorClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}")
                response = client.empty_response
        except Exception as e:
            logging.error(f"Error in IntegratorClientMessageMapper: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response
    
