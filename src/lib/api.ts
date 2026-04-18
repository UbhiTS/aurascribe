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
  utterances?: Utterance[];
  // Live intelligence — JSON strings for the array fields, plain text for
  // support_intelligence. Populated incrementally during recording by the
  // realtime-intelligence loop. Null until the LLM has run at least once.
  live_highlights: string | null;
  live_action_items_self: string | null;
  live_action_items_others: string | null;
  live_support_intelligence: string | null;
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

export interface Person {
  id: string;
  name: string;
  vault_path: string | null;
  created_at: string;
}

export interface AppStatus {
  engine_ready: boolean;
  is_recording: boolean;
  current_meeting_id: string | null;
  audio_devices: { index: number; name: string; channels: number; host_api?: string }[];
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
  },
  people: {
    list: () => request<Person[]>("/people"),
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
  enroll: {
    start: (name: string, duration?: number) =>
      request<{ person_id: string; name: string }>("/enroll/start", {
        method: "POST",
        body: JSON.stringify({ name, duration: duration ?? 10 }),
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
