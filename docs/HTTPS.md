# HTTPS for the API and the MCP server

This guide sets up HTTPS on the **server host**, the machine that runs
`scripts/start_server_host.sh` and `scripts/start_mcp_server.sh`. When you're done:

- Lumi-Deck reaches the API at `https://<name>:8000` (and `/ws` as `wss://`).
- MCP clients (Claude Code and others) reach `https://<name>:8100/mcp`.
- Any phone, laptop or browser signed in to your Tailscale network connects with **nothing
  to install or trust on the device**. The certificate is a real Let's Encrypt one.
- The certificate **renews itself**. You never have to remember to do anything.

`<name>` is the machine's Tailscale name, for example `msewkkeb1232c.tail2c7a5d.ts.net`.

The setup takes about ten minutes, and you only do it once per server.

> **Only setting up the simulator?** Skip all of this. `scripts/start_simulation.sh`
> always runs plain HTTP on localhost.

---

## Before you start

You need:

- [ ] **Tailscale installed and signed in on the server host.** Check:
  ```bash
  tailscale status --self
  ```
  The first line should show this machine with a `100.x.y.z` address. If the command
  isn't found or says `Logged out`, install Tailscale and run `sudo tailscale up` first.
- [ ] **Admin access to your Tailscale network**, the account that manages it at
  <https://login.tailscale.com/admin>. You need this for step 1 only.
- [ ] **sudo on the server host.** You need this for step 2 only.

---

## Step 1: turn on HTTPS certificates for your Tailscale network

*Once per Tailscale network. If another lab server already did this, skip to step 2.*

1. Open <https://login.tailscale.com/admin/dns>.
2. Under **MagicDNS**, make sure it's **enabled**.
3. Under **HTTPS Certificates**, click **Enable HTTPS**.

> Tailscale will tell you that the machine names you get certificates for are published
> in a public log (Certificate Transparency). That's true of every Let's Encrypt
> certificate. It exposes the *name* (`msewkkeb1232c.tail2c7a5d.ts.net`) but not the
> machine. Only devices on your tailnet can reach it.

**Check:** on the server host,
```bash
tailscale status --self --json | grep -A2 CertDomains
```
It should list this machine's name. If it shows `null`, wait a minute and try again.

---

## Step 2: let your user fetch certificates

*Once per server host.*

By default only root may ask Tailscale for a certificate. This makes your Linux user a
Tailscale "operator", so the daily renewal (which runs as you, not root) can fetch them:

```bash
sudo tailscale set --operator=$USER
```

**Check:**
```bash
tailscale debug prefs | grep OperatorUser
```
It should print `"OperatorUser": "<your username>"`. There's only one operator per
machine; running the command again with another user replaces it.

---

## Step 3: fetch the certificate and turn on automatic renewal

*Once per server host. From the Lumi-Lab checkout:*

```bash
scripts/tailscale_cert.sh --install-cron
```

This does two things:

1. It fetches the certificate into `cfg/tls/tailscale.crt` and `cfg/tls/tailscale.key`.
   The `cfg/tls/` folder is git-ignored, and the key is readable only by you.
2. It adds one line to your crontab that runs the same script every day at 04:17 with
   `--reload`. When a renewed certificate comes out, the script tells the running API and
   MCP servers to load it (SIGHUP). There's **no restart**, and open Lumi-Deck connections
   stay up.

Expected output:
```
... crontab: daily renewal at 04:17, log .../run/production/logs/tailscale_cert.log
... certificate for msewkkeb1232c.tail2c7a5d.ts.net written to cfg/tls/tailscale.crt (notAfter=...)
```

Running it again is safe. It won't add a second crontab line, and it reports
`unchanged` if the certificate hasn't changed.

**Check:**
```bash
crontab -l | grep tailscale_cert     # the renewal line is there
ls -l cfg/tls/                        # tailscale.crt and tailscale.key exist
```

If it fails, the error says which step is missing. See [Troubleshooting](#troubleshooting).

---

## Step 4: tell the servers to use it

*Once per server host.*

Add this to `cfg/.secrets.toml`. Create the file from `cfg/.secrets.example.toml` if it
doesn't exist. It's git-ignored, so this setting stays on this machine:

```toml
[tls]
enabled = true
certfile = "cfg/tls/tailscale.crt"
keyfile = "cfg/tls/tailscale.key"
```

Then list where Lumi-Deck is served from, so the API accepts its requests. Put this in
`cfg/settings.toml`, under the existing `[api]` section. Use Lumi-Deck's Tailscale name and
port (the Vite dev server defaults to 5173):

```toml
[api]
allow_origins = ["http://<deck machine name>:5173", "http://localhost:5173"]
```

**Check:** run the pre-start checks without starting anything:
```bash
scripts/start_server_host.sh --check
scripts/start_mcp_server.sh --check
```
Both should end with `preflight passed.` and include:
```
  ok    TLS on (.../cfg/tls/tailscale.crt)
  ok    certificate valid until <date>
  ok    certificate covers <name>
```

---

## Step 5: start the servers

```bash
scripts/start_server_host.sh      # API on https://<name>:8000
scripts/start_mcp_server.sh       # MCP on https://<name>:8100/mcp
```

Each prints the address clients should use.

**Check** from any machine on the tailnet:
```bash
curl https://<name>:8000/health
```
It should return JSON, with no `--insecure` needed. If you get a certificate error, you
connected by IP or a short name. Use the full `.ts.net` name.

**You're done.** Renewal is automatic from here on.

---

## Connecting clients

Every client needs to be **on the tailnet** (the Tailscale app installed and signed in)
and to use the **full `.ts.net` name**. The certificate is valid for that name only, never
for an IP address. Nothing else is needed: no certificate to import, no settings to change.

| Client | What to do |
| --- | --- |
| **Lumi-Deck** (browser, laptop or phone) | On the login screen, enter `https://<name>:8000` |
| **Claude Code** | `claude mcp add --transport http lumi https://<name>:8100/mcp`, then sign in when the browser opens |
| **Phone** | Install the Tailscale app and sign in, then open Lumi-Deck as above |
| **A script** | Use `https://<name>:8000`. Python and Node trust the certificate as-is |

---

## Day to day

**Nothing to do.** For reference:

- **Renewal log:** `run/production/logs/tailscale_cert.log`. Each day has one line:
  `unchanged`, or `written` followed by `sent SIGHUP to ...`.
- **Certificate expiry date:** `openssl x509 -in cfg/tls/tailscale.crt -noout -enddate`.
  Certificates last 90 days and are renewed about a month before they run out.
- **Renew right now** (for example, after the server was off for weeks):
  ```bash
  scripts/tailscale_cert.sh --reload
  ```
- **The pre-start checks warn** when the certificate has less than 30 days left. That
  means the renewal isn't running. Check the log and `crontab -l`.

---

## Troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| `HTTPS certificates are not enabled for this tailnet` | Step 1 not done | Do step 1 |
| `Access denied: cert access denied` | Step 2 not done | `sudo tailscale set --operator=$USER` |
| `this machine has no MagicDNS name` | MagicDNS is off | Turn it on (step 1) |
| `tailscale is not running` | Tailscale is down on the server | `sudo tailscale up` |
| `tls.certfile ... does not exist` when starting | `[tls]` is on but step 3 wasn't run, or the paths in step 4 are wrong | Run step 3; compare the paths with `ls cfg/tls/` |
| `certificate does not cover <x>` | A `--public-url` that isn't the certificate's name | Drop `--public-url`, or use the `.ts.net` name |
| `tls.enabled is false -- serving plain HTTP` (a `FAIL`) | Step 4 not done | Do step 4. For a bench network only: `--allow-plain-http` |
| Browser: "connection is not private", or Node: `altnames` | Connected by IP or short hostname | Use the full `.ts.net` name |
| Lumi-Deck connects but every request fails (CORS error in the browser console) | Its address isn't in `api.allow_origins` | Add it (step 4) and restart the API |
| Phone can't load anything | The phone isn't on the tailnet | Open the Tailscale app and sign in |

---

## Undoing it

1. Remove the `[tls]` block from `cfg/.secrets.toml` and restart the servers. Note that
   `start_server_host.sh` then refuses to start unless you pass `--allow-plain-http`.
2. Remove the renewal: `crontab -e` and delete the `tailscale_cert.sh` line.
3. Optionally, delete `cfg/tls/tailscale.*` and clear the operator with
   `sudo tailscale set --operator=`.

---

## No Tailscale? (fallback)

`scripts/make_lab_cert.sh` creates a lab certificate authority and a certificate for the
server's LAN address. Point `[tls]` at `cfg/tls/server.crt` and `cfg/tls/server.key`
(the defaults). It works, but:

- every client must trust `cfg/tls/ca.crt` by hand: import it into the browser or OS, and
  set `NODE_EXTRA_CA_CERTS=/path/to/ca.crt` for Claude Code. That's impractical on phones;
- clients must be on the same network as the server;
- it doesn't renew itself. Rerun the script yearly; it reuses the CA, so clients don't
  need to re-trust.

---

## How it works (for maintainers)

- `src/lumi/tls.py` reads `[tls]`. Both servers pass the files to uvicorn, which serves
  TLS itself, with no proxy. If `[tls]` is on and a file is missing, they refuse to start
  rather than fall back to plain HTTP.
- On SIGHUP, a single-process server (the MCP server, or an API with `workers = 1`)
  reloads the certificate into its running TLS context. A multi-worker API restarts its
  workers instead, which is uvicorn's own behaviour. Either way, nothing is restarted by hand.
- `scripts/start_simulation.sh` exports `DYNACONF_TLS__ENABLED=false`, so a `[tls]` block
  in this machine's `.secrets.toml` never affects the simulator.
