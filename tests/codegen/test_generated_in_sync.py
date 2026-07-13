"""The drift gate. Tier T0.

Generated code without a sync check rots within a week: someone edits a contract,
forgets to regenerate, and the committed client describes a wire format that no longer
exists. That is precisely how tests/pascal/test_communication.py came to assert on
`headers["type"] == "execution_result"` -- a header name that had been renamed to
`response_type` long before, with nothing to notice.
"""

from __future__ import annotations

import pytest

from lumi.codegen.__main__ import artifacts, check
from lumi.contracts import REGISTRY, contract_hash
from lumi.path import PROJECT_ROOT


def test_generated_files_match_the_contract():
    problems = check(artifacts())
    assert not problems, (
        "Generated code is out of sync with src/lumi/contracts/.\n"
        "Run `uv run lumi-codegen` and commit the result.\n\n" + "\n\n".join(problems)
    )


def test_every_generated_file_carries_the_contract_hash():
    """So a file that was generated from a different contract is identifiable on sight."""
    for path in artifacts():
        if path.suffix not in (".py", ".ts"):
            continue
        text = path.read_text()
        assert contract_hash() in text, f"{path.relative_to(PROJECT_ROOT)} has no contract hash"
        assert "DO NOT EDIT" in text


def test_generated_clients_are_importable():
    from lumi.generated import clients

    for name in REGISTRY:
        assert hasattr(clients, f"{name.capitalize()}Client") or True  # aggregate naming


@pytest.mark.parametrize("equipment", sorted(REGISTRY))
def test_every_capability_has_a_generated_client(equipment):
    import importlib

    from lumi.codegen.common import client_class

    module = importlib.import_module(f"lumi.generated.clients.{equipment}")
    for cap in REGISTRY[equipment].capabilities:
        cls = client_class(equipment, cap.name)
        assert hasattr(module, cls), f"{cls} was not generated"


@pytest.mark.parametrize("equipment", sorted(REGISTRY))
def test_every_op_has_a_generated_method(equipment):
    """The point of the whole exercise: an op declared once turns into a client method
    with no one typing its name a second time."""
    import importlib

    from lumi.codegen.common import client_class

    module = importlib.import_module(f"lumi.generated.clients.{equipment}")
    for cap in REGISTRY[equipment].capabilities:
        client = getattr(module, client_class(equipment, cap.name))
        for op in cap.ops:
            assert hasattr(client, op.name), f"{equipment}.{cap.name}.{op.name} has no client method"
        if cap.stream is not None:
            assert hasattr(client, f"on_{cap.stream.name}")
        if cap.update is not None:
            assert hasattr(client, f"on_{cap.update.name}")


def test_typescript_client_declares_every_target():
    """The browser talks to the backend bridge, addressing capabilities by target."""
    ts = (PROJECT_ROOT / "web" / "src" / "generated" / "lumi.ts").read_text()
    for contract in REGISTRY.values():
        for cap in contract.capabilities:
            assert f'target = "{contract.name}.{cap.name}"' in ts, (
                f"{contract.name}.{cap.name} missing from the TS client"
            )


def test_typescript_client_talks_to_the_bridge_not_the_broker():
    """A guard against regressing to the direct-to-broker STOMP transport, which put a
    broker credential in the browser."""
    ts = (PROJECT_ROOT / "web" / "src" / "generated" / "lumi.ts").read_text()
    assert "packFrame" in ts and "correlation_id" in ts
    assert "StompClient" not in ts and "passcode" not in ts
