// Secret redaction for any text the UI hands to a human (TD-1008 graft).
//
// Mirrors core's redact_secrets (tstd/logging.py SECRET_PATTERNS) — the
// daemon already redacts at event-log insertion (TD-1405), this is the belt:
// anything pasted out of the app (diagnostics report) gets one more pass so
// a future daemon path that forgets to redact can't leak through the UI.
// Wider than core's on purpose: Bearer headers and dashed key forms are
// cheap to catch here, and over-redaction in a diagnostics report is
// harmless. Pure module, no runes. It also cleans OS notification banners
// (os-notify.ts), which a person reads, so the sk- rule is core's (TD-5161).

// A run after "sk-" that reads as words, not a key (core's _SK_WORDS,
// TD-5160): short lowercase or Title-case words with up to four digits,
// short acronyms or short numbers, split by "-" or "_".
const SK_WORD = "(?:[A-Z]?[a-z]{1,24}[0-9]{0,4}|[A-Z]{1,4}[0-9]{0,4}|[0-9]{1,8})";
const SK_WORDS = `${SK_WORD}(?:[-_]+${SK_WORD})*[-_]*(?![a-zA-Z0-9_-])`;

const SECRET_PATTERNS: RegExp[] = [
	// sk-… API keys, including dashed forms (sk-ant-…). Straight after a
	// lowercase letter a run that reads as words is let go, so a path such
	// as "tst-desk-gap-audit-2026-10-14.md" keeps its name.
	new RegExp(`(?:(?<![a-z])|(?!sk-${SK_WORDS}))sk-[a-zA-Z0-9][a-zA-Z0-9_-]{15,}`, "g"),
	/github_pat_[a-zA-Z0-9_]{20,}/g,
	/ghp_[a-zA-Z0-9]{20,}/g,
	/ghu_[a-zA-Z0-9_]{20,}/g,
	/ghr_[a-zA-Z0-9_]{20,}/g,
	/gho_[a-zA-Z0-9_]{20,}/g,
	/ghs_[a-zA-Z0-9_]{20,}/g,
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
