// Dev: Vite proxies /api → sidecar, so relative URLs work.
// Prod: Tauri webview origin is `tauri://localhost` (or similar) with no
// proxy — we must hit the sidecar by its absolute URL. The port matches
// the sidecar's chosen port (8765 by default, falling back up to 8774
// when another process holds the preferred port). Discovery runs once
// on first module load and memoizes the winner in sessionStorage. Keep
// in sync with `sidecarWsUrl()` in lib/useWebSocket.ts.

const _SIDECAR_DEFAULT_PORT = 8765;
const _SIDECAR_PORT_RANGE = 10;  // try 8765..8774 inclusive
const _SIDECAR_PORT_STORAGE_KEY = "aurascribe.sidecar.port";

function _cachedSidecarPort(): number {
  if (import.meta.env.DEV) return _SIDECAR_DEFAULT_PORT;
  try {
    const raw = window.sessionStorage.getItem(_SIDECAR_PORT_STORAGE_KEY);
    const n = raw ? parseInt(raw, 10) : NaN;
    if (Number.isFinite(n) && n >= _SIDECAR_DEFAULT_PORT
        && n < _SIDECAR_DEFAULT_PORT + _SIDECAR_PORT_RANGE) {
      return n;
    }
  } catch {
    // sessionStorage unavailable — fall through
  }
  return _SIDECAR_DEFAULT_PORT;
}

export function sidecarHttpBase(): string {
  if (import.meta.env.DEV) return "";
  return `http://127.0.0.1:${_cachedSidecarPort()}`;
}

// Probe for the actual sidecar port — called once on app boot. If the
// preferred port doesn't respond within a short timeout, walks the
// fallback range until /api/status answers. Memoizes the winner so the
// rest of the app can keep using the synchronous `sidecarHttpBase()`.
// Safe to call multiple times; subsequent calls short-circuit on the
// cached value.
let _sidecarDiscoveryPromise: Promise<number> | null = null;
export async function discoverSidecarPort(): Promise<number> {
  if (import.meta.env.DEV) return _SIDECAR_DEFAULT_PORT;
  if (_sidecarDiscoveryPromise) return _sidecarDiscoveryPromise;

  _sidecarDiscoveryPromise = (async () => {
    const cached = _cachedSidecarPort();
    const tryPort = async (port: number): Promise<boolean> => {
      const ctrl = new AbortController();
      const timer = window.setTimeout(() => ctrl.abort(), 2000);
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/status`, { signal: ctrl.signal });
        return res.ok;
      } catch {
        return false;
      } finally {
        window.clearTimeout(timer);
      }
    };

    // Try cached first — fast-path when the sidecar stayed on its usual port.
    if (await tryPort(cached)) return cached;

    // Walk the fallback range. Starts at the preferred port even if the
    // cache was stale, so a sidecar that moved BACK to 8765 is found fast.
    for (let i = 0; i < _SIDECAR_PORT_RANGE; i++) {
      const p = _SIDECAR_DEFAULT_PORT + i;
      if (p === cached) continue;
      if (await tryPort(p)) {
        try {
          window.sessionStorage.setItem(_SIDECAR_PORT_STORAGE_KEY, String(p));
        } catch {
          // sessionStorage unavailable — subsequent calls will re-probe.
        }
        return p;
      }
    }
    // Give up — the rest of the app will surface the failure through
    // normal fetch errors / the WS reconnect banner.
    return cached;
  })();
  return _sidecarDiscoveryPromise;
}

// Kept for import compatibility — resolved at module load (dev: "",
// prod: http://127.0.0.1:<preferred-port>). Runtime call sites use
// `sidecarHttpBase()` instead so a mid-session port switch via the
// discovery flow is picked up without a reload.
export const SIDECAR_HTTP_BASE = sidecarHttpBase();

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
  // Action items extracted from the final summary. Parsed server-side from
  // the stored JSON TEXT column, so clients get native arrays.
  action_items: string[] | null;
  vault_path: string | null;
  audio_path: string | null;
  utterances?: Utterance[];
  // Live intelligence — populated incrementally during recording by the
  // realtime-intelligence loop. Null until the LLM has run at least once.
  // Parsed server-side from the stored JSON TEXT columns; support_intelligence
  // stays a plain text field (markdown-ish).
  live_highlights: string[] | null;
  live_action_items_self: string[] | null;
  live_action_items_others: ActionItemOther[] | null;
  live_support_intelligence: string | null;
  // Bumped on every pill/voice change that affects this meeting's labels.
  // Compared against last_recomputed_at by `tagsPending` to flag the
  // "Recompute to apply" hint.
  last_tagged_at: string | null;
  last_recomputed_at: string | null;
  // True when the user has claimed ownership of the title (typed a
  // custom one, or picked a suggestion, or clicked the freeze icon).
  // While false, the live-refinement loop + AI Summary can overwrite
  // it with a better suggestion from transcript context. Optional on
  // the type because legacy persisted rows predate the column.
  title_locked?: boolean;
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
  // All four source fields are already native arrays (parsed server-side
  // — see `normalize_meeting_row` in sidecar/aurascribe/routes/_shared.py).
  return {
    highlights: m.live_highlights ?? [],
    actionItemsSelf: m.live_action_items_self ?? [],
    actionItemsOthers: m.live_action_items_others ?? [],
    supportIntelligence: m.live_support_intelligence ?? "",
  };
}

export interface Voice {
  id: string;
  name: string;
  color: string | null;
  // File extension of the uploaded avatar image, or null if the user
  // hasn't uploaded one. Non-null means GET /api/voices/:id/avatar will
  // serve the image; null means render the generated initials circle.
  avatar_ext: string | null;
  // Descriptive metadata — surfaced inline on the Voices detail pane
  // and mirrored into the People-note frontmatter. All optional; email
  // also drives filename disambiguation when two voices share a display
  // name (e.g. "John Smith (acme)" vs. "John Smith (google)").
  email: string | null;
  org: string | null;
  role: string | null;
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
  avatar_ext: string | null;
  email: string | null;
  org: string | null;
  role: string | null;
  created_at: string;
  updated_at: string;
  snippet_count: number;
  snippets: VoiceSnippet[];
}

export interface AppStatus {
  engine_ready: boolean;
  // Populated when `engine.load()` failed (Whisper download interrupted,
  // pyannote 401 for an un-accepted HF licence, GPU OOM, missing CUDA
  // DLL). When set, engine_ready is false and the splash / welcome UI
  // surfaces this string with a Retry button that POSTs to /api/system/retry-init.
  engine_load_error: string | null;
  is_recording: boolean;
  current_meeting_id: string | null;
  audio_devices: { index: number; name: string; channels: number; host_api?: string }[];
  // WASAPI output devices usable as a loopback source. Populated from
  // sounddevice's output-capable list filtered to WASAPI — anything else
  // can't do loopback on Windows. Used by the second picker on the
  // recording bar ("Capture from: …") to attach system audio to a meeting.
  audio_output_devices: { index: number; name: string; channels: number; host_api?: string }[];
  // Friendly name of the mic the sidecar is actually pulling from right
  // now. null when idle. The dropdown in the UI can lie (default-mic, name
  // mismatch) — this is the authoritative source.
  active_audio_device: string | null;
  // True iff the sidecar's `OBSIDIAN_VAULT` is set. Authoritative — the
  // header uses this directly instead of inferring from meeting vault_paths
  // (which only land after the first markdown write).
  obsidian_configured: boolean;
  // Detected at sidecar import. Settings surfaces this as "Detected: …"
  // next to the device override, and the live page flags CPU-mode users.
  hardware: {
    device: "cuda" | "cpu";
    device_name: string | null;
    vram_gb: number | null;
  };
  // What the ASR engine is actually configured to use. Stable across the
  // session — matches sidecar/aurascribe/config.py values at import time.
  asr: {
    model: string;
    device: "cuda" | "cpu";
    compute_type: string;
  };
  // Speaker diarization runtime state. `device` is null when disabled
  // (no HF_TOKEN, licence not accepted, or pyannote failed to load).
  // cuda != asr.device can legitimately happen when torch is CPU-only but
  // ctranslate2 has CUDA — the UI surfaces this so users know why
  // diarization is slower than whisper even on a GPU machine.
  diarization: {
    enabled: boolean;
    device: "cuda" | "cpu" | null;
  };
  // sys.platform from the sidecar process — "win32", "darwin", or "linux".
  // Used by the UI to display OS-appropriate instructions and labels.
  platform?: string;
  // Auto-capture monitor snapshot. Snapshot-only — live updates arrive
  // on the WebSocket as `{ type: "auto_capture", ... }` messages.
  auto_capture?: AutoCaptureState;
}

// States the auto-capture monitor can be in. Drives the small toggle chip
// on the RecordingBar:
//   disabled   — master toggle is off; the chip shows "Auto: off"
//   listening  — monitor is hot, running VAD on the default mic
//   armed      — sustained speech detected, start_meeting is firing
//   recording  — a meeting is active (started by us OR manually)
//   error      — mic couldn't be opened (permission, device in use, etc.);
//                the monitor auto-retries with exponential backoff
export type AutoCaptureStateKind =
  | "disabled" | "listening" | "armed" | "recording" | "error";

export interface AutoCaptureState {
  enabled: boolean;
  state: AutoCaptureStateKind;
  // EMA-smoothed Silero VAD confidence in [0, 1]. Drives the tiny
  // activity bar inside the chip when listening.
  confidence: number;
  // Seconds of sustained silence observed DURING an auto-started
  // recording. Reaches `auto_capture_stop_silence_sec` → auto-stop fires.
  // Zero for manually-started meetings (they never auto-stop).
  silent_seconds?: number;
  // The deadline silent_seconds is racing toward — mirrors
  // `auto_capture_stop_silence_sec`. Lets the UI render a countdown
  // without a separate settings round-trip.
  stop_silence_seconds?: number;
  // Silence-duration threshold before the UI surfaces the countdown
  // on the Stop button. Mirrors `auto_capture_countdown_after_silence_sec`.
  countdown_after_silence_sec?: number;
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
  | "whisper_device"
  | "whisper_compute_type"
  | "whisper_language"
  | "obsidian_vault"
  | "rt_highlights_debounce_sec"
  | "rt_highlights_max_interval_sec"
  | "rt_highlights_window_sec"
  // Advanced knobs.
  | "chunk_duration"
  | "silence_duration"
  | "vad_threshold"
  | "aec_tail_ms"
  | "voice_match_threshold_multi"
  | "voice_match_threshold_solo"
  | "voice_ratio_margin"
  | "min_voice_samples"
  | "provisional_threshold"
  | "speculative_interval_sec"
  | "speculative_window_sec"
  | "obsidian_write_interval_sec"
  | "obsidian_write_chunks"
  | "daily_brief_auto_refresh"
  // Auto-capture (sustained-speech auto-start/stop).
  | "auto_capture_enabled"
  | "auto_capture_start_speech_sec"
  | "auto_capture_stop_silence_sec"
  | "auto_capture_vad_threshold"
  | "auto_capture_countdown_after_silence_sec";

export interface ConfigFieldState {
  // What the running process is actually using. Frozen at sidecar import
  // time — differs from `override` after a save, until restart.
  effective: string | number | boolean | null;
  // What's persisted in config.json. null = using the default.
  override: string | number | boolean | null;
  // Built-in fallback when nothing is set.
  default: string | number | boolean | null;
}

export interface AppConfig {
  settings: Record<ConfigKey, ConfigFieldState>;
  config_file: string;
  // PUT responses only — true when at least one saved value differs from
  // what the live process is using (i.e. restart will change behavior).
  requires_restart?: boolean;
}

export type AppConfigPatch = Partial<Record<ConfigKey, string | number | boolean | null>>;

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

/** Typed HTTP error so callers can branch on status + structured detail
 *  (e.g. FastAPI's 403 for mic-permission denial, which carries a
 *  `{message, kind}` dict in `detail`). The `message` exposed via the
 *  Error.message property is always a string — safe to render as-is. */
export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    // Unwrap a string or `{message}` so `error.message` is always readable.
    const msg =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail
          ? String((detail as { message: unknown }).message)
          : "Request failed";
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData bodies must NOT carry a Content-Type header — the browser
  // adds its own with the multipart boundary. Setting application/json
  // here would break the upload. For plain JSON bodies we keep the
  // default so every caller doesn't have to set it explicitly.
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const headers: HeadersInit = isFormData ? {} : { "Content-Type": "application/json" };
  // Resolve the base URL per-call so a discover-port flow that updates
  // sessionStorage mid-session is picked up without a page reload.
  const res = await fetch(`${sidecarHttpBase()}/api${path}`, {
    headers,
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail);
  }
  return res.json();
}

export const api = {
  status: () => request<AppStatus>("/status"),
  system: {
    openMicSettings: () =>
      request<{ ok: boolean; reason?: string }>("/system/open-mic-settings", {
        method: "POST",
      }),
    // Kick off a retry of the failed engine init. Returns immediately;
    // progress streams via WS status events. Used by the splash's
    // engine-load-error dialog.
    retryInit: () =>
      request<{ ok: boolean; message?: string }>("/system/retry-init", {
        method: "POST",
      }),
  },
  meetings: {
    list: (days = 2, limit = 20, offset = 0, date?: string) => {
      const params = new URLSearchParams();
      if (date) params.set("date", date);
      else params.set("days", String(days));
      params.set("limit", String(limit));
      params.set("offset", String(offset));
      return request<Meeting[]>(`/meetings?${params.toString()}`);
    },
    bulkDelete: (ids: string[]) =>
      request<{ ok: boolean; deleted: number }>("/meetings/bulk-delete", {
        method: "POST",
        body: JSON.stringify({ ids }),
      }),
    clearAll: (days = 2) =>
      request<{ ok: boolean; deleted: number }>(`/meetings/all?days=${days}`, { method: "DELETE" }),
    get: (id: string) => request<Meeting>(`/meetings/${id}`),
    start: (
      title: string,
      opts: {
        device?: number;
        loopbackDevice?: number;
        captureMic?: boolean;
      } = {},
    ) =>
      request<{ meeting_id: string; status: string }>("/meetings/start", {
        method: "POST",
        body: JSON.stringify({
          title,
          device: opts.device,
          loopback_device: opts.loopbackDevice,
          // Default true on the backend — only send when explicitly false
          // so older callers stay mic-only by accident-free design.
          capture_mic: opts.captureMic ?? true,
        }),
      }),
    stop: (summarize = false) =>
      request<Meeting>("/meetings/stop", {
        method: "POST",
        body: JSON.stringify({ summarize }),
      }),
    // Pre-recording monitor: opens the capture pipeline (same mic +
    // loopback plumbing a real meeting uses) but skips transcription —
    // only the ~30Hz audio_level WS broadcast is emitted, so the
    // visualizers can animate the exact signal you'd record if you hit
    // Start. Automatically torn down server-side when a real meeting
    // starts; callers don't need to sequence that.
    monitorStart: (opts: {
      device?: number;
      loopbackDevice?: number;
      captureMic?: boolean;
    } = {}) =>
      request<{ ok: boolean }>("/meetings/monitor/start", {
        method: "POST",
        body: JSON.stringify({
          device: opts.device,
          loopback_device: opts.loopbackDevice,
          capture_mic: opts.captureMic ?? true,
        }),
      }),
    monitorStop: () =>
      request<{ ok: boolean }>("/meetings/monitor/stop", { method: "POST" }),
    // `enrollUtteranceId` (provisional cluster fold only): enroll just that
    // single anchor utterance's embedding into the target voice's pool.
    // Without it, the cluster is relabeled for transcript display but no
    // sample is added to the voice library — keeping "1 click = 1 sample".
    renameSpeaker: (
      meetingId: string,
      oldName: string,
      newName: string,
      enrollUtteranceId?: string,
    ) =>
      request<{ ok: boolean }>(`/meetings/${meetingId}/rename-speaker`, {
        method: "POST",
        body: JSON.stringify({
          meeting_id: meetingId,
          old_name: oldName,
          new_name: newName,
          enroll_utterance_id: enrollUtteranceId,
        }),
      }),
    // `enroll=false` updates the speaker label without folding the embedding
    // into the voice's pool. Used for the trailing utterances of a merged
    // bubble where the user's single click should produce a single sample.
    assignSpeaker: (
      meetingId: string,
      utteranceId: string,
      speaker: string,
      createIfNew = true,
      enroll = true,
    ) =>
      request<{ ok: boolean; speaker: string }>(
        `/meetings/${meetingId}/utterances/${utteranceId}/assign`,
        {
          method: "POST",
          body: JSON.stringify({ speaker, create_if_new: createIfNew, enroll }),
        },
      ),
    // Import an audio file as a new completed meeting. The sidecar runs
    // ffmpeg → 16 kHz mono Opus → transcribe + diarize → AI summary, then
    // returns the new meeting's `started_at` so the library can navigate
    // to its date (imported recordings keep their original timestamp via
    // File.lastModified, so a week-old file appears at its real date).
    importAudio: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("last_modified_ms", String(file.lastModified || Date.now()));
      return request<Meeting & { started_at: string }>("/meetings/import", {
        method: "POST",
        body: fd,
      });
    },
    delete: (id: string) =>
      request<{ ok: boolean }>(`/meetings/${id}`, { method: "DELETE" }),
    rename: (id: string, title: string) =>
      request<{ ok: boolean }>(`/meetings/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      }),
    // Toggle the title-frozen flag WITHOUT changing the title. Typing a
    // custom title via `rename()` already locks automatically; this is
    // for the explicit lock/unlock icon next to the title.
    setTitleLock: (id: string, locked: boolean) =>
      request<{ ok: boolean; locked: boolean }>(`/meetings/${id}/title-lock`, {
        method: "PATCH",
        body: JSON.stringify({ locked }),
      }),
    summarize: (id: string) =>
      request<Meeting>(`/meetings/${id}/summarize`, { method: "POST" }),
    suggestTitle: (id: string) =>
      // Response includes the refreshed Meeting because this endpoint
      // also persists a fresh AI summary as a side effect — a single
      // LLM call produces both artefacts, so we return both and let
      // the caller update its local state without a second fetch.
      request<{ suggestions: string[]; meeting: Meeting }>(
        `/meetings/${id}/suggest-title`,
        { method: "POST" },
      ),
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
    audioUrl: (id: string) => `${sidecarHttpBase()}/api/meetings/${id}/audio`,
  },
  voices: {
    list: () => request<Voice[]>("/voices"),
    get: (id: string) => request<VoiceDetail>(`/voices/${id}`),
    update: (
      id: string,
      patch: {
        name?: string;
        color?: string | null;
        // Empty string clears the field (persisted as NULL on the
        // backend); undefined leaves it untouched.
        email?: string;
        org?: string;
        role?: string;
      },
    ) =>
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
    // Cache-buster defaults to the voice's updated_at so the WebView
    // reliably fetches the new image after an upload. Works fine against
    // the server's 10s Cache-Control max-age.
    avatarUrl: (id: string, cacheKey?: string | null) =>
      `${sidecarHttpBase()}/api/voices/${id}/avatar${cacheKey ? `?v=${encodeURIComponent(cacheKey)}` : ""}`,
    uploadAvatar: (id: string, file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return request<{ ok: boolean; avatar_ext: string }>(
        `/voices/${id}/avatar`,
        { method: "POST", body: fd },
      );
    },
    deleteAvatar: (id: string) =>
      request<{ ok: boolean }>(`/voices/${id}/avatar`, { method: "DELETE" }),
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
  autoCapture: {
    // Snapshot — only needed for the initial render. Live updates ride
    // on the WebSocket `auto_capture` message type; see useWebSocket.ts.
    get: () => request<AutoCaptureState>("/auto-capture"),
    // Persists to config.json AND applies to the running monitor in one
    // call — no restart required, which is the point of surfacing this
    // as a front-and-center toggle instead of burying it in Settings.
    setEnabled: (enabled: boolean) =>
      request<AutoCaptureState>("/auto-capture", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      }),
  },
};
