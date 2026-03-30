const BASE = "/api";

export interface Utterance {
  speaker: string;
  text: string;
  start_time: number;
  end_time: number;
}

export interface Meeting {
  id: number;
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
  id: number;
  name: string;
  vault_path: string | null;
  created_at: string;
}

export interface AppStatus {
  is_recording: boolean;
  current_meeting_id: number | null;
  audio_devices: { index: number; name: string; channels: number }[];
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
    bulkDelete: (ids: number[]) =>
      request<{ ok: boolean; deleted: number }>("/meetings/bulk-delete", {
        method: "POST",
        body: JSON.stringify({ ids }),
      }),
    clearAll: (days = 2) =>
      request<{ ok: boolean; deleted: number }>(`/meetings/all?days=${days}`, { method: "DELETE" }),
    get: (id: number) => request<Meeting>(`/meetings/${id}`),
    start: (title: string, device?: number) =>
      request<{ meeting_id: number; status: string }>("/meetings/start", {
        method: "POST",
        body: JSON.stringify({ title, device }),
      }),
    stop: (summarize = false) =>
      request<Meeting>("/meetings/stop", {
        method: "POST",
        body: JSON.stringify({ summarize }),
      }),
    renameSpeaker: (meetingId: number, oldName: string, newName: string) =>
      request<{ ok: boolean }>(`/meetings/${meetingId}/rename-speaker`, {
        method: "POST",
        body: JSON.stringify({ meeting_id: meetingId, old_name: oldName, new_name: newName }),
      }),
    delete: (id: number) =>
      request<{ ok: boolean }>(`/meetings/${id}`, { method: "DELETE" }),
    rename: (id: number, title: string) =>
      request<{ ok: boolean }>(`/meetings/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      }),
    summarize: (id: number) =>
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
      request<{ person_id: number; name: string }>("/enroll/start", {
        method: "POST",
        body: JSON.stringify({ name, duration: duration ?? 10 }),
      }),
  },
};
