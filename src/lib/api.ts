const BASE = "/api";

export interface Utterance {
  id?: string;
  speaker: string;
  text: string;
  start_time: number;
  end_time: number;
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
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
  },
  people: {
    list: () => request<Person[]>("/people"),
  },
  llm: {
    models: () => request<{ models: string[] }>("/models"),
  },
  enroll: {
    start: (name: string, duration?: number) =>
      request<{ person_id: string; name: string }>("/enroll/start", {
        method: "POST",
        body: JSON.stringify({ name, duration: duration ?? 10 }),
      }),
  },
};
