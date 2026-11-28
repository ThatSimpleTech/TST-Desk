// RENDERSEC-02: text the person approves is shown in the order it will run.
//
// A command, path or summary that carries a bidi override (U+202E and kin),
// a zero-width character or a terminal control code can display as something
// other than what executes: `rm -rf <RLO>/emoh` reads one way and runs another.
// Each such character is replaced by a visible, named marker, so the card
// shows the logical order and nothing hides. Newlines and tabs stay.

const HIDDEN =
  // eslint-disable-next-line no-control-regex
  /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u00AD\u061C\u115F\u1160\u180E\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u206F\u3164\uFEFF\uFFF9-\uFFFB]/gu;

const NAMES: Record<number, string> = {
  0x1b: "ESC",
  0x7f: "DEL",
  0x200b: "ZWSP",
  0x200c: "ZWNJ",
  0x200d: "ZWJ",
  0x200e: "LRM",
  0x200f: "RLM",
  0x202a: "LRE",
  0x202b: "RLE",
  0x202c: "PDF",
  0x202d: "LRO",
  0x202e: "RLO",
  0x2066: "LRI",
  0x2067: "RLI",
  0x2068: "FSI",
  0x2069: "PDI",
  0x061c: "ALM",
  0xfeff: "BOM",
};

function marker(ch: string): string {
  const code = ch.codePointAt(0) ?? 0;
  const hex = code.toString(16).toUpperCase().padStart(4, "0");
  const name = NAMES[code];
  return name ? `⟨${name} U+${hex}⟩` : `⟨U+${hex}⟩`;
}

/** The text with every hidden or reordering character shown as a marker. */
export function reveal(text: string): string {
  return text.replace(HIDDEN, marker);
}

/** Whether `text` holds anything `reveal` would mark. */
export function hasHidden(text: string): boolean {
  HIDDEN.lastIndex = 0;
  const found = HIDDEN.test(text);
  HIDDEN.lastIndex = 0;
  return found;
}
