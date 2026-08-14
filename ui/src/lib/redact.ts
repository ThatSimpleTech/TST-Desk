// Secret redaction for any text the UI hands to a human (TD-1008 graft).
//
// Mirrors core's redact_secrets (tstd/logging.py SECRET_PATTERNS) — the
// daemon already redacts at event-log insertion (TD-1405), this is the belt:
// anything pasted out of the app (diagnostics report) gets one more pass so
// a future daemon path that forgets to redact can't leak through the UI.
// Wider than core's on purpose: Bearer headers and dashed key forms are
// cheap to catch here, and over-redaction in a diagnostics report is
// harmless. Pure module, no runes.

const SECRET_PATTERNS: RegExp[] = [
	// sk-… API keys, including dashed forms (sk-ant-…); core requires 20+
	// alphanumerics with no dashes, which misses those.
	/sk-[a-zA-Z0-9][a-zA-Z0-9_-]{15,}/g,
	/github_pat_[a-zA-Z0-9_]{20,}/g,
	/ghp_[a-zA-Z0-9]{20,}/g,
	/AKIA[0-9A-Z]{16}/g,
	// Consumes the marker dashes too — assertion-shape note for tests.
	/-----BEGIN\s+(RSA |EC |DSA |OPENSSH )?PRIVATE KEY/g,
	/Bearer\s+\S+/g,
];

/** Replace known credential patterns with ``[REDACTED]``. */
export function redact(text: string): string {
	let out = text;
	for (const pattern of SECRET_PATTERNS) out = out.replace(pattern, "[REDACTED]");
	return out;
}
