// Composer camera capture. Two routes (see captureRoute):
//
// - Android opens the *camera app* through an `<input type="file" capture>`:
//   WebView turns that into an image-capture intent and the shot returns on
//   the normal file-input path. getUserMedia here is a silent background
//   capture (the privacy dot blinks, no app opens), which reads as a bug on
//   a phone even when it works.
// - Everywhere else (the desktop shells) uses getUserMedia: open the default
//   camera, grab one frame as PNG bytes, release the device — the camera
//   light should not outlive the shot.
//
// Both routes end at the same attachment gate (acceptAttachment); nothing
// bypasses the caps. `open` and `grabFrame` are injectable so this unit-tests
// without a real camera (a jsdom video element decodes nothing).

import type { Platform } from './sent-files';

/** Android launches the camera app; everything else captures in-page.
 *  `android-old` has no file bridge, but `<input capture>` is handled by the
 *  shell's file chooser, so the app route works there too. */
export function captureRoute(platform: Platform): 'camera-app' | 'getUserMedia' {
  return platform === 'android' || platform === 'android-old' ? 'camera-app' : 'getUserMedia';
}

/** What a failed capture reports. `message` is the copy the composer shows;
 *  a refusal never claims a photo was taken. */
export type CaptureFailureCode = 'camera_unavailable' | 'camera_denied' | 'camera_empty';

export interface CaptureFailure {
  code: CaptureFailureCode;
  message: string;
}

export type CaptureOutcome = { ok: true; bytes: Uint8Array } | { ok: false; failure: CaptureFailure };

const DENIED_COPY = 'Camera access was refused. Allow the camera for TST Desk, then try again.';
const UNAVAILABLE_COPY = 'No camera is available to this window.';
const EMPTY_COPY = 'The camera opened but delivered no frame. Try again.';

/** A photo the user just took, named for when it was taken. The daemon
 *  strips paths again, but the chip should read like what it is. */
export function photoName(now: Date = new Date()): string {
  const p = (n: number): string => String(n).padStart(2, '0');
  return `photo-${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}-${p(
    now.getHours(),
  )}${p(now.getMinutes())}${p(now.getSeconds())}.png`;
}

async function defaultOpen(): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } } });
}

/** Draw the live stream onto an offscreen canvas and encode one PNG frame.
 *  The video element goes in the document because WebKit decodes faster to
 *  an attached element; a frame must have landed before drawImage is valid. */
async function defaultGrabFrame(stream: MediaStream): Promise<Uint8Array | null> {
  const video = document.createElement('video');
  video.srcObject = stream;
  video.muted = true;
  video.style.display = 'none';
  document.body.appendChild(video);
  try {
    await video.play();
    for (let tries = 0; tries < 50 && !(video.videoWidth > 0 && video.videoHeight > 0); tries += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (video.videoWidth <= 0 || video.videoHeight <= 0) return null;
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    if (ctx === null) return null;
    ctx.drawImage(video, 0, 0);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob((b) => resolve(b), 'image/png'));
    if (blob === null) return null;
    return new Uint8Array(await blob.arrayBuffer());
  } catch {
    return null;
  } finally {
    video.srcObject = null;
    video.remove();
  }
}

export async function capturePhoto(
  open: () => Promise<MediaStream> = defaultOpen,
  grabFrame: (stream: MediaStream) => Promise<Uint8Array | null> = defaultGrabFrame,
): Promise<CaptureOutcome> {
  if (typeof navigator === 'undefined' || navigator.mediaDevices?.getUserMedia == null) {
    return { ok: false, failure: { code: 'camera_unavailable', message: UNAVAILABLE_COPY } };
  }
  let stream: MediaStream;
  try {
    stream = await open();
  } catch (err) {
    const name = err instanceof DOMException ? err.name : '';
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      return { ok: false, failure: { code: 'camera_denied', message: DENIED_COPY } };
    }
    return { ok: false, failure: { code: 'camera_unavailable', message: UNAVAILABLE_COPY } };
  }
  try {
    const bytes = await grabFrame(stream);
    if (bytes === null) return { ok: false, failure: { code: 'camera_empty', message: EMPTY_COPY } };
    return { ok: true, bytes };
  } finally {
    for (const track of stream.getTracks()) track.stop();
  }
}
