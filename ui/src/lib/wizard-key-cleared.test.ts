// @vitest-environment jsdom
//
// ONBOARD-02: a key typed into Add a model never outlives the wizard and is
// never offered to a different server. The field was component state that
// closing did not clear, so the next server's key step came up filled.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";

import WizardPane from "./components/WizardPane.svelte";
import { closeWizard, onboarding, openAddModel, resetOnboarding, setModelUrl } from "./onboarding.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function keyField(): HTMLInputElement | null {
  return document.querySelector('input[type="password"]');
}

async function typeKey(value: string): Promise<void> {
  const input = keyField();
  expect(input).not.toBeNull();
  input!.value = value;
  input!.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}

beforeEach(() => {
  resetOnboarding();
  openAddModel();
  app = mount(WizardPane, { target: document.body });
});

afterEach(async () => {
  if (app !== null) await unmount(app);
  app = null;
  document.body.replaceChildren();
  resetOnboarding();
});

describe("the key typed into Add a model", () => {
  it("is gone when the wizard closes and opens again for another server", async () => {
    setModelUrl("https://a.example/v1");
    await tick();
    await typeKey("sk-provider-a");
    expect(onboarding.model.apiKey).toBe("sk-provider-a");

    closeWizard();
    openAddModel();
    setModelUrl("https://b.example/v1");
    await tick();
    expect(keyField()?.value).toBe("");
  });

  it("is cleared when the server URL changes", async () => {
    setModelUrl("https://a.example/v1");
    await tick();
    await typeKey("sk-provider-a");
    setModelUrl("https://b.example/v1");
    await tick();
    expect(onboarding.model.apiKey).toBe("");
    expect(keyField()?.value).toBe("");
  });

  it("stays for the same server", async () => {
    setModelUrl("https://a.example/v1");
    await tick();
    await typeKey("sk-provider-a");
    setModelUrl("https://a.example/v1");
    await tick();
    expect(keyField()?.value).toBe("sk-provider-a");
  });
});
