from lumi.api.websockets.base import BaseClientMessageMapper, BaseStreamClientMessageMapper
from lumi.api.communication import IntegratorMessageQueueClient, STFTMessageQueueClient, LiveDetectionMessageQueueClient
from typing import Union
import logging
from lumi.utils.common import encode_json, decode_json

class LiveDetectionStreamClientMessageMapper(BaseStreamClientMessageMapper):
    client : LiveDetectionMessageQueueClient

    async def stream_map(
        self,
        target: str,
        headers: dict,
        body: bytes
    ):
        body = decode_json(body)
        body["pattern"] = None
        if "bboxes" in body:
            for key, value in body["bboxes"].items():
                if "mask" in value: value["mask"] = None
        body = encode_json(body)

        return target, headers, body

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
                logging.warning(f"Invalid operation in IntegratorClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}")
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
                logging.warning(f"Invalid operation in STFTClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}")
                response = client.empty_response
        except Exception as e:
            logging.error(f"Error in STFTClientMessageMapper: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response