import asyncio
from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
)
import json
from lumi.base.models import (
    BaseResponseMessageHeader,
    RequestMessageQueueMessage,
    ResponseMessageQueueMessage,
)
from lumi.utils.common import decode_json
from lumi.utils.error import get_error_info
import numpy as np
from dataclasses import dataclass

from ..base.message_queue import BasicServer, BasicClient, BasicStreamClient
from ..rheed.communication import (
    LiveCameraMessageQueueClient, 
    CameraMessageQueueClient, 
    LiveIntegratorMessageQueueClient,
)
from ..detection.communication import (
    LiveDetectionMessageQueueClient,
    DetectionMessageQueueClient,
    decode_detections,
)
from ..pascal.communication import (
    LiveChamberLogMessageQueueClient,
    ChamberLogMessageQueueClient,
)

from .record import RecorderConfig, Recorder, RecorderServer, RecorderServerConfig
from ..utils.image import decode_img

import logging
from collections.abc import Callable, Awaitable
from typing import Any, Dict, TypedDict
import time

# TODO : Move client to where the server is and we would import the client to here.
# this reduce the redundancy in code.


class OperationStatus(TypedDict):
    succ: bool
    msg: str
    error_type: str
    error_message: str


class StorageMessageQueueClient(BasicClient):
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        request_routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_state_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        client_name: str,
        time_out: float,
    ) -> None:

        super().__init__(
            channel=channel,
            exchange=exchange,
            request_routing_key=request_routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            client_name=client_name,
            time_out=time_out,
            on_state_callback=on_state_callback,
        )

    async def start_storage(
        self,
        project_name: str,
        save_frame: bool = True,
        save_ai: bool = True,
        save_log: bool = True,
        save_integration: bool = True,
        force_rewrite: bool = False,
    ):
        # request_message = self.create_request_message(
        #     body="".encode(),
        #     headers={
        #         "save_frame": save_frame,
        #         "save_ai": save_ai,
        #         "save_log": save_log,
        #         "save_integration": save_integration,
        #         "project_name": project_name,
        #         "force_rewrite": force_rewrite,
        #     },
        #     request_type="start",
        # )
        request_message = self.create_request_message(
            body={
                "save_frame": save_frame,
                "save_ai": save_ai,
                "save_log": save_log,
                "save_integration": save_integration,
                "project_name": project_name,
                "force_rewrite": force_rewrite,
            },
            headers={},
            request_type="start",
        )

        return await self.request(request_message)

    async def end_storage(self):
        request_message = self.create_request_message(
            body="".encode(),
            headers={},
            request_type="end",
        )
        return await self.request(request_message)


@dataclass
class StorageMessageQueueServerConfig:
    root_folder: str
    initial_size: int = 1000

class StorageMessageQueueServer(BasicServer):
    recorder: Recorder
    recorder_config: RecorderConfig
    recorder_server: RecorderServer
    recorder_server_config: RecorderServerConfig

    def __init__(
        self,
        camera_client: CameraMessageQueueClient,
        log_client: ChamberLogMessageQueueClient,
        detector_client: DetectionMessageQueueClient,
        live_integrator_client: LiveIntegratorMessageQueueClient,
        live_camera_client: LiveCameraMessageQueueClient,
        live_detection_client: LiveDetectionMessageQueueClient,
        live_log_client: LiveChamberLogMessageQueueClient,
        config: StorageMessageQueueServerConfig,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        request_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            request_routing_key=request_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )

        self.camera_client = camera_client
        self.log_client = log_client
        self.detector_client = detector_client
        self.live_integrator_client = live_integrator_client
        self.live_camera_client = live_camera_client
        self.live_detection_client = live_detection_client
        self.live_log_client = live_log_client

        self.config = config

        self.state["is_storing"] = False
        self.state["is_storing_frame"] = False
        self.state["is_storing_ai"] = False
        self.state["is_storing_log"] = False
        self.state["is_storing_integration"] = False

        self.recorder_server = None
        self.recorder_server_config = None

    async def on_message(
        self, message: AbstractIncomingMessage
    ) -> ResponseMessageQueueMessage:
        body, headers = message.body, message.headers
        request_type = headers["request_type"]

        if request_type == "start":
            response = await self.start_storages(body, headers)

        elif request_type == "end":
            response = await self.end_storages(body, headers)
        else:
            response = self.create_response_message(
                body="",
                headers={},
                request_type=request_type,
                response_type=request_type,
                succ=False,
                error_type="UnknownRequestError",
                error_message=f"unknown request [{request_type}]",
            )
        return response

    async def _get_frame_metadata(
        self,
    ) -> OperationStatus:
        status: OperationStatus = {
            "succ": True,
            "msg": "Get frame metadata success",
            "error_type": "",
            "error_message": "",
        }
        try:
            camera_state = await self.camera_client.get_state()

            frame_dim = camera_state["frame_dims"]
            frame_metas_columns = camera_state["frame_metas"]
        except Exception as e:
            status["succ"] = False
            status["error_type"] = "MetadataObtainError"
            status["error_message"] = str(e)
            return None, None, status

        if len(frame_dim) < 2:
            status["succ"] = False
            status["msg"] = "Get frame metadata failed"
            status["error_type"] = "InvalidFrameDimError"
            status["error_message"] = "frame_dim is not valid"

        if frame_metas_columns is None:
            status["succ"] = False
            status["msg"] = "Get frame metadata failed"
            status["error_type"] = "InvalidFrameMetasColumnsError"
            status["error_message"] = "frame_metas_columns is not valid"

        return frame_dim, frame_metas_columns, status

    async def _get_log_metadata(
        self,
    ) -> OperationStatus:
        status: OperationStatus = {
            "succ": True,
            "msg": "Get log metadata success",
            "error_type": "",
            "error_message": "",
        }
        try:
            log_state = await self.log_client.get_state()
            log_columns = log_state["entries"]
        except Exception as e:
            status["succ"] = False
            status["msg"] = "Get log metadata failed"
            status["error_type"] = "MetadataObtainError"
            status["error_message"] = str(e)
            return None, status

        if log_columns is None or len(log_columns) == 0:
            status["succ"] = False
            status["msg"] = "Get log metadata failed"
            status["error_type"] = "InvalidLogColumnsError"
            status["error_message"] = "log_columns is not valid"

        return log_columns, status

    async def _get_ai_metadata(
        self,
    ) -> OperationStatus:
        status: OperationStatus = {
            "succ": True,
            "msg": "Get ai metadata success",
            "error_type": "",
            "error_message": "",
        }
        try:
            detection_state = await self.detector_client.get_state()
            pattern_dim = detection_state["pattern_dim"]
            detection_meta_columns = detection_state["detection_metas"]
            classifier_classes = detection_state["classifier_classes"]
            detector_classes = detection_state["detector_classes"]
        except Exception as e:
            status["succ"] = False
            status["msg"] = "Get ai metadata failed"
            status["error_type"] = "MetadataObtainError"
            status["error_message"] = str(e)
            return None, None, None, status

        if pattern_dim is None or len(pattern_dim) < 2:
            status["succ"] = False
            status["msg"] = "Get ai metadata failed"
            status["error_type"] = "InvalidPatternDimError"
            status["error_message"] = "pattern_dim is not valid"

        if detection_meta_columns is None or len(detection_meta_columns) == 0:
            status["succ"] = False
            status["msg"] = "Get ai metadata failed"
            status["error_type"] = "InvalidDetectionMetaColumnsError"
            status["error_message"] = "detection_meta_columns is not valid"

        if classifier_classes is None or len(classifier_classes) == 0:
            status["succ"] = False
            status["msg"] = "Get ai metadata failed"
            status["error_type"] = "InvalidClassifierClassesError"
            status["error_message"] = "classifier_classes is not valid"

        return pattern_dim, detection_meta_columns, classifier_classes, detector_classes, status

    async def _check_live_clients(self, options: Dict[str, Any]) -> OperationStatus:
        async def check_live_client(client: BasicStreamClient) -> bool:
            state = await client.get_state()
            return state["is_streaming"]

        status: OperationStatus = {
            "succ": True,
            "msg": "",
            "error_type": "",
            "error_message": "",
        }
        not_active_clients = []
        missing_options = []
        check_lists = [
            ("save_frame", self.live_camera_client, "live_camera"),
            ("save_log", self.live_log_client, "live_log"),
            ("save_ai", self.live_detection_client, "live_detection"),
            ("save_integration", self.live_integrator_client, "live_integrator"),
        ]

        for save_type, client, name in check_lists:
            # make the headers backward compatible
            if save_type not in options: 
                options[save_type] = False
                missing_options.append(save_type)

            if options[save_type] and not await check_live_client(client):
                status["succ"] = False
                not_active_clients.append(name)


        if status["succ"]:
            status["msg"] = "All live clients are streaming"
        else:
            status["msg"] = (
                f"One or more live clients are not streaming: {not_active_clients}"
            )
            status["error_type"] = "LiveClientNotStreamingError"
            status["error_message"] = "One or more live clients are not streaming"

        if len(missing_options) > 0:
            status["msg"] += f" Missing options: {missing_options}."

        return status

    async def _create_storages(self, options: dict, headers: BaseResponseMessageHeader):

        frame_dim, frame_metas_columns, frame_status = await self._get_frame_metadata()
        if options["save_frame"] and not frame_status["succ"]:
            return frame_status

        log_columns, log_status = await self._get_log_metadata()
        if options["save_log"] and not log_status["succ"]:
            return log_status

        pattern_dim, detection_meta_columns, classifier_classes, detector_classes, ai_status = (
            await self._get_ai_metadata()
        )
        if options["save_ai"] and not ai_status["succ"]:
            return ai_status
        
        # live oscillation do not need meta check

        self.recorder_config = RecorderConfig(
            project_name=options["project_name"],
            root_folder=self.config.root_folder,
            frame_dim=frame_dim,
            frame_meta_columns=frame_metas_columns,
            log_columns=log_columns,
            pattern_dim=pattern_dim,
            detection_meta_columns=detection_meta_columns,
            classifier_classes=classifier_classes,
            detector_classes=detector_classes,
            initial_size=self.config.initial_size,
            save_frame=options["save_frame"] if "save_frame" in options else False,
            save_log=options["save_log"] if "save_log" in options else False,
            save_ai=options["save_ai"] if "save_ai" in options else False,
            save_integration=options["save_integration"] if "save_integration" in options else False,
            frame_speed_limit=5, # TODO: make this configurable
            detection_speed_limit=5, # TODO: make this configurable
            force_rewrite=options["force_rewrite"],
            # compression="gzip",
            # compression_opts=4,
            compression="lzf",
            compression_opts=None,
            scaleoffset=0,
        )

        # TODO: make custom exception for recorder
        try:
            self.recorder = Recorder(config=self.recorder_config)
        except FileExistsError as e:
            error_info = get_error_info(e)
            logging.error(f"{self.log_prefix} {error_info}")
            return {
                "succ": False,
                "msg": str(e),
                "error_type": "FileExistsError",
                "error_message": str(e),
            }

        self.recorder_server_config = RecorderServerConfig() # use the default config that controlled by settings.toml
        self.recorder_server = RecorderServer(
            self.recorder_server_config, self.recorder, name="recoder_server"
        )
        create_status = self.recorder_server.create_datasets()
        if not create_status["succ"]:
            return {
                "succ": False,
                "msg": f"Fail to create datasets {create_status['failed_datasets']}",
                "error_type": "DatasetsCreationError",
                "error_message": create_status["error_message"],
            }
        self.recorder_server.start()

        return {
            "succ": True,
            "msg": "Create storages success",
            "error_type": "",
            "error_message": "",
        }

    async def _start_individual_storages(self, options):
        status = {
            "succ": True,
            "msg": "Start storages success",
            "error_type": "",
            "error_message": "",
        }
        starting_storage = ""
        try:
            if "save_frame" in options and options["save_frame"]:
                starting_storage = "frame"
                await self._start_frame_storage()
                self.state["is_storing_frame"] = True

            if "save_log" in options and options["save_log"]:
                starting_storage = "log"
                await self._start_log_storage()
                self.state["is_storing_log"] = True

            if "save_ai" in options and options["save_ai"]:
                starting_storage = "ai"
                await self._start_ai_storage()
                self.state["is_storing_ai"] = True

            if "save_integration" in options and options["save_integration"]:
                starting_storage = "integration"
                await self._start_integration_storage()
                self.state["is_storing_integration"] = True

            self.state["is_storing"] = True
        except Exception as e:
            logging.error(f"{self.log_prefix} Fail to start storage {starting_storage}: {e}")
            status["succ"] = False
            status["msg"] = f"Fail to start storage {starting_storage}"
            status["error_type"] = "StartStorageError"
            status["error_message"] = str(e)

        return status

    async def _close_storages(self):
        if self.recorder_server is not None:
            self.recorder_server.close_storages()
            self.recorder_server.join()

    async def start_storages(
        self, body, headers: BaseResponseMessageHeader
    ) -> ResponseMessageQueueMessage:
        payload = decode_json(body)
        logging.info(f"{self.log_prefix} start storage response with args {payload}")
        def create_response(
            status: OperationStatus, headers: BaseResponseMessageHeader
        ):
            response = self.create_response_message(
                body=status["msg"].encode(),
                headers={},
                request_type=headers["request_type"],
                response_type=headers["request_type"],
                succ=status["succ"],
                error_type=status["error_type"],
                error_message=status["error_message"],
            )
            return response

        if not self.state["is_storing"]:
            live_status = await self._check_live_clients(payload)
            if not live_status["succ"]:
                response = create_response(live_status, headers)
                return response

            status = await self._create_storages(payload, headers)
            if not status["succ"]:
                response = create_response(status, headers)
                return response

            status = await self._start_individual_storages(payload)
            if not status["succ"]:
                response = create_response(status, headers)
                return response

            status = {
                "succ": True,
                "msg": "Start storage success",
                "error_type": "",
                "error_message": "",
            }
            response = create_response(status, headers)
            return response

        else:
            status = {
                "succ": False,
                "msg": "Storage already running",
                "error_type": "StorageAlreadyRunningError",
                "error_message": "Storage already running",
            }
            response = create_response(status, headers)
            return response

    async def _start_frame_storage(self):
        async def on_response_callback(message: AbstractIncomingMessage):
            try:
                body, headers = message.body, message.headers
                frame, frame_headers = decode_img(body, headers)
                self.recorder_server.save_frame(frame, frame_headers)
                return False

            except Exception as e:
                logging.error(e)
                return True

        self.live_camera_client.update_on_reponse_callback(
            on_response_callback=on_response_callback
        )
        await self.live_camera_client.start_main(start_consume_loop=True)

        # add a check to see if the live camera is streaming, if not, start the server streaming
        state = await self.live_camera_client.get_state()
        if not state["is_streaming"]:
            await self.live_camera_client.start_server_streaming()

        logging.info(f"{self.log_prefix} starts frame storage.")

    async def _start_log_storage(self):
        async def on_response_callback(message: AbstractIncomingMessage):
            try:
                body, headers = message.body, message.headers
                chamber_log = json.loads(body)
                # print("chamber log", chamber_log)
                self.recorder_server.save_log(chamber_log=chamber_log)
                return False

            except Exception as e:
                logging.error(e)
                return True

        self.live_log_client.update_on_reponse_callback(
            on_response_callback=on_response_callback
        )
        await self.live_log_client.start_main(start_consume_loop=True)
        logging.info(f"{self.log_prefix} starts log storage.")

    async def _start_integration_storage(self):
        async def on_response_callback(message: AbstractIncomingMessage):
            try:
                body, headers = message.body, message.headers
                integrations = json.loads(body)
                self.recorder_server.save_integrations(integrations=integrations, integrations_meta=headers)
                return False

            except Exception as e:
                logging.error(e)
                return True

        self.live_integrator_client.update_on_reponse_callback(
            on_response_callback=on_response_callback
        )
        await self.live_integrator_client.start_main(start_consume_loop=True)
        logging.info(f"{self.log_prefix} starts integration storage.")

    async def _start_ai_storage(self):
        async def on_response_callback(message: AbstractIncomingMessage):
            try:
                body, headers = message.body, message.headers

                result, result_headers = decode_detections(body, headers)
                pattern = result["pattern"]["pattern"]
                n_detections = len(result["bboxes"].keys())
                if n_detections > 0:
                    masks = np.stack(
                        [
                            result["bboxes"][f"{i}"]["mask"]["mask"]
                            for i in range(n_detections)
                        ]
                    )
                    bboxes = np.stack(
                        [result["bboxes"][f"{i}"]["bbox"] for i in range(n_detections)]
                    )
                    labels = np.stack(
                        [result["bboxes"][f"{i}"]["label"] for i in range(n_detections)]
                    )
                    scores = np.stack(
                        [result["bboxes"][f"{i}"]["score"] for i in range(n_detections)]
                    )
                else:
                    masks = np.array([])
                    bboxes = np.array([])
                    labels = np.array([])
                    scores = np.array([])

                cls_result = result["classification"]
                tracking = result["region2tracks"]

                self.recorder_server.save_prediction(
                    pattern=pattern,
                    masks=masks,
                    bboxes=bboxes,
                    labels=labels,
                    scores=scores,
                    cls_result=cls_result,
                    tracking=tracking,
                    detection_meta=result_headers,
                )
                return False
            except Exception as e:
                logging.error(e)
                return True

        self.live_detection_client.update_on_reponse_callback(
            on_response_callback=on_response_callback
        )
        await self.live_detection_client.start_main(start_consume_loop=True)
        logging.info(f"{self.log_prefix} starts ai storage.")

    async def _end_log_storage(self):
        await self.live_log_client.stop_main()
        self.state["is_storing_log"] = False
        logging.info(f"{self.log_prefix} ends log storage.")

    async def _end_ai_storage(self):
        await self.live_detection_client.stop_main()
        self.state["is_storing_ai"] = False
        logging.info(f"{self.log_prefix} ends ai storage.")

    async def _end_frame_storage(self):
        await self.live_camera_client.stop_main()
        self.state["is_storing_frame"] = False
        logging.info(f"{self.log_prefix} ends frame storage.")

    async def _end_integration_storage(self):
        await self.live_integrator_client.stop_main()
        self.state["is_storing_integration"] = False
        logging.info(f"{self.log_prefix} ends integration storage.")

    async def end_storages(
        self, body: bytes, headers: BaseResponseMessageHeader
    ) -> ResponseMessageQueueMessage:
        if self.state["is_storing"]:
            await self._end_frame_storage()
            await self._end_log_storage()
            await self._end_ai_storage()
            await self._end_integration_storage()
            self.state["is_storing"] = False

            await self._close_storages()
            logging.info(f"{self.log_prefix} ends storage")
            response = self.create_response_message(
                body="End storage success".encode(),
                headers={},
                request_type=headers["request_type"],
                response_type=headers["request_type"],
                succ=True,
                error_type="",
                error_message="",
            )

        else:
            response = self.create_response_message(
                body=f"Cannot end storage, the storage process have been termined.",
                headers={},
                request_type=headers["request_type"],
                response_type=headers["request_type"],
                succ=False,
                error_type="StorageTerminationError",
                error_message=f"Cannot end storage, the storage process have been termined.",
            )
        return response