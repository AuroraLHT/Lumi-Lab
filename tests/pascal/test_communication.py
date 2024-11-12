import pytest
from unittest.mock import AsyncMock, MagicMock
from lumi.pascal.communication import MIModeMessageQueueServer, MIModeMessageQueueClient
from lumi.pascal.mi_mode import MIModeExecution
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage
import asyncio
import logging
from lumi.utils.common import encode_json, decode_json


logging.basicConfig(level=logging.INFO)


@pytest.fixture
def mock_mimode_server():
    server = MagicMock()
    future = asyncio.Future()

    # Set the result of the future
    future.set_result(
        {
            "commands_uuid": "test-uuid",
            "is_aborted": False,
            "is_cleaned_up": True,
            "is_stopped": False,
        }
    )
    server.register_commands = MagicMock(return_value=future)

    # server.register_commands = AsyncMock(return_value={
    #     "commands_uuid": "test-uuid",
    #     "is_aborted": False,
    #     "is_cleaned_up": True,
    #     "is_stopped": False
    # })

    execution = MIModeExecution(
        commands="test",
        uuid="test-uuid",
        assist_filename="assist.txt",
        mi_folder="mi_folder",
    )
    execution.is_execution_aborted = False
    execution.is_cleaned_up = True
    execution.is_stopped = False

    execution2 = MIModeExecution(
        commands="test2",
        uuid="test-uuid2",
        assist_filename="assist.txt",
        mi_folder="mi_folder",
    )
    execution2.is_execution_aborted = False
    execution2.is_cleaned_up = True
    execution2.is_stopped = False

    server.get_latest_execution.return_value = execution
    server.get_all_executions.return_value = [execution, execution2]
    return server


@pytest.fixture
def mock_channel():
    return MagicMock(spec=AbstractChannel)


@pytest.fixture
def mock_exchange():
    return MagicMock(spec=AbstractExchange)


@pytest.fixture
def mock_message():
    message = MagicMock(spec=AbstractIncomingMessage)
    # message.body.decode.return_value = '{"commands": "test", "commands_uuid": "test-uuid"}'
    message.body = b'{"commands": "test", "commands_uuid": "test-uuid"}'
    message.headers = {"type": "execution_request"}
    return message


@pytest.mark.asyncio
async def test_handle_execution_request(
    mock_mimode_server, mock_channel, mock_exchange
):
    server = MIModeMessageQueueServer(
        mimode_server=mock_mimode_server,
        channel=mock_channel,
        exchange=mock_exchange,
        routing_key="test",
        control_routing_key="test",
        state_routing_key="test",
        server_name="test_server",
    )

    content = {"commands": "test", "commands_uuid": "test-uuid"}
    headers = {}
    body, response_headers = await server.handle_execution_request(content, headers)

    assert response_headers["type"] == "execution_result"
    assert response_headers["success"] is True
    assert body == encode_json({
        "commands_uuid": "test-uuid", 
        "is_aborted": False,
        "is_cleaned_up": True,
        "is_stopped": False
    })


@pytest.mark.asyncio
async def test_on_message_execution_request(
    mock_mimode_server, mock_channel, mock_exchange, mock_message
):
    server = MIModeMessageQueueServer(
        mimode_server=mock_mimode_server,
        channel=mock_channel,
        exchange=mock_exchange,
        routing_key="test",
        control_routing_key="test",
        state_routing_key="test",
        server_name="test_server",
    )

    body, headers = await server.on_message(mock_message)

    assert headers["type"] == "execution_result"
    assert headers["success"] is True
    assert body == encode_json(
        {
            "commands_uuid": "test-uuid",
            "is_aborted": False,
            "is_cleaned_up": True,
            "is_stopped": False,
        }
    )


@pytest.mark.asyncio
async def test_mimode_message_queue_client_request(mock_channel, mock_exchange):
    client = MIModeMessageQueueClient(
        channel=mock_channel,
        exchange=mock_exchange,
        routing_key="test",
        control_routing_key="test",
        state_routing_key="test",
        on_response_callback=AsyncMock(),
        on_state_callback=AsyncMock(),
        client_name="test_client",
        time_out=5.0,
    )

    client.request = AsyncMock(
        return_value=(
            encode_json(
                {
                    "commands_uuid": "test-uuid",
                    "is_aborted": False,
                    "is_cleaned_up": True,
                    "is_stopped": False,
                }
            ),
            {"type": "execution_result", "success": True},
        )
    )

    body, headers = await client.request(commands="test", commands_uuid="test-uuid")

    assert headers["type"] == "execution_result"
    assert headers["success"] is True
    assert body == encode_json(
        {
            "commands_uuid": "test-uuid",
            "is_aborted": False,
            "is_cleaned_up": True,
            "is_stopped": False,
        }
    )
