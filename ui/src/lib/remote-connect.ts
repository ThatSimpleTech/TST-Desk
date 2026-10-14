// Browser attach target (TD-3701).
//
// The desktop window reads port.json through Tauri. A browser has no port
// file: the user (or a query/hash) supplies `ws://host:port` plus the
// rotating `{user_data_dir}/remote-token` from TD-3602. Same `hello.token`
// field; same ProtocolClient. No account, no relay, no 0.0.0.0.

export interface AttachTarget {
  host: string;
  port: number;
  token: string;
  /** Set when the token is this device's own key from pairing (TD-5014). */
  deviceId?: string;
  /** The paired computer's name, for the banner and the refusal copy. */
  desktopName?: string;
}

/** What survives a refused key (TD-5014): where and who, never a token. */
export interface SavedAddress {
  host: string;
  port: number;
  desktopName: string | null;
}

export type AttachParse =
  | { ok: true; target: AttachTarget }
  | { ok: false; reason: "missing" }
  | { ok: false; reason: "invalid"; error: string };

const UNSPECIFIED_HOSTS = new Set(["0.0.0.0", "::", "", "*", "[::]"]);

/** Desktop attach allows any specific host. The phone shell allows Tailscale only. */
export type WsHostPolicy = "any-specific" | "remote-shell";

export function wsHostPolicyFor(mode: string): WsHostPolicy {
  return mode === "android" ? "remote-shell" : "any-specific";
}

const REMOTE_SHELL_REFUSAL =
  "The phone shell only connects to a Tailscale address (100.64.0.0/10, fd7a:115c:a1e0::/48, or a .ts.net name).";

function ipv4Octets(host: string): number[] | null {
  const parts = host.split(".");
  if (parts.length !== 4) return null;
  const octets: number[] = [];
  for (const part of parts) {
    if (!/^[0-9]{1,3}$/.test(part)) return null;
    const value = Number(part);
    if (value > 255) return null;
    octets.push(value);
  }
  return octets;
}

/** Tailscale CGNAT `100.64.0.0/10`. */
export function isTailscaleIpv4(host: string): boolean {
  const octets = ipv4Octets(host);
  if (octets === null) return false;
  return octets[0] === 100 && octets[1] >= 64 && octets[1] <= 127;
}

function expandIpv6(host: string): number[] | null {
  const bare = host.toLowerCase().split("%")[0];
  if (bare === "" || bare.includes(".")) return null;
  const halves = bare.split("::");
  if (halves.length > 2) return null;
  const parseSide = (side: string): string[] | null => {
    if (side === "") return [];
    const groups = side.split(":");
    if (groups.some((group) => !/^[0-9a-f]{1,4}$/.test(group))) return null;
    return groups;
  };
  const head = parseSide(halves[0] ?? "");
  if (head === null) return null;
  const tail = halves.length === 2 ? parseSide(halves[1] ?? "") : [];
  if (tail === null) return null;
  if (halves.length === 1 && head.length !== 8) return null;
  const missing = 8 - head.length - tail.length;
  if (missing < 0) return null;
  const groups = [...head, ...Array.from({ length: missing }, () => "0"), ...tail];
  if (groups.length !== 8) return null;
  return groups.map((group) => Number.parseInt(group, 16));
}

/** Tailscale ULA `fd7a:115c:a1e0::/48`. */
export function isTailscaleIpv6(host: string): boolean {
  const groups = expandIpv6(unwrapHost(host));
  if (groups === null) return false;
  return groups[0] === 0xfd7a && groups[1] === 0x115c && groups[2] === 0xa1e0;
}

const MAGIC_DNS = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net$/;

/** Tailscale MagicDNS. `ts.net` alone is not a machine. */
export function isTailscaleMagicDns(host: string): boolean {
  const name = unwrapHost(host).toLowerCase();
  if (name.length > 253) return false;
  return MAGIC_DNS.test(name);
}

export function isRemoteShellHost(host: string): boolean {
  const bare = unwrapHost(host).toLowerCase();
  if (bare === "" || isUnspecifiedHost(bare)) return false;
  return isTailscaleIpv4(bare) || isTailscaleIpv6(bare) || isTailscaleMagicDns(bare);
}

export function isUnspecifiedHost(host: string): boolean {
  const bare = unwrapHost(host).toLowerCase();
  if (UNSPECIFIED_HOSTS.has(bare)) return true;
  return bare.startsWith("::ffff:") && bare.slice("::ffff:".length) === "0.0.0.0";
}

export function unwrapHost(host: string): string {
  return host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
}

/** Build the WebSocket URL the protocol client opens. IPv6 is bracketed. */
export function daemonWsUrl(host: string, port: number): string {
  const bare = unwrapHost(host);
  const wrapped = bare.includes(":") ? `[${bare}]` : bare;
  return `ws://${wrapped}:${port}`;
}

export function parseWsUrl(
  input: string,
  policy: WsHostPolicy = wsHostPolicyFor(import.meta.env.MODE),
): { ok: true; host: string; port: number } | { ok: false; error: string } {
  const trimmed = input.trim();
  if (trimmed === "") {
    return { ok: false, error: "Enter a WebSocket URL (ws://host:port)." };
  }
  const withScheme = trimmed.includes("://") ? trimmed : `ws://${trimmed}`;
  let url: URL;
  try {
    url = new URL(withScheme);
  } catch {
    return { ok: false, error: "Not a valid WebSocket URL." };
  }
  if (url.protocol !== "ws:") {
    return { ok: false, error: "Use ws://host:port — the daemon does not serve TLS." };
  }
  const host = unwrapHost(url.hostname);
  if (isUnspecifiedHost(host)) {
    return { ok: false, error: "Refusing 0.0.0.0 / :: — connect to a specific address." };
  }
  if (policy === "remote-shell" && !isRemoteShellHost(host)) {
    return { ok: false, error: REMOTE_SHELL_REFUSAL };
  }
  if (host === "") {
    return { ok: false, error: "URL is missing a host." };
  }
  if (url.port === "") {
    return { ok: false, error: "URL must include a port." };
  }
  const port = Number(url.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return { ok: false, error: "URL port is out of range." };
  }
  return { ok: true, host, port };
}

export function validateAttachTarget(
  ws: string,
  token: string,
  policy: WsHostPolicy = wsHostPolicyFor(import.meta.env.MODE),
): AttachParse {
  const parsed = parseWsUrl(ws, policy);
  if (!parsed.ok) return { ok: false, reason: "invalid", error: parsed.error };
  const trimmedToken = token.trim();
  if (trimmedToken === "") {
    return { ok: false, reason: "invalid", error: "Enter the remote token (not the port-file token)." };
  }
  return { ok: true, target: { host: parsed.host, port: parsed.port, token: trimmedToken } };
}

function paramsOf(raw: string): URLSearchParams {
  const body = raw.startsWith("?") || raw.startsWith("#") ? raw.slice(1) : raw;
  return new URLSearchParams(body);
}

/** Read `ws` + `token` from the hash first (not sent to an HTTP server), then the query. */
export function parseAttachParams(
  search: string,
  hash = "",
  policy: WsHostPolicy = wsHostPolicyFor(import.meta.env.MODE),
): AttachParse {
  const fromHash = paramsOf(hash);
  const fromSearch = paramsOf(search);
  const ws = fromHash.get("ws") ?? fromSearch.get("ws");
  const token = fromHash.get("token") ?? fromSearch.get("token");
  if (ws === null && token === null) return { ok: false, reason: "missing" };
  return validateAttachTarget(ws ?? "", token ?? "", policy);
}

const DEVICE_ID = /^dev_[0-9a-f]{12}$/;
export const MAX_NAME_CHARS = 64;

/** The daemon's device_name rule (TD-5014): no control characters, trimmed,
 *  at most 64 characters. Also applied to a desktop name read from a link or
 *  the store, since both are text someone else wrote. */
export function cleanName(raw: string): string {
  const visible = Array.from(raw)
    .filter((ch) => {
      const code = ch.codePointAt(0) ?? 0;
      return code >= 0x20 && !(code >= 0x7f && code <= 0x9f);
    })
    .join("");
  return Array.from(visible.trim()).slice(0, MAX_NAME_CHARS).join("").trim();
}

function storedRecord(raw: string | null): Record<string, unknown> | null {
  if (raw === null || raw === "") return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
    return value as Record<string, unknown>;
  } catch {
    return null;
  }
}

function storedName(rec: Record<string, unknown>): string | null {
  if (typeof rec.desktopName !== "string") return null;
  const name = cleanName(rec.desktopName);
  return name === "" ? null : name;
}

export function parseStoredAttach(raw: string | null): AttachTarget | null {
  const rec = storedRecord(raw);
  if (rec === null) return null;
  if (typeof rec.host !== "string" || typeof rec.port !== "number" || typeof rec.token !== "string") {
    return null;
  }
  const checked = validateAttachTarget(daemonWsUrl(rec.host, rec.port), rec.token);
  if (!checked.ok) return null;
  // TD-5014: a 0.1.5 record has neither extra field and stays valid.
  const target: AttachTarget = checked.target;
  if (typeof rec.deviceId === "string" && DEVICE_ID.test(rec.deviceId)) target.deviceId = rec.deviceId;
  const name = storedName(rec);
  if (name !== null) target.desktopName = name;
  return target;
}

/** The address part of any saved record, including one whose key was refused. */
export function parseStoredAddress(raw: string | null): SavedAddress | null {
  const rec = storedRecord(raw);
  if (rec === null || typeof rec.host !== "string" || typeof rec.port !== "number") return null;
  const parsed = parseWsUrl(daemonWsUrl(rec.host, rec.port));
  if (!parsed.ok) return null;
  return { host: parsed.host, port: parsed.port, desktopName: storedName(rec) };
}

export function serializeAttach(target: AttachTarget): string {
  const record: Record<string, string | number> = {
    host: target.host,
    port: target.port,
    token: target.token,
  };
  if (target.deviceId !== undefined) record.deviceId = target.deviceId;
  if (target.desktopName !== undefined) record.desktopName = target.desktopName;
  return JSON.stringify(record);
}

/** A refused key's record (TD-5014). The token is emptied, not omitted: the
 *  shell's shape check wants the field, and an empty one never loads as a
 *  target, so the next launch asks to pair instead of retrying a dead key. */
export function serializeAddress(address: SavedAddress): string {
  const record: Record<string, string | number> = { host: address.host, port: address.port, token: "" };
  if (address.desktopName !== null) record.desktopName = address.desktopName;
  return JSON.stringify(record);
}

export function resolveBrowserTarget(input: {
  search: string;
  hash: string;
  stored: AttachTarget | null;
}):
  | { ok: true; target: AttachTarget; from: "url" | "stored"; stripUrl: boolean }
  | { ok: false; error: string | null } {
  const parsed = parseAttachParams(input.search, input.hash);
  if (parsed.ok) return { ok: true, target: parsed.target, from: "url", stripUrl: true };
  if (parsed.reason === "invalid") return { ok: false, error: parsed.error };
  if (input.stored !== null) {
    return { ok: true, target: input.stored, from: "stored", stripUrl: false };
  }
  return { ok: false, error: null };
}

/** Drop `ws` / `token` from the URL so a shared history entry is not a credential. */
export function stripAttachFromUrl(href: string): string {
  const url = new URL(href);
  url.searchParams.delete("ws");
  url.searchParams.delete("token");
  if (url.hash !== "") {
    const hashParams = new URLSearchParams(url.hash.startsWith("#") ? url.hash.slice(1) : url.hash);
    if (hashParams.has("ws") || hashParams.has("token")) {
      hashParams.delete("ws");
      hashParams.delete("token");
      const leftover = hashParams.toString();
      url.hash = leftover;
    }
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
