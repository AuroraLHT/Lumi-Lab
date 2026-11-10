"""The PLD chamber driver: pld-service (the REA control service on the PLD PC) over gRPC.

    client.py       PldClient -- the gRPC calls, control taken on demand
    handler.py      PldHandler -- the `chamber.pld` capability on the bus
    monitor.py      the state/event watchers, and the growth-log CSV written from state
    convert.py      pld.v1 messages -> lumi.contracts.payloads.pld
    sim_service.py  a stand-in pld-service over the chamber simulator, for --src sim
    proto/          the vendored pld.proto and its generated stubs

This is the Pascal node's default driver; MI mode through PLD Controller remains as the
fallback behind `--use-legacy-mi-mode`.
"""
