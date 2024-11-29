from .base import BaseClientMessageMapper, BaseStreamClientMessageMapper
from ..communication import IntegratorMessageQueueClient, STFTMessageQueueClient, LiveDetectionMessageQueueClient
from ..communication import VideoFragmentsMessageQueueClient
from typing import Union
import logging
from lumi.base.models import BaseResponseMessageHeader, BaseStreamMessageHeader
from lumi.utils.common import encode_json, decode_json

class LiveDetectionStreamClientMessageMapper(BaseStreamClientMessageMapper):
    client : LiveDetectionMessageQueueClient

    async def stream_map(
        self,
        target: str,
        headers: BaseStreamMessageHeader,
        body: bytes
    ):
        if headers["stream_type"] == "live_detection":
            body = decode_json(body)
            body["pattern"] = None
            if "bboxes" in body:
                for key, value in body["bboxes"].items():
                    if "mask" in value: value["mask"] = None
            body = encode_json(body)

        return target, headers, body

class VideoFragmentsMessageMapper(BaseClientMessageMapper):
    client : VideoFragmentsMessageQueueClient

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
                if headers["request_type"] == "initial_fragments_size":
                    response = await client.get_initial_fragments_size()
                elif headers["request_type"] == "get_video_fragment":
                    response = await client.get_video_fragment(int(parsed_payload["index"]))
                elif headers["request_type"] == "initial_fragments":
                    response = await client.get_initial_fragments()
            else:
                logging.warning(f"Invalid request in VideoFragmentsMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}")
                response = client.create_response_message(
                    body=None,
                    headers={},
                    request_type=headers["request_type"],
                    response_type="bytes",
                    succ=False,
                    error_type="UnknownRequest",
                    error_message=f"Invalid request in VideoFragmentsMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}",
                )
        except Exception as e:
            logging.error(f"Error in VideoFragmentsMessageMapper: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response
    

class IntegratorClientMessageMapper(BaseClientMessageMapper):
    client : IntegratorMessageQueueClient

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
                if headers["request_type"] == "register":
                    response = await client.register_bbox(int(parsed_payload["id"]), parsed_payload["bbox"])

                elif headers["request_type"] == "remove":
                    response = await client.remove_bbox(int(parsed_payload["id"]))

                elif headers["request_type"] == "cache":
                    response = await client.get_cache(int(parsed_payload["id"]))

                elif headers["request_type"] == "bboxes":
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
        # response = await super().map(operation, headers, parsed_payload)

        client = self.client
        try:
            if operation == "request":
                if headers["request_type"] == "register":
                    response = await client.register_bbox(int(parsed_payload["id"]))

                elif headers["request_type"] == "remove":
                    response = await client.remove_bbox(int(parsed_payload["id"]))

                elif headers["request_type"] == "cache":
                    response = await client.get_cache(int(parsed_payload["id"]))

                elif headers["request_type"] == "bboxes":
                    response = await client.get_bboxes()
            else:
                logging.warning(f"Invalid operation in STFTClientMessageMapper: {operation}. Payload: {parsed_payload}, headers: {headers}")
                response = client.empty_response
        except Exception as e:
            logging.error(f"Error in STFTClientMessageMapper: {e}. Payload: {parsed_payload}, headers: {headers}")
            raise e
        
        return response