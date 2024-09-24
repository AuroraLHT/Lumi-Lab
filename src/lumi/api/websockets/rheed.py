from lumi.api.websockets.base import BaseClientMessageMapper
from lumi.api.communication import IntegratorMessageQueueClient, STFTMessageQueueClient
from typing import Union
import logging

class IntegratorClientMessageMapper(BaseClientMessageMapper):
    client : IntegratorMessageQueueClient

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
                if headers["type"] == "register":
                    response = await client.register_bbox(int(parsed_payload["id"]), parsed_payload["bbox"])

                elif headers["type"] == "remove":
                    response = await client.remove_bbox(int(parsed_payload["id"]))

                elif headers["type"] == "cache":
                    response = await client.get_cache(int(parsed_payload["id"]))

                elif headers["type"] == "bboxes":
                    response = await client.get_bboxes()
            else:
                response = client.empty_response
        except Exception as e:
            logging.error(f"Error in IntegratorClientMessageMapper: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response
    

class STFTClientMessageMapper(BaseClientMessageMapper):
    client : STFTMessageQueueClient

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
                if headers["type"] == "register":
                    response = await client.register_bbox(int(parsed_payload["id"]))

                elif headers["type"] == "remove":
                    response = await client.remove_bbox(int(parsed_payload["id"]))

                elif headers["type"] == "cache":
                    response = await client.get_cache(int(parsed_payload["id"]))

                elif headers["type"] == "bboxes":
                    response = await client.get_bboxes()
            else:
                response = client.empty_response
        except Exception as e:
            logging.error(f"Error in IntegratorClientMessageMapper: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response