// Enter while an input method is composing (COMPOSER-08). With a Japanese,
// Chinese or Korean IME, Enter accepts the candidate; the text field then
// still sees a keydown for Enter, and sending on it sent half a word. A
// composing keydown says so with isComposing, or keyCode 229 in older
// engines; WebKit can deliver the accepting Enter just after compositionend
// with neither, which is what a caller's own `tracked` flag catches.

export interface ComposeKey {
  isComposing?: boolean;
  keyCode?: number;
}

/** True when this keydown belongs to an input method, not to the field. */
export function composing(event: ComposeKey, tracked = false): boolean {
  return tracked || event.isComposing === true || event.keyCode === 229;
}
