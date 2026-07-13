"""Emit the TypeScript client for the browser.

The browser talks to the **backend bridge** (lumi/api/bridge.py) over one WebSocket,
NOT to the broker directly. The bridge authenticates the user, enforces what their role
may do, and proxies to the bus with a privileged credential -- so the broker is never
exposed to an untrusted client, and access is tied to a real login rather than a shared
broker password living in the browser.

Everything below is generated from the same contract as the Python client, so the two
cannot drift. Types come from pydantic's JSON Schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from lumi.contracts import REGISTRY, Capability, Codec, EquipmentContract

from .common import all_models, pascal, ts_banner

PRELUDE = r"""
// --- transport: one WebSocket to the backend bridge -------------------------
// Frame: [u32 header_len][header json][payload bytes]. Binary-safe, so frames, video
// fragments and detection masks pass through intact.

function packFrame(header: Record<string, unknown>, payload: Uint8Array = new Uint8Array()): ArrayBuffer {
  const head = new TextEncoder().encode(JSON.stringify(header));
  const out = new ArrayBuffer(4 + head.length + payload.length);
  const view = new DataView(out);
  const bytes = new Uint8Array(out);
  view.setUint32(0, head.length);
  bytes.set(head, 4);
  bytes.set(payload, 4 + head.length);
  return out;
}

function unpackFrame(buf: ArrayBuffer): { header: any; payload: Uint8Array } {
  const view = new DataView(buf);
  const bytes = new Uint8Array(buf);
  const headLen = view.getUint32(0);
  const header = JSON.parse(new TextDecoder().decode(bytes.slice(4, 4 + headLen)));
  return { header, payload: bytes.slice(4 + headLen) };
}

export interface Response<T> { meta: T; payload: Uint8Array }

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void; timer: number };
type StreamHandler = (meta: any, payload: Uint8Array) => void;

/**
 * The browser's connection to the backend bridge. RPCs correlate on an id; streams are
 * routed to per-(target,stream) handlers. What a request is *allowed* to do is enforced
 * by the bridge from the logged-in user's role -- this client cannot bypass it.
 */
export class LumiTransport {
  private ws!: WebSocket;
  private pending = new Map<string, Pending>();
  private streams = new Map<string, StreamHandler>();
  private seq = 0;

  constructor(private url: string, private timeoutMs = 10000) {}

  connect(): Promise<void> {
    this.ws = new WebSocket(this.url);
    this.ws.binaryType = "arraybuffer";
    return new Promise((resolve, reject) => {
      this.ws.onopen = () => resolve();
      this.ws.onerror = () => reject(new Error(`bridge websocket failed: ${this.url}`));
      this.ws.onmessage = (ev) => this.onMessage(ev.data as ArrayBuffer);
    });
  }

  private onMessage(buf: ArrayBuffer): void {
    const { header, payload } = unpackFrame(buf);

    if (header.kind === "stream") {
      const h = this.streams.get(`${header.target}:${header.stream}`);
      if (h) h(header.meta, payload);
      return;
    }

    const p = this.pending.get(header.correlation_id);
    if (!p) return;
    this.pending.delete(header.correlation_id);
    clearTimeout(p.timer);

    if (header.kind === "error") {
      p.reject(new Error(`[${header.error_type}] ${header.error_message}`));
    } else if (header.meta !== undefined) {
      p.resolve({ meta: header.meta, payload });     // binary op
    } else {
      p.resolve(header.body ?? {});
    }
  }

  private send(header: Record<string, unknown>, payload?: Uint8Array): void {
    this.ws.send(packFrame(header, payload));
  }

  private rpc<T>(header: Record<string, unknown>, payload?: Uint8Array): Promise<T> {
    const id = `c${this.seq++}`;
    return new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${header.target}.${header.op} timed out after ${this.timeoutMs}ms`));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.send({ ...header, kind: "request", correlation_id: id }, payload);
    });
  }

  /** A request/response op. */
  call<T>(target: string, op: string, body: unknown): Promise<T> {
    return this.rpc<T>({ target, op }, new TextEncoder().encode(JSON.stringify(body ?? {})));
  }

  /** A control verb (start/stop/state). */
  controlCall<T>(target: string, verb: string): Promise<T> {
    return this.rpc<T>({ target, op: verb, control: true });
  }

  /** Subscribe to a stream/update. Returns a key for unsubscribe(). */
  subscribe(target: string, stream: string, handler: StreamHandler): string {
    const key = `${target}:${stream}`;
    this.streams.set(key, handler);
    this.send({ kind: "subscribe", target, stream });
    return key;
  }

  unsubscribe(key: string): void {
    const [target] = key.split(":");
    this.streams.delete(key);
    this.send({ kind: "unsubscribe", target });
  }

  close(): void {
    this.ws.close();
  }
}
"""


def _ts_type(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """JSON Schema -> a TypeScript type expression."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]

    if "anyOf" in schema:
        parts = [_ts_type(s, defs) for s in schema["anyOf"]]
        # pydantic renders `X | None` as anyOf[X, null]
        return " | ".join(dict.fromkeys(parts))

    if "enum" in schema:
        return " | ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in schema["enum"])

    t = schema.get("type")
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "null":
        return "null";
    if t == "array":
        return f"{_ts_type(schema.get('items', {}), defs)}[]"
    if t == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {_ts_type(extra, defs)}>"
        return "Record<string, unknown>"
    return "unknown"


def _collect_interfaces(model: type[BaseModel], into: dict[str, str]) -> None:
    """Emit this model and every model it nests, each exactly once.

    Nested models arrive as $defs, and the same $def turns up under several parents
    (NodeStatus is reachable from NodeRecord, NodeList and RegistryEvent). Deduping
    only the top-level names emits it three times, which is a TypeScript compile
    error -- `tsc --strict` catches it, which is why it is in the check.
    """
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    defs = schema.get("$defs", {})

    for name, sub in sorted(defs.items()):
        if name not in into:
            into[name] = _object_interface(name, sub, defs)

    if model.__name__ not in into:
        into[model.__name__] = _object_interface(model.__name__, schema, defs)


def _object_interface(name: str, schema: dict[str, Any], defs: dict[str, Any]) -> str:
    if "enum" in schema:
        values = " | ".join(f'"{v}"' for v in schema["enum"])
        return f"export type {name} = {values};"

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return f"export interface {name} {{}}"

    lines = [f"export interface {name} {{"]
    for field, sub in props.items():
        optional = "" if field in required else "?"
        desc = sub.get("description")
        if desc:
            lines.append(f"  /** {desc} */")
        lines.append(f"  {field}{optional}: {_ts_type(sub, defs)};")
    lines.append("}")
    return "\n".join(lines)


def _capability_client(contract: EquipmentContract, cap: Capability) -> str:
    target = f"{contract.name}.{cap.name}"
    cls = f"{pascal(contract.name)}{pascal(cap.name)}Client"

    lines = [
        f"/** {cap.doc or target} */",
        f"export class {cls} {{",
        f'  static readonly target = "{target}";',
        "  constructor(private t: LumiTransport) {}",
        "",
    ]

    for op in cap.ops:
        takes = bool(op.request.model_fields)
        arg = f"req: {op.request.__name__}" if takes else ""
        body = "req" if takes else "{}"
        if op.response_codec is Codec.JSON:
            ret = op.response.__name__
        else:
            ret = f"Response<{op.response.__name__}>"
        doc = op.doc or f"{target}.{op.name}"
        lines += [
            f"  /** {doc} */",
            f"  async {op.name}({arg}): Promise<{ret}> {{",
            f'    return this.t.call<{ret}>({cls}.target, "{op.name}", {body});',
            "  }",
            "",
        ]

    for spec in (cap.stream, cap.update):
        if spec is None:
            continue
        lines += [
            f"  /** Subscribe to {spec.name}. Returns a key for unsubscribe(). */",
            f"  on{pascal(spec.name)}(handler: (meta: {spec.payload.__name__}, payload: Uint8Array) => void): string {{",
            f'    return this.t.subscribe({cls}.target, "{spec.name}", handler);',
            "  }",
            "",
            "  unsubscribe(key: string): void {",
            "    this.t.unsubscribe(key);",
            "  }",
            "",
        ]

    if cap.stream is not None:
        lines += [
            "  async startStreaming(): Promise<void> {",
            f'    await this.t.controlCall({cls}.target, "start");',
            "  }",
            "",
            "  async stopStreaming(): Promise<void> {",
            f'    await this.t.controlCall({cls}.target, "stop");',
            "  }",
            "",
        ]

    lines += [
        f"  async getState(): Promise<{cap.state.__name__}> {{",
        f'    return this.t.controlCall<{cap.state.__name__}>({cls}.target, "state");',
        "  }",
        "}",
    ]
    return "\n".join(lines)


def emit() -> str:
    out = [ts_banner(), PRELUDE, "// --- payload types ---------------------------------------------------------", ""]

    interfaces: dict[str, str] = {}
    for contract in REGISTRY.values():
        for model in all_models(contract):
            _collect_interfaces(model, interfaces)

    for name in sorted(interfaces):
        out.append(interfaces[name])
        out.append("")

    out.append("// --- clients ---------------------------------------------------------------")
    out.append("")
    for contract in REGISTRY.values():
        for cap in contract.capabilities:
            out.append(_capability_client(contract, cap))
            out.append("")

    # One aggregate per node.
    for contract in REGISTRY.values():
        cls = f"{pascal(contract.name)}Client"
        lines = [f"/** Every capability of the {contract.name} node. */", f"export class {cls} {{"]
        for cap in contract.capabilities:
            sub = f"{pascal(contract.name)}{pascal(cap.name)}Client"
            lines.append(f"  readonly {cap.name}: {sub};")
        lines.append("  constructor(t: LumiTransport) {")
        for cap in contract.capabilities:
            sub = f"{pascal(contract.name)}{pascal(cap.name)}Client"
            lines.append(f"    this.{cap.name} = new {sub}(t);")
        lines.append("  }")
        lines.append("}")
        out.append("\n".join(lines))
        out.append("")

    return "\n".join(out)
