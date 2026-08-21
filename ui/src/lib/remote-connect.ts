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
}

export type AttachParse =
  | { ok: true; target: AttachTarget }
  | { ok: false; reason: "missing" }
  | { ok: false; reason: "invalid"; error: string };

const UNSPECIFIED_HOSTS = new Set(["0.0.0.0", "::", "", "*", "[::]"]);

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

export function validateAttachTarget(ws: string, token: string): AttachParse {
  const parsed = parseWsUrl(ws);
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
export function parseAttachParams(search: string, hash = ""): AttachParse {
  const fromHash = paramsOf(hash);
  const fromSearch = paramsOf(search);
  const ws = fromHash.get("ws") ?? fromSearch.get("ws");
  const token = fromHash.get("token") ?? fromSearch.get("token");
  if (ws === null && token === null) return { ok: false, reason: "missing" };
  return validateAttachTarget(ws ?? "", token ?? "");
}

export function parseStoredAttach(raw: string | null): AttachTarget | null {
  if (raw === null || raw === "") return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null) return null;
    const rec = value as Record<string, unknown>;
    if (typeof rec.host !== "string" || typeof rec.port !== "number" || typeof rec.token !== "string") {
      return null;
    }
    const checked = validateAttachTarget(daemonWsUrl(rec.host, rec.port), rec.token);
    return checked.ok ? checked.target : null;
  } catch {
    return null;
  }
}

export function serializeAttach(target: AttachTarget): string {
  return JSON.stringify({ host: target.host, port: target.port, token: target.token });
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
