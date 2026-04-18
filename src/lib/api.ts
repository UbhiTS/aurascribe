const BASE = "/api";

export interface Utterance {
  id?: string;
  speaker: string;
  text: string;
  start_time: number;
  end_time: number;
  // Cosine distance to the matched speaker's centroid. Smaller = more
  // confident. null/undefined when no embedding-based match was made
  // (Unknown, or pre-migration rows).
  match_distance?: number | null;
  // Wall-clock offset (seconds) into the meeting's .opus recording. Used
  // by click-to-play. Null for meetings that predate the audio feature or
  // when recording failed.
  audio_start?: number | null;
}

export interface Meeting {
  id: string;
  title: string;
  started_at: string;
  ended_at: string | null;
  status: "recording" | "processing" | "done";
  summary: string | null;
  action_items: string | null;
  vault_path: string | null;
  audio_path: string | null;
  utterances?: Utterance[];
  // Live intelligence — JSON strings for the array fields, plain text for
  // support_intelligence. Populated incrementally during recording by the
  // realtime-intelligence loop. Null until the LLM has run at least once.
  live_highlights: string | null;
  live_action_items_self: string | null;
  live_action_items_others: string | null;
  live_support_intelligence: string | null;
  // Bumped on every pill/voice change that affects this meeting's labels.
  // Compared against last_recomputed_at by `tagsPending` to flag the
  // "Recompute to apply" hint.
  last_tagged_at: string | null;
  last_recomputed_at: string | null;
}

/**
 * True when this meeting has had label-affecting changes since the last
 * recompute (or has never been recomputed and has at least one tag).
 */
export function tagsPending(m: Pick<Meeting, "last_tagged_at" | "last_recomputed_at"> | null | undefined): boolean {
  if (!m || !m.last_tagged_at) return false;
  if (!m.last_recomputed_at) return true;
  return m.last_tagged_at > m.last_recomputed_at;
}

export interface ActionItemOther {
  speaker: string;
  item: string;
}

export interface LiveIntel {
  highlights: string[];
  actionItemsSelf: string[];
  actionItemsOthers: ActionItemOther[];
  supportIntelligence: string;
}

export const EMPTY_LIVE_INTEL: LiveIntel = {
  highlights: [],
  actionItemsSelf: [],
  actionItemsOthers: [],
  supportIntelligence: "",
};

export function liveIntelFromMeeting(m: Meeting | null): LiveIntel {
  if (!m) return EMPTY_LIVE_INTEL;
  return {
    highlights: safeJsonArray<string>(m.live_highlights),
    actionItemsSelf: safeJsonArray<string>(m.live_action_items_self),
    actionItemsOthers: safeJsonArray<ActionItemOther>(m.live_action_items_others),
    supportIntelligence: m.live_support_intelligence ?? "",
  };
}

function safeJsonArray<T>(raw: string | null): T[] {
  if (!raw) return [];
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? (v as T[]) : [];
  } catch {
    return [];
  }
}

export interface Voice {
  id: string;
  name: string;
  color: string | null;
  created_at: string;
  updated_at: string;
  // Aggregates returned by /api/voices list view.
  snippet_count: number;
  total_seconds: number;
  last_tagged_at: string | null;
}

export interface VoiceSnippet {
  id: string;
  meeting_id: string | null;
  utterance_id: string | null;
  start_time: number | null;
  end_time: number | null;
  source: "manual" | "auto_match" | "merge";
  created_at: string;
  meeting_title: string | null;
  meeting_started_at: string | null;
  utterance_text: string | null;
  // Wall-clock seek offset into the meeting's .opus file for playback.
  audio_start: number | null;
}

export interface VoiceDetail {
  id: string;
  name: string;
  color: string | null;
  created_at: string;
  updated_at: string;
  snippet_count: number;
  snippets: VoiceSnippet[];
}

export interface AppStatus {
  engine_ready: boolean;
  is_recording: boolean;
  current_meeting_id: string | null;
  audio_devices: { index: number; name: string; channels: number; host_api?: string }[];
  // Friendly name of the mic the sidecar is actually pulling from right
  // now. null when idle. The dropdown in the UI can lie (default-mic, name
  // mismatch) — this is the authoritative source.
  active_audio_device: string | null;
  // True iff the sidecar's `OBSIDIAN_VAULT` is set. Authoritative — the
  // header uses this directly instead of inferring from meeting vault_paths
  // (which only land after the first markdown write).
  obsidian_configured: boolean;
}

export interface DailyBriefDecision {
  decision: string;
  context: string;
}

export interface DailyBriefActionSelf {
  item: string;
  due: string;
  source: string;
  priority: "high" | "medium" | "low";
}

export interface DailyBriefActionOther {
  speaker: string;
  item: string;
  due: string;
  source: string;
}

export interface DailyBriefPerson {
  name: string;
  takeaway: string;
}

export interface DailyBriefData {
  tldr: string;
  highlights: string[];
  decisions: DailyBriefDecision[];
  action_items_self: DailyBriefActionSelf[];
  action_items_others: DailyBriefActionOther[];
  open_threads: string[];
  people: DailyBriefPerson[];
  themes: string[];
  tomorrow_focus: string[];
  coaching: string[];
}

// Keys exposed by /api/settings/config. Keep in sync with _CONFIG_FIELDS
// in the sidecar — adding a key here without updating the backend is a
// silent no-op.
export type ConfigKey =
  | "hf_token"
  | "my_speaker_label"
  | "llm_base_url"
  | "llm_api_key"
  | "llm_model"
  | "llm_context_tokens"
  | "whisper_model"
  | "whisper_language"
  | "obsidian_vault"
  | "rt_highlights_debounce_sec"
  | "rt_highlights_max_interval_sec"
  | "rt_highlights_window_sec";

export interface ConfigFieldState {
  // What the running process is actually using. Frozen at sidecar import
  // time — differs from `override` after a save, until restart.
  effective: string | number | null;
  // What's persisted in config.json. null = using the default.
  override: string | number | null;
  // Built-in fallback when nothing is set.
  default: string | number | null;
}

export interface AppConfig {
  settings: Record<ConfigKey, ConfigFieldState>;
  config_file: string;
  // PUT responses only — true when at least one saved value differs from
  // what the live process is using (i.e. restart will change behavior).
  requires_restart?: boolean;
}

export type AppConfigPatch = Partial<Record<ConfigKey, string | number | null>>;

export interface DataDirSettings {
  // Path currently in use by the running sidecar (resolved at import time).
  effective: string;
  // What the Settings UI has persisted for next startup. Null = using
  // the default.
  override: string | null;
  // Where the app would land with nothing configured.
  default: string;
  // Where the bootstrap pointer file lives (fixed OS location — NOT moved
  // by changing data_dir). Shown as a diagnostic.
  bootstrap_file: string;
  // Only on PUT responses; true when the saved override differs from
  // what's currently loaded in memory.
  requires_restart?: boolean;
}

export interface DailyBriefResponse {
  date: string;
  brief: DailyBriefData | null;
  meeting_count: number;
  meeting_ids: string[];
  meetings: { id: string; title: string; started_at: string; ended_at: string | null; status: string }[];
  generated_at: string | null;
  is_stale: boolean;
  exists: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Request failed");
  }
  return res.json();
}

export const api = {
  status: () => request<AppStatus>("/status"),
  meetings: {
    list: (days = 2, limit = 20, offset = 0) =>
      request<Meeting[]>(`/meetings?days=${days}&limit=${limit}&offset=${offset}`),
    bulkDelete: (ids: string[]) =>
      request<{ ok: boolean; deleted: number }>("/meetings/bulk-delete", {
        method: "POST",
        body: JSON.stringify({ ids }),
      }),
    clearAll: (days = 2) =>
      request<{ ok: boolean; deleted: number }>(`/meetings/all?days=${days}`, { method: "DELETE" }),
    get: (id: string) => request<Meeting>(`/meetings/${id}`),
    start: (title: string, device?: number) =>
      request<{ meeting_id: string; status: string }>("/meetings/start", {
        method: "POST",
        body: JSON.stringify({ title, device }),
      }),
    stop: (summarize = false) =>
      request<Meeting>("/meetings/stop", {
        method: "POST",
        body: JSON.stringify({ summarize }),
      }),
    renameSpeaker: (meetingId: string, oldName: string, newName: string) =>
      request<{ ok: boolean }>(`/meetings/${meetingId}/rename-speaker`, {
        method: "POST",
        body: JSON.stringify({ meeting_id: meetingId, old_name: oldName, new_name: newName }),
      }),
    assignSpeaker: (meetingId: string, utteranceId: string, speaker: string, createIfNew = true) =>
      request<{ ok: boolean; speaker: string }>(
        `/meetings/${meetingId}/utterances/${utteranceId}/assign`,
        {
          method: "POST",
          body: JSON.stringify({ speaker, create_if_new: createIfNew }),
        },
      ),
    delete: (id: string) =>
      request<{ ok: boolean }>(`/meetings/${id}`, { method: "DELETE" }),
    rename: (id: string, title: string) =>
      request<{ ok: boolean }>(`/meetings/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      }),
    summarize: (id: string) =>
      request<Meeting>(`/meetings/${id}/summarize`, { method: "POST" }),
    trim: (id: string, opts: { before?: number; after?: number }) =>
      request<{ ok: boolean; shifted_by: number }>(`/meetings/${id}/trim`, {
        method: "POST",
        body: JSON.stringify(opts),
      }),
    split: (id: string, at: number, newTitle?: string) =>
      request<{ ok: boolean; new_meeting_id: string }>(`/meetings/${id}/split`, {
        method: "POST",
        body: JSON.stringify({ at, new_title: newTitle }),
      }),
    recompute: (id: string) =>
      request<{ ok: boolean; turns: number; updated: number }>(
        `/meetings/${id}/recompute`,
        { method: "POST" },
      ),
    audioUrl: (id: string) => `${BASE}/meetings/${id}/audio`,
  },
  voices: {
    list: () => request<Voice[]>("/voices"),
    get: (id: string) => request<VoiceDetail>(`/voices/${id}`),
    update: (id: string, patch: { name?: string; color?: string | null }) =>
      request<{ ok: boolean }>(`/voices/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      }),
    delete: (id: string) =>
      request<{ ok: boolean }>(`/voices/${id}`, { method: "DELETE" }),
    deleteSnippet: (voiceId: string, snippetId: string) =>
      request<{ ok: boolean }>(`/voices/${voiceId}/snippets/${snippetId}`, {
        method: "DELETE",
      }),
    merge: (fromId: string, intoId: string) =>
      request<{ ok: boolean; merged_into: string }>("/voices/merge", {
        method: "POST",
        body: JSON.stringify({ from_id: fromId, into_id: intoId }),
      }),
  },
  llm: {
    models: () => request<{ models: string[] }>("/models"),
  },
  intel: {
    refresh: (meetingId: string) =>
      request<{ ok: boolean }>(`/meetings/${meetingId}/intel/refresh`, { method: "POST" }),
    promptPath: () => request<{ path: string }>("/intel/prompt-path"),
    prompts: () =>
      request<{ dir: string; prompts: { name: string; filename: string; path: string }[] }>(
        "/intel/prompts",
      ),
    openPrompt: (filename: string) =>
      request<{ ok: boolean; path: string }>("/intel/open-prompt", {
        method: "POST",
        body: JSON.stringify({ filename }),
      }),
  },
  settings: {
    getDataDir: () => request<DataDirSettings>("/settings/data-dir"),
    setDataDir: (data_dir: string | null) =>
      request<DataDirSettings>("/settings/data-dir", {
        method: "PUT",
        body: JSON.stringify({ data_dir }),
      }),
    getConfig: () => request<AppConfig>("/settings/config"),
    updateConfig: (patch: AppConfigPatch) =>
      request<AppConfig>("/settings/config", {
        method: "PUT",
        body: JSON.stringify(patch),
      }),
  },
  dailyBrief: {
    get: (date: string) =>
      request<DailyBriefResponse>(`/daily-brief?date=${encodeURIComponent(date)}`),
    refresh: (date: string) =>
      request<DailyBriefResponse>(`/daily-brief/refresh?date=${encodeURIComponent(date)}`, {
        method: "POST",
      }),
  },
};
