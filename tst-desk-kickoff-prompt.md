# Claude Code kickoff prompt — TST Desk v0.1

Paste everything below the line into Claude Code, in the directory where you want the repo.
Make sure these four files are present in that directory first:

- `tst-desk-spec.md`
- `tst-desk-backlog.md`
- `AGENTS.md`
- this file

And clone `tst-cua` alongside it if it isn't already available locally.

---

We are building **TST Desk** — an open-source, bring-your-own-model desktop agent workspace.
It is a local application that feels like Claude Desktop + Cowork, running on user-selected
models through an OpenAI-compatible API. There is no server, no hosted backend, no account,
and no subscription. The only credential is the user's own API key.

## Read these first, in this order

1. **`AGENTS.md`** — the working contract. How you operate in this repo. It outranks your
   defaults. Pay particular attention to §2 prime directives, §3 scope discipline, §5 decision
   protocol, and §9 reporting format.
2. **`tst-desk-spec.md`** — what we're building and why. Source of truth for behavior.
3. **`tst-desk-backlog.md`** — epics, stories, acceptance criteria, sequencing. Source of truth
   for scope and order.

Confirm you've read all three and summarize the prime directives back to me in one short list
before doing anything else.

## Your first two actions

**First: `TD-101`.** Ask me the seven open decisions from spec §10 — repo layout, `tst-cua`
reuse strategy, Goose's role, license, frontend framework, product name, and autonomy isolation
policy. Ask all seven in one message. Give me a recommendation and a one-line rationale for
each. Then wait. Do not proceed on defaults; I want to decide these.

**Second: `TD-102`.** Read the `tst-cua` repository in full and produce `docs/REUSE.md`
classifying every module as reuse / adapt / ignore / defer. Assess the agent loop specifically:
its control flow, its tool interface, and what must change to sit behind a three-tier router.
Review that report with me before porting a single line.

Those two stories are the highest-leverage hours in this project. Do not rush them.

## Then work the backlog in order

Milestones are defined in the backlog §0. Work stories in dependency order, one at a time,
following the workflow in `AGENTS.md` §4 and reporting in the format in `AGENTS.md` §9.

**Milestone M1 (headless core) comes before M2 (the window).** This is deliberate and not
negotiable. A correct, tested, headless core makes the UI a presentation problem instead of a
debugging problem. M1 exits when `TD-1401` — the end-to-end headless harness — passes.

For any story sized 5 or higher, propose your approach and wait for sign-off before building.

## Scope discipline

Build only the current milestone. The post-v0.1 backlog at the end of the backlog document —
memory, detached sessions, computer use, remote access, notifications, the autonomy engine,
MCP — is **out of scope**. Do not build it. Do not scaffold it beyond a one-line
`TODO(TD-###)` comment where a future seam belongs.

This project has a specific failure mode: it is architecturally interesting, and building the
fun later phases early is tempting. A shipped v0.1 beats an elaborate half-built v0.4.

If you believe something out of scope is genuinely required, stop and ask. If something I say
in chat conflicts with the backlog, say so and ask which wins — do not silently follow the
newer instruction.

## The three things most likely to go wrong

Flagged so you weigh them correctly, not so you solve them now:

1. **`TD-1301`, bundling the Python runtime inside Tauri**, is the highest-risk story. Start it
   earlier than its position suggests, timebox it, and escalate to me if it resists.
2. **The shell (E10) is the largest body of work in v0.1**, larger than the agent core. Size it
   honestly. If it slips, M1 still delivers a working headless product.
3. **The classifier and boundary enforcement (E6, E7) are security controls**, not conveniences.
   `TD-1402` is a release blocker. Write the attacks as tests before writing the defenses.

## Success criterion for v0.1

A stranger clones or downloads the app, runs one install command, pastes an API key, points it
at a folder, types a request, watches the activity timeline, approves a shell command, and gets
a result — **without opening a terminal after install, and without creating an account
anywhere.**

Build to that. Start with `TD-101`.
