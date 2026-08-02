# Frontend migration guide

**For the sister frontend repo.** Branch: `worktree-refactor-contract-nodes`.

Every HTTP route and websocket the frontend currently uses has been **deleted**. There is
now one websocket endpoint and a **generated TypeScript client**. This guide maps the old
surface onto the new one and calls out the two changes that need real work rather than a
find-and-replace: **binary payload decoding** and **role-based permissions**.

---

## 1. What replaced what

The backend's only two jobs now are *authenticate* and *bridge to the message bus*. There
are no per-feature routes, and none are generated — a capability added to the contract is
reachable from the browser with no backend change.

| Gone | Now |
| --- | --- |
| `src/lumi/api/routes/{rheed,chamber,storage,nodes}.py` | `src/lumi/api/bridge.py` (one generic handler) |
| `src/lumi/api/websockets/*` | same bridge, one `/ws` endpoint |
| `src/lumi/api/{communication,connection,models,lifespan,utils}.py` | `src/lumi/base/mq` + `src/lumi/contracts` |
| hand-written frontend API layer | `web/src/generated/lumi.ts` (generated, **do not edit**) |

Endpoints that still exist: `GET /health` (liveness + list of capabilities) and
`WS /ws` (everything else).

### Endpoint → client-call mapping

**RHEED camera**

| Old | New |
| --- | --- |
| `GET /RHEED/image` | `RheedCameraClient.image()` → `{ meta: ImageMeta, payload }` |
| `GET /RHEED/camera/config` | `RheedCameraClient.get_camera_config()` |
| `POST /RHEED/camera/config` | `RheedCameraClient.update_camera_config(cfg)` |
| `GET /RHEED/camera/live/state` (SSE) | `RheedCameraClient.getState()` |
| frame stream on `WS /RHEED/data/live` | `RheedCameraClient.onFrame(cb)` + `startStreaming()` |

**RHEED video**

| Old | New |
| --- | --- |
| `GET /RHEED/video/live/state` (SSE) | `RheedVideoClient.getState()` |
| initial fragments on `WS /RHEED/data/live` | `RheedVideoClient.initial_fragments()`, `.initial_fragments_size()` |
| fragment fetch on `WS /RHEED/data/live` | `RheedVideoClient.video_fragment({ fragment_idx })` |
| fragment stream on `WS /RHEED/data/live` | `RheedVideoClient.onFragment(cb)` + `startStreaming()` |

**RHEED analysis** (old `WS /RHEED/analysis/live` carried detection + integrator + STFT on one socket; they are now three clients on the shared transport)

| Old | New |
| --- | --- |
| `GET /RHEED/detection/live/state` | `DetectionDetectionClient.getState()` |
| detection stream | `DetectionDetectionClient.onDetection(cb)` — **operator only**, see §4 |
| — (new) | `DetectionOverlayClient.onOverlay(cb)` — boxes only, viewer-safe |
| — (new) | `DetectionDetectionClient.detection()`, `.set_detection_crop(crop)` |
| `GET /RHEED/integrator/live/state` | `RheedIntegratorClient.getState()` |
| integrator register/remove/bboxes/cache | `RheedIntegratorClient.register(…)`, `.remove(…)`, `.bboxes()`, `.cache(…)` |
| integrator stream | `RheedIntegratorClient.onIntegration(cb)` |
| `GET /RHEED/stft/live/state` | `RheedStftClient.getState()` |
| stft register/remove/bboxes/cache | `RheedStftClient.register(…)`, `.remove(…)`, `.bboxes()`, `.cache(…)` |
| stft stream | `RheedStftClient.onStft(cb)` |

**Chamber** (old `WS /chamber/live` carried log + MI mode)

| Old | New |
| --- | --- |
| `GET /chamber/log` | `ChamberLogClient.log()` |
| `GET /chamber/log/live/state` | `ChamberLogClient.getState()` |
| log stream | `ChamberLogClient.onLog(cb)` + `startStreaming()` |
| `GET /chamber/mi/live/state` | `ChamberMiModeClient.getState()` |
| MI register/list on `WS /chamber/live` | `ChamberMiModeClient.register_commands(…)`, `.list_execution()` |
| MI execution updates | `ChamberMiModeClient.onExecution(cb)` |
| — (new) | `ChamberConfigClient.get_all_config()`, `.get_config()`, `.get_configs_by_section()`, `.get_sections()` |
| — (new) | `ChamberCameraClient` — same shape as `RheedCameraClient` |

**Storage / system**

| Old | New |
| --- | --- |
| `POST /storage/start` | `StorageStorageClient.start_recording(req)` |
| `POST /storage/end` | `StorageStorageClient.stop_recording()` |
| — (new) | `StorageStorageClient.getState()` |
| `GET /nodes/state` (SSE) | `SystemRegistryClient.list_nodes()` / `.getState()` |
| — (new) | `SystemRegistryClient.get_node(…)`, `.wait_for(…)`, `.onRegistryEvent(cb)` |
| — (new) | `SystemSupervisorClient.list_hosts()`, `.spawn()`, `.kill()`, `.restart()` |

### New capability worth building UI for

`SystemRegistry` gives **live node presence** — nodes are heartbeat-detected, so the UI can
finally show what is actually up, and `onRegistryEvent` pushes join/leave as it happens.
`SystemSupervisor` can spawn/kill/restart node processes per host. Neither had an
equivalent before. (Both are permission-gated; see §4 — and note the supervisor's
*mutating* ops are not usable from any browser role today, so build that view read-only
for now.)

---

## 2. Getting the client

`web/src/generated/lumi.ts` is generated from the pydantic contract registry
(`src/lumi/contracts/`) by `uv run lumi-codegen`, and CI checks it is current with
`uv run lumi-codegen --check`. It carries a `contract_hash` in its header.

**Do not hand-edit it, and do not hand-roll a client against the wire protocol.** When the
backend contract changes, regenerate and the types change with it — that is the whole point
of the refactor. Copy or symlink the file into the frontend repo (or publish it as a small
package) and pull it forward on each backend change.

---

## 3. Connecting

One websocket, one transport object, typed clients on top:

```ts
import { LumiTransport, RheedCameraClient, DetectionOverlayClient } from "./generated/lumi";

const t = new LumiTransport(`ws://${host}/ws?token=${token}`);
await t.connect();

const cam = new RheedCameraClient(t);

// request/response
const cfg = await cam.get_camera_config();          // CameraConfig
await cam.update_camera_config({ exposure_time: 5000 });

// binary request/response
const { meta, payload } = await cam.image();        // meta: ImageMeta, payload: Uint8Array

// streams
await cam.startStreaming();
const key = cam.onFrame((meta: ImageMeta, payload: Uint8Array) => draw(meta, payload));
// later
cam.unsubscribe(key);
await cam.stopStreaming();
```

Notes:

- **One transport, many clients.** Construct `LumiTransport` once and share it; every
  capability client multiplexes over the same socket. RPCs correlate by id; streams are
  dispatched per `(target, stream)`.
- **RPC timeout** defaults to 10s (`new LumiTransport(url, timeoutMs)`); a timeout rejects
  the promise.
- **Reconnection is not implemented** in the generated transport. If the socket drops, the
  frontend must reconnect and re-subscribe (re-issue `onX(...)`); pending RPCs will not be
  retried. Worth wrapping `LumiTransport` in a small reconnect/resubscribe layer on the
  frontend side.
- `startStreaming()` / `stopStreaming()` toggle the producing node's stream, and the flag
  is **server-wide**, not per-client — see the caveat in §4.

---

## 4. Auth and permissions — the UI must handle `Forbidden`

Auth is a `?token=` query param on the websocket URL, resolved by `src/lumi/api/auth.py`
into an `Identity(user, role)` from `api.tokens` in settings. This is a deliberately
replaceable seam — a real login system (session cookie, OAuth, LDAP) plugs in there — so
expect the token mechanism to change; the *role* model is the stable part.

- Rejected connection → the socket is closed with code **4401**. Handle it as "not logged
  in", not as a network blip.
- If `api.allow_anonymous_viewer` is set, an unauthenticated connection gets `viewer`
  (read-only). It **never** defaults to `operator`.

Roles are derived from the contract in `src/lumi/contracts/policy.py` and are
**default-deny**: an op is a mutation unless it is explicitly listed read-only. The same
predicate (`policy.permits`) gates the bridge and the broker, so they cannot drift.

**What a `viewer` may do:** watch streams, call read-only ops (`image`, `get_*`, `log`,
`bboxes`, `cache`, `list_nodes`, …), and `start`/`stop`/`state` on streams.

**What a `viewer` may NOT do** — these reject with `error_type: "Forbidden"`:

- `update_camera_config` (RHEED and chamber)
- integrator/STFT `register` / `remove`
- `set_detection_crop`
- `start_recording` / `stop_recording`
- MI `register_commands`
- supervisor `spawn` / `kill` / `restart` — an **`operator` cannot do these either**. The
  code comments call these "admin", but see the warning below: **no role can currently
  perform them from the browser.**

> **Backend bug — supervisor mutations are unreachable, and an `admin` token hangs.**
> `auth.py` accepts a role of `admin`, but `policy.ROLES` defines only `viewer`, `operator`
> and `node`, so `permits("admin", …)` raises `KeyError` inside the bridge. The bridge
> swallows it and sends **no frame back at all**, so the browser's RPC hangs until its 10s
> timeout rather than failing cleanly. Net effect for the frontend: (a) do not issue `admin`
> tokens yet, and (b) `spawn`/`kill`/`restart` cannot be driven from any browser role today
> — build the supervisor UI read-only (`list_hosts`) until the backend adds a real `admin`
> role. Flagged to the backend; this should be fixed there, not worked around here.

**And one that is not obvious:** a `viewer` **cannot subscribe to the `detection.detection`
stream at all** — that is the heavy NPZ stream carrying the pattern and the masks, and it is
excluded from the viewer's read set. A viewer-role UI must render boxes from
`DetectionOverlayClient.onOverlay()` instead. `DetectionOverlay` exists precisely so the
viewer UI has a lightweight, safe source for overlays. Plan the detection view around
overlay-by-default, and light up the full detection stream only for an operator.

Practically: **the frontend needs the current user's role** to decide what to render as
enabled. Errors surface as rejected promises (`[Forbidden] role 'viewer' may not …`), so a
failed mutation is catchable — but discovering permissions by firing requests and catching
errors is a poor UX. Gate the controls on role up front.

### Known caveat: viewers can stop a stream for everyone

The stream on/off flag is **server-wide** (one flag for all subscribers), so a `viewer`
calling `stopStreaming()` stops the live camera/video feed for *every* connected user, not
just themselves. This was deferred deliberately as nuisance-level (a viewer still cannot
read or mutate anything they shouldn't), but **do not expose a stop-stream button to
viewers** until it is fixed backend-side. Subscribing/unsubscribing is per-session and safe;
it is only the `start`/`stop` control verbs that are global.

---

## 5. Binary payloads — the biggest piece of new frontend work

Not everything is JSON. Frames and video fragments are raw buffers; forcing them through
JSON would mean base64-ing megabytes per frame. For binary ops the **model describes the
metadata** and the **body stays an opaque buffer**, and the client hands back
`{ meta, payload: Uint8Array }`.

The codec varies by capability, and the frontend needs a decoder for each:

| Capability / stream | Codec | Payload is |
| --- | --- | --- |
| `RheedCamera.image()` / `onFrame` | **NPY** | a numpy `.npy` buffer (header included) |
| `ChamberCamera.image()` / `onFrame` | **NPY** | same |
| `RheedVideo.initial_fragments()`, `.video_fragment()`, `onFragment` | **RAW** | opaque video fragment bytes |
| `DetectionDetection.detection()` / `onDetection` | **NPZ** | a numpy `.npz` archive (pattern + masks) |
| `RheedIntegrator.onIntegration`, `RheedStft.onStft` | binary | buffer + typed `meta` |
| everything else (configs, log, bboxes, node registry, storage) | **JSON** | `payload` is empty; data is in the returned model |

For NPY frames, `ImageMeta` carries what you need to interpret the buffer:

```ts
interface ImageMeta {
  time: number;
  uuid: string;
  time_stamp: string;
  frame_idx?: number | null;
  dtype?: string | null;    // e.g. "uint8", "uint16"
  shape?: number[] | null;  // e.g. [H, W] or [H, W, C]
}
```

Note `dtype` and `shape` are also in the `.npy` header itself, so a proper `.npy` parser can
work standalone; `meta` lets you fast-path without parsing the header. **Check this against
the current rendering path** — if the old API handed the frontend pre-encoded images, this is
a real change and the frontend now owns dtype/shape → canvas conversion (including any
16-bit → 8-bit windowing).

---

## 6. Suggested order of work

1. Vendor `web/src/generated/lumi.ts` into the frontend repo; set up a way to refresh it.
2. Stand up `LumiTransport` + auth (`?token=`, handle close code 4401) and a
   reconnect/resubscribe wrapper.
3. Port the read-only views first (chamber log, configs, node registry) — those are plain
   JSON and prove the transport.
4. Do the NPY frame decoder; port the camera view. This is the risky one — budget for it.
5. Port video (RAW fragments), then analysis (integrator/STFT).
6. Detection: build the overlay path (`DetectionOverlay`) as the default; treat the full NPZ
   detection stream as an operator-only extra.
7. Add role-gating to the UI, then build the new registry/supervisor views.

---

## 7. Running the backend locally

The bridge needs a RabbitMQ broker; it will not start without one.

```bash
LUMI_AMQP_URL=amqp://guest:guest@localhost:5672/ uv run uvicorn lumi.api.main:app --reload
```

`LUMI_AMQP_URL` wins over settings — **set it**, or the app dials the lab IP and hangs for
~135s on TCP timeout. Provision broker permissions from the contract with
`scripts/apply_broker_permissions.py`. Note the exchanges are now `topic` (they were
`direct`): a broker that has the old exchanges must have them **deleted first**, so cut over
on a fresh vhost or stop-delete-start.

`GET /health` returns `{ ok, capabilities: [...] }` — a quick check that the bridge is up and
which targets exist.
