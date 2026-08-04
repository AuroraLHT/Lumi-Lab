# Autonomous-Servers

Requirements:
- Python 3.11+ (capped below 3.12 for now -- see the `detection` extra's comment in pyproject.toml)
- aio-pika
- pypylon
- numpy
- dynaconf
- fastapi
- pydantic
- openmmlab (detection node only): run `scripts/install_detection_deps.sh` on the detection host after `uv sync --all-extras`


The node configuration is stored in `nodes/settings.toml` which is automatically loaded by `dynaconf`.







