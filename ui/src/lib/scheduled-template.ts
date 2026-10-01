// Job templates and "Schedule this chat" (TD-3816).
//
// A template fills the draft. It does not save a job. The one-shot rule
// `tomorrow at H:MM` is resolved here, when the user picks it, so the
// stored rule does not go stale. A cadence phrase is copied as written.

import { showScheduled } from "./projects.svelte.js";
import type {
	DaemonEventUnion,
	JobTemplateEntry,
	SaveJobTemplate,
	SessionJobSource,
} from "./protocol";
import {
	RETRY_DELAY,
	emptyDraft,
	graceDraftValue,
	retriesDraftValue,
	type JobDraftFields,
} from "./scheduled";
import { maxRunDraftValue } from "./scheduled-max-run";

export interface TemplateFlags {
	templateSavePending: boolean;
	awaitingSource: boolean;
}

/** The scheduled-store fields this reducer writes. */
export interface TemplateFormState {
	templates: JobTemplateEntry[];
	templateId: string;
	templateName: string;
	loading: boolean;
	error: string | null;
	editingId: string | null;
	draft: JobDraftFields;
}

const TOMORROW = /^tomorrow at (\d{1,2}):(\d{2})$/i;

/** `tomorrow at 9:00` as a local ISO instant. Anything else is returned as stored. */
export function resolveTemplateNextRun(rule: string, now = new Date()): string {
	const match = TOMORROW.exec(rule.trim());
	if (match === null) return rule;
	const hour = Number(match[1]);
	const minute = Number(match[2]);
	if (hour > 23 || minute > 59) return rule;
	const when = new Date(now.getTime());
	when.setDate(when.getDate() + 1);
	when.setHours(hour, minute, 0, 0);
	return when.toISOString();
}

/**
 * Replace the draft with a template.
 *
 * An omitted workspace keeps the folder already on the form: the built-ins
 * do not name one, and wiping it would make the user pick the folder again.
 * A one-shot clears cadence, including the new-job default. A cadence
 * template clears next run. If both were set, cadence wins so Create does
 * not send both.
 */
export function draftFromTemplate(
	template: JobTemplateEntry,
	current: JobDraftFields,
	now = new Date(),
): JobDraftFields {
	const cadence = (template.cadence ?? "").trim();
	const next = (template.next_run ?? "").trim();
	const useCadence = cadence !== "";
	const workspace = template.workspace?.trim() ?? "";
	return {
		workspace: workspace !== "" ? workspace : current.workspace,
		instruction: template.instruction,
		cadence: useCadence ? cadence : "",
		next_run: useCadence || next === "" ? "" : resolveTemplateNextRun(next, now),
		deliver_to: template.deliver_to,
		paused: false,
		preset: template.preset ?? "",
		engine: template.engine ?? "",
		grace: graceDraftValue(template.grace),
		retries: retriesDraftValue(template.retries),
		max_run: maxRunDraftValue(template.max_run),
		// A template does not name a follow-on or a calendar. Leaving the
		// previous choice would create a link, or skip days, the template
		// never had.
		then: "",
		skip_calendar: "",
		skip_match: "",
	};
}

/**
 * A chat, as a new job. Cadence, grace, and retries stay the new-job
 * defaults: the session does not have a schedule.
 */
export function draftFromSession(source: SessionJobSource): JobDraftFields {
	const draft = emptyDraft(source.workspace);
	const engine = source.engine === "native" || source.engine === "grok" ? source.engine : "";
	return {
		...draft,
		instruction: source.instruction,
		preset: source.preset ?? "",
		engine,
	};
}

/** Wire payload for a new template. Blank schedule fields are omitted. */
export function saveTemplateFromDraft(name: string, draft: JobDraftFields): SaveJobTemplate {
	const payload: SaveJobTemplate = {
		type: "save_job_template",
		name: name.trim(),
		instruction: draft.instruction.trim(),
		deliver_to: draft.deliver_to,
	};
	const cadence = draft.cadence.trim();
	const next = draft.next_run.trim();
	if (cadence !== "") payload.cadence = cadence;
	if (next !== "") payload.next_run = next;
	const workspace = draft.workspace.trim();
	if (workspace !== "") payload.workspace = workspace;
	const preset = draft.preset.trim();
	if (preset !== "") payload.preset = preset;
	if (draft.engine === "native" || draft.engine === "grok") payload.engine = draft.engine;
	const grace = draft.grace.trim();
	if (grace !== "") payload.grace = grace;
	const retries = retryCount(draft.retries);
	if (retries !== undefined) {
		payload.retries = retries;
		payload.retry_delay = RETRY_DELAY;
	}
	const maxRun = draft.max_run.trim();
	if (maxRun !== "") payload.max_run = maxRun;
	return payload;
}

function retryCount(raw: string): number | undefined {
	const text = raw.trim();
	if (text === "") return undefined;
	const count = Number(text);
	if (!Number.isInteger(count) || count < 1) return undefined;
	return count;
}

/**
 * Template replies and the chat-source reply.
 *
 * `clearJobSave` means a session prefill replaced the form, so a job save
 * that was in flight must not clear that prefill when `job_list` arrives.
 * A template save does not set the job-save flag: `job_list` would wipe
 * the form the user just stored.
 */
export function applyTemplateEvent(
	event: DaemonEventUnion,
	state: TemplateFormState,
	flags: TemplateFlags,
): { handled: boolean; clearJobSave: boolean } {
	if (event.type === "job_templates") {
		state.templates = event.templates;
		state.loading = false;
		if (!event.templates.some((row) => row.id === state.templateId)) {
			state.templateId = "";
		}
		if (flags.templateSavePending) {
			flags.templateSavePending = false;
			state.templateName = "";
			state.error = null;
		}
		return { handled: true, clearJobSave: false };
	}
	if (event.type === "session_job_source") {
		if (!flags.awaitingSource) return { handled: true, clearJobSave: false };
		flags.awaitingSource = false;
		state.loading = false;
		state.error = null;
		state.editingId = null;
		state.templateId = "";
		state.draft = draftFromSession(event);
		showScheduled();
		return { handled: true, clearJobSave: true };
	}
	if (
		event.type === "error" &&
		(event.code === "template_invalid" ||
			event.code === "template_builtin" ||
			event.code === "template_not_found")
	) {
		flags.templateSavePending = false;
		state.loading = false;
		state.error = event.message;
		return { handled: true, clearJobSave: false };
	}
	if (event.type === "error" && event.code === "session_not_found" && flags.awaitingSource) {
		flags.awaitingSource = false;
		state.loading = false;
		state.error = event.message;
		return { handled: true, clearJobSave: false };
	}
	return { handled: false, clearJobSave: false };
}
