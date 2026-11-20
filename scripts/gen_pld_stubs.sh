#!/usr/bin/env bash
#
# Regenerate the checked-in gRPC stubs for pld-service from the vendored proto:
#
#   src/lumi/pascal/pld/proto/pld.proto  ->  pld_pb2.py, pld_pb2.pyi, pld_pb2_grpc.py
#
# Run after copying a new pld.proto over from the REA repo. The stubs are checked in so
# the Pascal node needs only the `pld` extra (grpcio, protobuf) at run time, not
# grpcio-tools. Generated with -I src, so the grpc module imports its messages as
# `lumi.pascal.pld.proto.pld_pb2` rather than a top-level `pld_pb2`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
uv run --no-sync python -m grpc_tools.protoc -I src \
    --python_out=src --pyi_out=src --grpc_python_out=src \
    src/lumi/pascal/pld/proto/pld.proto
echo "regenerated src/lumi/pascal/pld/proto/pld_pb2{.py,.pyi,_grpc.py}"
