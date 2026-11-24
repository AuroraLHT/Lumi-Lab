# Starting the production stack

Two machines:

- **Keck lab PLD PC** (Windows, ENMAWDREN033081): the *instrument host*.
  pld-service (REA), the `pascal` node (chamber driver, growth log, chamber webcam)
  and the `rheed` node (Basler camera).
- **Office workstation** (Linux, `msewkkeb1232c.tail2c7a5d.ts.net`): the *server
  host*. The RabbitMQ broker, the `monitor`, `storage`, `detection`, `experiment`,
  `simulation` and `api` nodes, and the MCP server.

Start the workstation first: its storage node declares the exchanges the lab PC
publishes into.

## Before the first start (once per release)

1. **The same code on both machines.** The RHEED gun, agent boxes and pld target
   names are on `rheed-ops` (PR #38, on top of #36 and #37), not yet on `main`.
   Merge them, or check out the same commit on both machines. Both launchers
   print the contract hash; the two must match (now `bcbbcfdf74b1d7aa`).
2. **The Deck built for the same contract.** The browser client is generated
   from the contract. A mismatch gives wrong or refused ops at runtime, not a
   startup error.
3. **pld-service on the lab PC:**
   - It must be new enough to have `GetSettings` and `Motion.sample_too_low`
     (Lumi vendors REA's proto at c7de330). On an older one, target lookups
     (`show_available_targets`, `get_current_target`, and each layer's material)
     fail.
   - Its `target_names` section must list what is really in slots A–F. Lumi
     names every layer from it, and PLDconfig.ini is no longer read.
   - Its `[rheed]` caps (filament 2200 mA, HV 28 kV) take effect at its next
     start.
4. **Broker accounts on the workstation broker.** The lab PC needs a node
   account:
   ```bash
   uv run python scripts/apply_broker_permissions.py --host localhost \
       --user lumi-node --role node --password <pw>
   ```
   Re-run it for every operator or viewer account after a contract change, since
   their permissions list each op. Node accounts are unrestricted, so they don't
   need it. The browser and MCP server check permissions in-process and need
   nothing.
5. **`cfg/settings.toml` on each machine:**
   - It's machine-local and git-ignored, so start it from
     `cfg/settings.example.toml`.
   - An older copy still works: a missing `[experiment.rheed]` key takes the
     code's default, the lab's gun values. An unknown key is refused.
   - On the lab PC, set `[rabbitmq] host` in `cfg/.secrets.toml` to the
     workstation's address as the lab PC reaches it, or pass `-BrokerHost`. Port
     5672 must be open from the lab PC to the workstation.

## Workstation (server host)

```bash
cd ~/codebase/Lumi-Lab
scripts/stop_nodes.sh              # the dev/twin stack shares this broker and port 8100
scripts/start_server_host.sh --check
scripts/start_server_host.sh       # terminal 1; Ctrl-C stops every node
scripts/start_mcp_server.sh        # terminal 2; https://msewkkeb1232c.tail2c7a5d.ts.net:8100/mcp
```

- **Stop the dev stack first.** Dev nodes and production nodes on one broker are
  competing consumers: two experiment or pascal nodes take turns answering RPCs.
  The dev twin (rea-twin and the twin pld-service) can stay up; nothing in
  production talks to it.
- **What `--check` covers (it passed on 2026-11-23):** the venv extras, the
  broker, auth (a real key and 2 accounts), TLS (the Tailscale certificate is
  valid until 2026-12-31 and renewed daily by cron), the HDF5 root (`database` →
  `/mnt/FastTerp/Data/UMD/Lumi-RHEED`, writable), the detection models, the
  experiment config and gemmi.
- **Logs:** `run/production/logs/`.

## Keck lab PLD PC (instrument host)

1. **pld-service:** a person at the chamber starts it, the real backend
   (`pld-service --hardware --allow-operations`, by REA's procedure). It listens on
   127.0.0.1:50051 only.
2. **Lumi** (PowerShell, in the Lumi-Lab checkout):
   ```powershell
   uv sync --extra pascal --extra camera --extra pld
   scripts\start_instrument_host.ps1 -BrokerHost <workstation address> `
       -User lumi-node -Password <pw> -Log 'D:\...\growth_log' -Check
   scripts\start_instrument_host.ps1 -BrokerHost <workstation address> `
       -User lumi-node -Password <pw> -Log 'D:\...\growth_log'
   ```
   - **What `-Check` covers:** the extras, the broker and its account, the
     growth-log folder, pld-service (up, read-only or not, who holds control),
     the Basler camera, and nodes already running.
   - **PLDconfig.ini is not needed.**
   - **Stopping:** Ctrl-C hard-kills both nodes (Windows has no SIGTERM).
   - **Logs:** `run\production\logs\`, with stderr in `*.err.log`.

## After both are up

- **Deck presence:** the presence panel lists every node.
- **Chamber:** `chamber.pld.chamber_state` shows `connected`, `backend =
  HARDWARE`, and the controller you expect.
- **Targets:** `chamber.pld.get_settings` (or `show_available_targets`) lists the
  real targets.
- **Camera:** `rheed.camera.image` returns a frame.
- **Gun:** `rheed_on` starts at 25 kV and 1800 mA, with the agent judging the
  saturation.
