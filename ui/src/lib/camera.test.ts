// @vitest-environment jsdom
//
// Composer camera capture: the camera opens, one frame becomes bytes, the
// stream is always released, and a refusal says so without claiming a photo
// was taken. The frame grab is injected — a jsdom video element decodes
// nothing, so the real drawImage path is exercised in the browser.

import { describe, it, expect, vi, afterEach } from "vitest";
import { capturePhoto, photoName } from "./camera";

function fakeStream(): MediaStream {
  const stop = vi.fn();
  const track = { getSettings: () => ({}), stop };
  return {
    getVideoTracks: () => [track],
    getTracks: () => [track],
  } as unknown as MediaStream;
}

afterEach(() => vi.unstubAllGlobals());

// jsdom ships no mediaDevices; the availability guard reads it before the
// injected opener runs, so it has to exist for the happy/denied/empty paths.
function stubMediaDevices(): void {
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn() } });
}

describe("capturePhoto", () => {
  it("grabs a frame and releases the camera", async () => {
    stubMediaDevices();
    const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);
    const stream = fakeStream();
    const outcome = await capturePhoto(async () => stream, async () => bytes);
    expect(outcome).toEqual({ ok: true, bytes });
    expect((stream.getTracks()[0] as unknown as { stop: ReturnType<typeof vi.fn> }).stop).toHaveBeenCalled();
  });

  it("reports a denied camera without claiming a photo", async () => {
    stubMediaDevices();
    const grab = vi.fn(async () => new Uint8Array([1]));
    const outcome = await capturePhoto(
      async () => {
        throw new DOMException("nope", "NotAllowedError");
      },
      grab,
    );
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.code).toBe("camera_denied");
    expect(grab).not.toHaveBeenCalled();
  });

  it("reports an unavailable camera when getUserMedia is missing", async () => {
    vi.stubGlobal("navigator", {});
    const outcome = await capturePhoto(async () => fakeStream(), async () => new Uint8Array([1]));
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.code).toBe("camera_unavailable");
  });

  it("reports an empty frame when nothing decodes, and still releases the camera", async () => {
    stubMediaDevices();
    const stream = fakeStream();
    const outcome = await capturePhoto(async () => stream, async () => null);
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.code).toBe("camera_empty");
    expect((stream.getTracks()[0] as unknown as { stop: ReturnType<typeof vi.fn> }).stop).toHaveBeenCalled();
  });

  it("names the photo photo-YYYY-MM-DD-HHMMSS.png", () => {
    const d = new Date(2026, 1, 14, 9, 3, 12);
    expect(photoName(d)).toBe("photo-2026-02-14-090312.png");
  });
});
