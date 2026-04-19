import { useEffect, useMemo, useState } from "react";
import {
  Cpu, Mic, FolderCheck, FolderX, Sparkles, FileText, ExternalLink,
  HardDrive, AlertTriangle, Languages, Users, Zap, SlidersHorizontal,
} from "lucide-react";
import { api } from "../lib/api";
import type {
  AppStatus, DataDirSettings, AppConfig, AppConfigPatch, ConfigKey,
} from "../lib/api";

interface Props {
  appStatus: AppStatus | null;
  obsidianConfigured: boolean;
}

// Fields grouped into UI sections. Save buttons apply to their own section
// only — a bad Obsidian path shouldn't block saving LLM changes.
const SECTION_KEYS = {
  llm: ["llm_base_url", "llm_api_key", "llm_model", "llm_context_tokens"] as ConfigKey[],
  speech: ["whisper_model", "whisper_device", "whisper_compute_type", "whisper_language", "my_speaker_label"] as ConfigKey[],
  diarization: ["hf_token"] as ConfigKey[],
  obsidian: ["obsidian_vault"] as ConfigKey[],
  realtime: ["rt_highlights_debounce_sec", "rt_highlights_max_interval_sec", "rt_highlights_window_sec"] as ConfigKey[],
  adv_chunking: ["chunk_duration", "silence_duration", "vad_threshold"] as ConfigKey[],
  adv_aec: ["aec_tail_ms"] as ConfigKey[],
  adv_speakers: [
    "voice_match_threshold_multi",
    "voice_match_threshold_solo",
    "voice_ratio_margin",
    "min_voice_samples",
    "provisional_threshold",
  ] as ConfigKey[],
  adv_partials: ["speculative_interval_sec", "speculative_window_sec"] as ConfigKey[],
  adv_obsidian: ["obsidian_write_interval_sec", "obsidian_write_chunks"] as ConfigKey[],
  adv_daily_brief: ["daily_brief_auto_refresh"] as ConfigKey[],
};

// Per-field option lists for fields rendered as a <select>. Keys not listed
// here fall through to a plain <input>. Option strings are what gets stored
// in config.json — matches what faster-whisper / ctranslate2 accept.
const ENUM_OPTIONS: Partial<Record<ConfigKey, readonly string[]>> = {
  whisper_device: ["cuda", "cpu"],
  whisper_compute_type: ["float16", "int8_float16", "int8", "float32"],
};

const NUMERIC_KEYS = new Set<ConfigKey>([
  "llm_context_tokens",
  "rt_highlights_debounce_sec",
  "rt_highlights_max_interval_sec",
  "rt_highlights_window_sec",
  "chunk_duration",
  "silence_duration",
  "vad_threshold",
  "aec_tail_ms",
  "voice_match_threshold_multi",
  "voice_match_threshold_solo",
  "voice_ratio_margin",
  "min_voice_samples",
  "provisional_threshold",
  "speculative_interval_sec",
  "speculative_window_sec",
  "obsidian_write_interval_sec",
  "obsidian_write_chunks",
]);

// Boolean fields render as a three-way select (auto / on / off) and save
// as real JSON booleans. "auto" clears the override so the built-in
// default comes back on next restart.
const BOOL_KEYS = new Set<ConfigKey>([
  "daily_brief_auto_refresh",
]);

// Stringifies a stored config value for display inside an <input>. Null /
// undefined map to empty string (the "no override" signal on save). Booleans
// map to "on" / "off" so the select control can round-trip them cleanly.
function valToDraft(v: string | number | boolean | null | undefined): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "on" : "off";
  return String(v);
}

interface PromptFile {
  name: string;
  filename: string;
  path: string;
}

export function Settings({ appStatus, obsidianConfigured }: Props) {
  const [lmModels, setLmModels] = useState<string[]>([]);
  const [prompts, setPrompts] = useState<PromptFile[]>([]);
  const [promptsDir, setPromptsDir] = useState<string>("");
  const [openingPath, setOpeningPath] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);

  const [dataDir, setDataDir] = useState<DataDirSettings | null>(null);
  // Draft for the data-dir input. Empty string = clear override (fall back
  // to default on next restart).
  const [dataDirDraft, setDataDirDraft] = useState<string>("");
  const [savingDataDir, setSavingDataDir] = useState(false);
  const [dataDirError, setDataDirError] = useState<string | null>(null);
  const [dataDirSavedAt, setDataDirSavedAt] = useState<number | null>(null);

  // Full config.json snapshot + per-field text drafts (strings only —
  // numeric fields are parsed on save). `savingSection` tracks which group
  // is currently round-tripping; only one save can be in flight at a time
  // since they all write to the same file.
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [drafts, setDrafts] = useState<Partial<Record<ConfigKey, string>>>({});
  const [savingSection, setSavingSection] = useState<string | null>(null);
  const [sectionErrors, setSectionErrors] = useState<Record<string, string | null>>({});
  const [sectionSavedAt, setSectionSavedAt] = useState<Record<string, number>>({});

  useEffect(() => {
    api.llm.models().then((r) => setLmModels(r.models)).catch(() => {});
    api.intel.prompts()
      .then((r) => { setPrompts(r.prompts); setPromptsDir(r.dir); })
      .catch(() => {});
    api.settings.getDataDir()
      .then((r) => {
        setDataDir(r);
        setDataDirDraft(r.override ?? "");
      })
      .catch(() => {});
    api.settings.getConfig()
      .then((r) => {
        setCfg(r);
        const d: Partial<Record<ConfigKey, string>> = {};
        for (const [k, field] of Object.entries(r.settings) as [ConfigKey, typeof r.settings[ConfigKey]][]) {
          d[k] = valToDraft(field.override);
        }
        setDrafts(d);
      })
      .catch(() => {});
  }, []);

  const setDraft = (key: ConfigKey, value: string) => {
    setDrafts((prev) => ({ ...prev, [key]: value }));
  };

  // True when the draft for `key` diverges from what's in config.json.
  const isDirty = (key: ConfigKey): boolean => {
    if (!cfg) return false;
    const persisted = valToDraft(cfg.settings[key].override);
    return (drafts[key] ?? "") !== persisted;
  };

  // Build a PATCH from the dirty fields within a section. Empty strings
  // send `null` to clear the override (revert to default on next restart).
  // Numbers parse here so a malformed "1.2.3" surfaces before we hit the
  // network.
  const buildPatch = (keys: ConfigKey[]): AppConfigPatch => {
    const patch: AppConfigPatch = {};
    for (const k of keys) {
      if (!isDirty(k)) continue;
      const raw = (drafts[k] ?? "").trim();
      if (raw === "") {
        patch[k] = null;
        continue;
      }
      if (BOOL_KEYS.has(k)) {
        patch[k] = raw === "on" ? true : raw === "off" ? false : null;
        continue;
      }
      if (NUMERIC_KEYS.has(k)) {
        const n = Number(raw);
        if (Number.isNaN(n)) {
          throw new Error(`${k} must be a number`);
        }
        patch[k] = n;
      } else {
        patch[k] = raw;
      }
    }
    return patch;
  };

  const saveSection = async (sectionId: string, keys: ConfigKey[]) => {
    if (!cfg) return;
    setSavingSection(sectionId);
    setSectionErrors((e) => ({ ...e, [sectionId]: null }));
    try {
      const patch = buildPatch(keys);
      if (Object.keys(patch).length === 0) return;
      const next = await api.settings.updateConfig(patch);
      setCfg(next);
      // Re-seed drafts for this section from the new persisted state, so
      // the dirty check resets cleanly.
      setDrafts((prev) => {
        const out = { ...prev };
        for (const k of keys) out[k] = valToDraft(next.settings[k].override);
        return out;
      });
      setSectionSavedAt((s) => ({ ...s, [sectionId]: Date.now() }));
    } catch (e: any) {
      setSectionErrors((errs) => ({ ...errs, [sectionId]: e?.message ?? String(e) }));
    } finally {
      setSavingSection(null);
    }
  };

  // One global "needs restart" nudge for the whole config area. True when
  // any persisted value differs from what the running process is using.
  const configNeedsRestart = useMemo(() => {
    if (!cfg) return false;
    return Object.values(cfg.settings).some((f) => {
      const persisted = f.override !== null ? f.override : f.default;
      return persisted !== f.effective;
    });
  }, [cfg]);

  const dataDirDirty = dataDir ? dataDirDraft !== (dataDir.override ?? "") : false;
  const canSaveDataDir = dataDirDirty && !savingDataDir;
  const dataDirNeedsRestart = dataDir
    ? dataDir.override !== null && dataDir.override !== dataDir.effective
    : false;

  const handleSaveDataDir = async () => {
    if (!dataDir) return;
    setSavingDataDir(true);
    setDataDirError(null);
    try {
      const next = await api.settings.setDataDir(dataDirDraft === "" ? null : dataDirDraft);
      setDataDir(next);
      setDataDirDraft(next.override ?? "");
      setDataDirSavedAt(Date.now());
    } catch (e: any) {
      setDataDirError(e?.message ?? String(e));
    } finally {
      setSavingDataDir(false);
    }
  };

  const handleOpen = async (filename: string, path: string) => {
    setOpeningPath(path);
    setOpenError(null);
    try {
      await api.intel.openPrompt(filename);
    } catch (e: any) {
      setOpenError(e?.message ?? String(e));
    } finally {
      setOpeningPath(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto scrollbar-thin">
      <div className="max-w-5xl mx-auto px-6 py-6 space-y-5">
        <div>
          <h1 className="text-lg font-semibold text-gray-100">Settings &amp; System Status</h1>
          <p className="text-sm text-gray-400 mt-1">
            Everything you change here is saved to your data folder — copy that folder to a new
            PC and all your settings come with you. Most changes take effect after restarting
            AuraScribe.
          </p>
          {configNeedsRestart && (
            <div className="mt-3 flex items-start gap-2 px-3 py-2 rounded-lg border border-amber-700/50 bg-amber-900/20">
              <AlertTriangle size={12} className="text-amber-400 flex-shrink-0 mt-0.5" />
              <p className="text-[11px] text-amber-200">
                Restart AuraScribe to apply saved settings. Until then, the running process is
                using the previous values.
              </p>
            </div>
          )}
        </div>

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            <HardDrive size={14} className="text-brand-400" />
            <h2 className="text-sm font-semibold text-gray-100">Data Directory</h2>
          </div>
          <p className="text-[11px] text-gray-500 mb-3">
            One folder holds all your AuraScribe data — the transcript database,
            meeting audio recordings, and downloaded AI models. Point a new install at
            the same folder to pick up right where you left off.
          </p>

          <PathField
            label="Data folder"
            value={dataDirDraft}
            onChange={setDataDirDraft}
            placeholder={dataDir?.default ?? ""}
            effective={dataDir?.effective}
            overridden={dataDir?.override != null}
            disabled={!dataDir || savingDataDir}
          />

          <div className="flex items-center gap-3 mt-4">
            <button
              onClick={handleSaveDataDir}
              disabled={!canSaveDataDir}
              className="text-xs font-medium px-3 py-1.5 rounded-lg
                         bg-brand-600 hover:bg-brand-500 text-white
                         disabled:bg-gray-800 disabled:text-gray-500 disabled:cursor-not-allowed
                         transition-colors"
            >
              {savingDataDir ? "Saving…" : "Save"}
            </button>
            {dataDirSavedAt && !canSaveDataDir && !dataDirError && (
              <span className="text-[11px] text-emerald-400">Saved.</span>
            )}
            {dataDirError && (
              <span className="text-[11px] text-red-400">{dataDirError}</span>
            )}
          </div>

          {dataDirNeedsRestart && (
            <div className="mt-3 flex items-start gap-2 px-3 py-2 rounded-lg border border-amber-700/50 bg-amber-900/20">
              <AlertTriangle size={12} className="text-amber-400 flex-shrink-0 mt-0.5" />
              <p className="text-[11px] text-amber-200">
                Restart AuraScribe to start using the new location. Existing data at the
                old folder isn&apos;t moved — copy it across manually if you want to keep it.
              </p>
            </div>
          )}

          {dataDir?.bootstrap_file && (
            <p className="text-[10px] text-gray-600 mt-3 font-mono break-all">
              pointer file: {dataDir.bootstrap_file}
            </p>
          )}
        </section>

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            <FileText size={14} className="text-amber-400" />
            <h2 className="text-sm font-semibold text-gray-100">Prompt Files</h2>
          </div>
          <p className="text-[11px] text-gray-500 mb-3">
            Text files that tell the AI how to extract highlights, action items, and
            talking points. Open them in any editor to tune the tone and style of
            AuraScribe's AI — changes apply on the next request, no restart needed.
          </p>
          {prompts.length === 0 ? (
            <p className="text-xs text-gray-500 italic">No prompt files found.</p>
          ) : (
            <ul className="space-y-1.5">
              {prompts.map((p) => (
                <li key={p.path}>
                  <button
                    onClick={() => handleOpen(p.filename, p.path)}
                    disabled={openingPath === p.path}
                    title={p.path}
                    className="group w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-left
                               bg-gray-800/40 hover:bg-gray-800/80 border border-gray-800 hover:border-brand-700
                               transition-colors disabled:opacity-50"
                  >
                    <FileText size={12} className="text-amber-400 flex-shrink-0" />
                    <span className="text-xs text-gray-200 font-medium flex-shrink-0">{p.name}</span>
                    <span className="text-[10px] text-gray-500 font-mono truncate">{p.filename}</span>
                    <ExternalLink size={11} className="ml-auto text-gray-500 group-hover:text-brand-400 flex-shrink-0" />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {promptsDir && (
            <p className="text-[10px] text-gray-600 mt-2.5 font-mono break-all">{promptsDir}</p>
          )}
          {openError && (
            <p className="text-[11px] text-red-400 mt-2">Could not open file: {openError}</p>
          )}
        </section>

        {/* ── LLM Provider ──────────────────────────────────────────── */}
        <ConfigSection
          icon={<Sparkles size={14} className="text-brand-400" />}
          title="AI Model (LLM)"
          description="AuraScribe uses an AI chat model for meeting summaries, live coaching, and daily briefs. Point it at any OpenAI-compatible service — LM Studio or Ollama running on your PC, or a cloud provider like OpenAI, Gemini, OpenRouter, or Anthropic."
          sectionId="llm"
          keys={SECTION_KEYS.llm}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("llm", SECTION_KEYS.llm)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_base_url" label="Server address" hint="Where AuraScribe sends AI requests. Use http://127.0.0.1:1234/v1 for LM Studio on this PC, or https://api.openai.com/v1 for OpenAI." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_api_key" label="API key" type="password" hint="Your provider's secret key. LM Studio and Ollama don't check it — any non-empty text works." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_model" label="Model name" hint="The exact model name your provider uses — for example, gpt-4o, gemini-2.0-flash, or the name of a model you've loaded locally." />
          <TokenSlider cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_context_tokens" label="Context window"
            stops={[4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576]}
            hint="How much text the model can read at once. Bigger models with larger windows let the Daily Brief cover more of your day in a single call." />
          <p className="text-[11px] text-gray-500 mt-1">
            Status: {lmModels.length > 0
              ? `Connected — ${lmModels.length} model(s) available, first is ${lmModels[0]}`
              : "Not reachable — check the server address above"}
          </p>
        </ConfigSection>

        {/* ── Speech & Transcription ───────────────────────────────── */}
        <ConfigSection
          icon={<Languages size={14} className="text-emerald-400" />}
          title="Speech Recognition"
          description="Which Whisper model turns your audio into text, and whether it runs on your GPU or CPU. AuraScribe picks sensible defaults based on your hardware — only change these if you know what you're doing."
          sectionId="speech"
          keys={SECTION_KEYS.speech}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("speech", SECTION_KEYS.speech)}
        >
          {appStatus?.hardware && (
            <div className="col-span-full -mt-1 mb-1 flex items-center gap-2 px-2.5 py-1.5 rounded-lg border border-gray-800 bg-gray-950/50 text-[11px] text-gray-400">
              <Cpu size={11} className="text-brand-400 flex-shrink-0" />
              <span>
                Detected: <span className="text-gray-200 font-mono">{appStatus.hardware.device}</span>
                {appStatus.hardware.device_name && (
                  <> · <span className="text-gray-200">{appStatus.hardware.device_name}</span></>
                )}
                {appStatus.hardware.vram_gb != null && (
                  <> · <span className="text-gray-300">{appStatus.hardware.vram_gb} GB VRAM</span></>
                )}
              </span>
            </div>
          )}
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="whisper_model" label="Whisper model" hint="Bigger models are more accurate but need more GPU/CPU. Options: large-v3-turbo (best on GPU), large-v3, medium, small, base, tiny." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="whisper_device" label="Runs on"
            hint="cuda uses your NVIDIA GPU (fast). cpu works on any PC but is 5–10× slower." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="whisper_compute_type" label="Precision"
            hint="Trade speed for memory. float16 is fastest (GPU only). int8 uses the least memory. int8_float16 is a middle ground for smaller GPUs." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="whisper_language" label="Language" hint="Two-letter language code: en for English, es for Spanish, fr for French, de for German, etc. Leave blank to auto-detect." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="my_speaker_label" label="Your name in transcripts" hint="The label that appears next to your lines (e.g. Me, Tarun, Alex)." />
        </ConfigSection>

        {/* ── Diarization (HuggingFace token) ──────────────────────── */}
        <ConfigSection
          icon={<Users size={14} className="text-purple-400" />}
          title="Speaker Identification"
          description="AuraScribe uses a free HuggingFace model to tell speakers apart. Paste your access token here once, and remember to accept the model licence on the three pyannote pages on huggingface.co."
          sectionId="diarization"
          keys={SECTION_KEYS.diarization}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("diarization", SECTION_KEYS.diarization)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="hf_token" label="HuggingFace token" type="password"
            hint="Get a free token at huggingface.co/settings/tokens, then accept the licences on the pyannote/speaker-diarization-3.1, pyannote/segmentation-3.0, and pyannote/wespeaker-voxceleb-resnet34-LM pages." />
        </ConfigSection>

        {/* ── Obsidian (editable vault path) ───────────────────────── */}
        <ConfigSection
          icon={obsidianConfigured
            ? <FolderCheck size={14} className="text-emerald-400" />
            : <FolderX size={14} className="text-gray-500" />}
          title="Obsidian Integration"
          description="Optional — mirror your meetings, people notes, and daily briefs into an Obsidian vault as Markdown files so they show up alongside your other notes."
          sectionId="obsidian"
          keys={SECTION_KEYS.obsidian}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("obsidian", SECTION_KEYS.obsidian)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="obsidian_vault" label="Vault folder"
            hint="Full path to your Obsidian vault folder. Leave empty to skip writing Markdown files." />
          <p className="text-[11px] text-gray-500 mt-1">
            Meetings are saved under <code>&lt;vault&gt;\AuraScribe\Meetings\</code>, people notes under <code>People\</code>, and daily briefs under <code>Daily\</code>.
          </p>
        </ConfigSection>

        {/* ── Live Intelligence cadence ────────────────────────────── */}
        <ConfigSection
          icon={<Zap size={14} className="text-amber-400" />}
          title="Live Intelligence"
          description="Controls how often the AI re-reads the meeting during recording to extract highlights and suggest talking points. Lower values feel snappier; higher values go easier on the AI model."
          sectionId="realtime"
          keys={SECTION_KEYS.realtime}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("realtime", SECTION_KEYS.realtime)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="rt_highlights_debounce_sec" label="Wait after pause (seconds)" type="number"
            hint="How long to wait after someone stops talking before asking the AI for fresh highlights." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="rt_highlights_max_interval_sec" label="Maximum wait (seconds)" type="number"
            hint="Even if people keep talking nonstop, refresh at least this often so the panel doesn't go stale." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="rt_highlights_window_sec" label="Conversation window (seconds)" type="number"
            hint="How many seconds of recent conversation the AI sees each time it refreshes." />
        </ConfigSection>

        {/* ── Advanced Settings ────────────────────────────────────── */}
        <section className="rounded-xl border border-amber-900/40 bg-amber-950/10 p-4">
          <div className="flex items-center gap-2 mb-2">
            <SlidersHorizontal size={14} className="text-amber-400" />
            <h2 className="text-sm font-semibold text-gray-100">Advanced Settings</h2>
          </div>
          <p className="text-[11px] text-amber-200/80">
            Expert-level knobs that control how AuraScribe listens, transcribes, and
            identifies speakers. The defaults work well for most people — only touch
            these if something specific feels off, and revert to <em>auto</em> if you're unsure.
          </p>
        </section>

        {/* ── Advanced: Audio chunking ────────────────────────────── */}
        <ConfigSection
          icon={<Languages size={14} className="text-emerald-400/70" />}
          title="Audio chunking & voice detection"
          description="How AuraScribe slices continuous audio into sentences for transcription. Tweak these only if sentences are being cut in half, merged together, or if speech isn't being detected in your environment."
          sectionId="adv_chunking"
          keys={SECTION_KEYS.adv_chunking}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("adv_chunking", SECTION_KEYS.adv_chunking)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="chunk_duration" label="Chunk length (seconds)" type="number"
            hint="How many seconds of audio AuraScribe transcribes at a time. Shorter = faster partial results; longer = fewer but more complete sentences." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="silence_duration" label="End-of-sentence pause (seconds)" type="number"
            hint="How long a pause must be before AuraScribe treats it as the end of a sentence. Lower for fast talkers, higher for thoughtful speakers." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="vad_threshold" label="Speech detection sensitivity (0 – 1)" type="number"
            hint="How confident AuraScribe needs to be that a sound is speech. Raise in noisy rooms; lower if a quiet mic or whispers are being missed." />
        </ConfigSection>

        {/* ── Advanced: Echo cancellation ─────────────────────────── */}
        <ConfigSection
          icon={<Zap size={14} className="text-amber-400/70" />}
          title="Echo cancellation (Mix mode)"
          description="Only matters in Mix mode, where AuraScribe captures both your mic and your speakers and has to cancel the speaker audio out of the mic signal."
          sectionId="adv_aec"
          keys={SECTION_KEYS.adv_aec}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("adv_aec", SECTION_KEYS.adv_aec)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="aec_tail_ms" label="Echo memory (milliseconds)" type="number"
            hint="How long the echo canceller 'remembers' speaker audio when removing it from your mic. Raise this if Mix mode still sounds reverberant in a large room; lower it if your voice sounds muffled." />
        </ConfigSection>

        {/* ── Advanced: Speaker identification ────────────────────── */}
        <ConfigSection
          icon={<Users size={14} className="text-purple-400/70" />}
          title="Speaker identification tuning"
          description="How strict AuraScribe is when matching voices against people you've tagged. Tighten these if unknown speakers get labelled as someone they aren't; loosen them if tagged speakers keep coming back as 'Unknown'."
          sectionId="adv_speakers"
          keys={SECTION_KEYS.adv_speakers}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("adv_speakers", SECTION_KEYS.adv_speakers)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="voice_match_threshold_multi" label="Match strictness — group meetings (0 – 1)" type="number"
            hint="How similar a voice must be to match a known speaker when there are multiple people in the room. Lower = stricter, fewer false matches." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="voice_match_threshold_solo" label="Match strictness — solo (0 – 1)" type="number"
            hint="Same as above, but used when only one person has spoken so far. More forgiving because there's no one else to confuse with." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="voice_ratio_margin" label="Runner-up margin (0 – 1)" type="number"
            hint="How much better the top match must be than the second-best. Lower values are pickier — useful when two of your tagged voices sound similar." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="min_voice_samples" label="Samples before auto-matching" type="number"
            hint="How many tagged audio snippets a Voice needs before AuraScribe starts auto-assigning lines to it. Higher = more patient, fewer early mistakes." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="provisional_threshold" label="Split unknown speakers (0 – 1)" type="number"
            hint="How aggressively unknown voices are split into 'Speaker 1', 'Speaker 2'… during a live meeting. Lower = eager to split; higher = likely to merge different people." />
        </ConfigSection>

        {/* ── Advanced: Live partials ─────────────────────────────── */}
        <ConfigSection
          icon={<Zap size={14} className="text-brand-400/70" />}
          title="Live partial transcription"
          description="AuraScribe shows a partial sentence bubble while you're still talking, updated a few times a second. These knobs control how often that bubble refreshes and how much audio it re-reads."
          sectionId="adv_partials"
          keys={SECTION_KEYS.adv_partials}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("adv_partials", SECTION_KEYS.adv_partials)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="speculative_interval_sec" label="Refresh every (seconds)" type="number"
            hint="How often AuraScribe re-transcribes your current sentence. Lower = snappier updates, more GPU/CPU work." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="speculative_window_sec" label="Maximum bubble length (seconds)" type="number"
            hint="How many seconds of audio the partial bubble can show at most. The bubble accumulates what you say since the last full line landed, up to this cap. Bigger = more peace of mind; smaller = cheaper per refresh." />
        </ConfigSection>

        {/* ── Advanced: Obsidian write cadence ────────────────────── */}
        <ConfigSection
          icon={<FolderCheck size={14} className="text-emerald-400/70" />}
          title="Obsidian write cadence"
          description="How often the Markdown file in your Obsidian vault gets updated during a live meeting. Higher values mean fewer file writes — easier on Obsidian Sync and cloud backups."
          sectionId="adv_obsidian"
          keys={SECTION_KEYS.adv_obsidian}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("adv_obsidian", SECTION_KEYS.adv_obsidian)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="obsidian_write_interval_sec" label="Update at least every (seconds)" type="number"
            hint="Update the live meeting file in Obsidian after this many seconds…" />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="obsidian_write_chunks" label="…or every N new chunks" type="number"
            hint="…or after this many new transcript chunks have arrived — whichever comes first." />
        </ConfigSection>

        {/* ── Advanced: Daily Brief auto-refresh ──────────────────── */}
        <ConfigSection
          icon={<FileText size={14} className="text-amber-400/70" />}
          title="Daily Brief auto-refresh"
          description="The Daily Brief rolls up every meeting on a given date into one briefing — tl;dr, decisions, action items, and coaching notes."
          sectionId="adv_daily_brief"
          keys={SECTION_KEYS.adv_daily_brief}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("adv_daily_brief", SECTION_KEYS.adv_daily_brief)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="daily_brief_auto_refresh" label="Rebuild after every meeting"
            hint="When on, the Daily Brief regenerates in the background each time a meeting ends. Turn off to save AI calls and rebuild it manually from the Daily Briefs page." />
        </ConfigSection>

        {cfg?.config_file && (
          <p className="text-[10px] text-gray-600 font-mono break-all text-center">
            config file: {cfg.config_file}
          </p>
        )}

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Mic size={14} className="text-gray-400" />
            <h2 className="text-sm font-semibold text-gray-100">Audio (read-only)</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="Microphones detected">
              <span className="text-sm text-gray-200">{appStatus?.audio_devices.length ?? 0} device(s)</span>
              <ul className="text-[11px] text-gray-500 mt-1 space-y-0.5 max-h-24 overflow-y-auto scrollbar-thin">
                {(appStatus?.audio_devices ?? []).slice(0, 6).map((d) => (
                  <li key={d.index} className="truncate">
                    {d.name}
                    {d.host_api && <span className="text-gray-600"> ({d.host_api.replace("Windows ", "")})</span>}
                  </li>
                ))}
              </ul>
            </Field>
            <Field label="Sample rate">
              <span className="text-sm text-gray-200 font-mono">16 kHz (auto-resampled)</span>
            </Field>
          </div>
        </section>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">{label}</div>
      {children}
    </div>
  );
}

interface PathFieldProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  effective: string | undefined;
  overridden: boolean;
  disabled: boolean;
}

interface ConfigSectionProps {
  icon: React.ReactNode;
  title: string;
  description: string;
  sectionId: string;
  keys: ConfigKey[];
  isDirty: (k: ConfigKey) => boolean;
  savingSection: string | null;
  sectionErrors: Record<string, string | null>;
  sectionSavedAt: Record<string, number>;
  onSave: () => void;
  children: React.ReactNode;
}

function ConfigSection({
  icon, title, description, sectionId, keys, isDirty,
  savingSection, sectionErrors, sectionSavedAt, onSave, children,
}: ConfigSectionProps) {
  const dirty = keys.some((k) => isDirty(k));
  const saving = savingSection === sectionId;
  const canSave = dirty && !saving;
  const error = sectionErrors[sectionId];
  const savedAt = sectionSavedAt[sectionId];
  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <h2 className="text-sm font-semibold text-gray-100">{title}</h2>
      </div>
      <p className="text-[11px] text-gray-500 mb-4">{description}</p>

      <div className="space-y-4">
        {children}
      </div>

      <div className="flex items-center gap-3 mt-4">
        <button
          onClick={onSave}
          disabled={!canSave}
          className="text-xs font-medium px-3 py-1.5 rounded-lg
                     bg-brand-600 hover:bg-brand-500 text-white
                     disabled:bg-gray-800 disabled:text-gray-500 disabled:cursor-not-allowed
                     transition-colors"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {savedAt && !dirty && !error && (
          <span className="text-[11px] text-emerald-400">Saved.</span>
        )}
        {error && (
          <span className="text-[11px] text-red-400">{error}</span>
        )}
      </div>
    </section>
  );
}

interface ConfigFieldProps {
  cfg: AppConfig | null;
  drafts: Partial<Record<ConfigKey, string>>;
  onChange: (k: ConfigKey, v: string) => void;
  k: ConfigKey;
  label: string;
  type?: "text" | "password" | "number";
  hint?: string;
  // Optional dropdown options. When provided, renders as a <select>
  // instead of a free-text <input>. The leading empty option restores
  // the auto-detected default.
  options?: readonly string[];
}

function ConfigField({ cfg, drafts, onChange, k, label, type = "text", hint, options }: ConfigFieldProps) {
  const field = cfg?.settings[k];
  const value = drafts[k] ?? "";
  const overridden = field?.override !== null && field?.override !== undefined;
  const effective = field?.effective;
  const defaultVal = field?.default;
  const isBool = BOOL_KEYS.has(k);
  const placeholder = defaultVal !== null && defaultVal !== undefined
    ? (isBool && typeof defaultVal === "boolean" ? (defaultVal ? "on" : "off") : String(defaultVal))
    : "";
  // "currently using" only adds signal when it differs from the draft.
  // Secrets stay redacted so screenshots of the settings page don't leak.
  // Booleans render as on/off rather than JSON true/false.
  const effectiveDisplay = type === "password"
    ? (effective ? "••••••" : "(empty)")
    : effective === null || effective === undefined
    ? "(empty)"
    : typeof effective === "boolean"
    ? (effective ? "on" : "off")
    : String(effective);
  const effectiveDraftForm = effective === null || effective === undefined
    ? ""
    : typeof effective === "boolean"
    ? (effective ? "on" : "off")
    : String(effective);
  const showEffective = effective != null && effectiveDraftForm !== value;
  const resolvedOptions = options ?? (isBool ? (["on", "off"] as const) : ENUM_OPTIONS[k]);

  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider text-gray-500">{label}</span>
        {overridden ? (
          <span className="text-[9px] uppercase tracking-wider text-brand-400 px-1.5 py-0.5 rounded bg-brand-500/10 border border-brand-700/40">
            custom
          </span>
        ) : (
          <span className="text-[9px] uppercase tracking-wider text-gray-500 px-1.5 py-0.5 rounded bg-gray-800/60 border border-gray-700">
            default
          </span>
        )}
      </div>
      {resolvedOptions ? (
        <select
          value={value}
          onChange={(e) => onChange(k, e.target.value)}
          disabled={!cfg}
          className="w-full text-xs font-mono px-2.5 py-1.5 rounded-lg
                     bg-gray-950/70 border border-gray-800
                     text-gray-200
                     focus:outline-none focus:border-brand-600
                     disabled:opacity-50"
        >
          <option value="">auto ({placeholder || "detect"})</option>
          {resolvedOptions.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      ) : (
        <input
          type={type}
          spellCheck={false}
          value={value}
          onChange={(e) => onChange(k, e.target.value)}
          placeholder={placeholder}
          disabled={!cfg}
          className="w-full text-xs font-mono px-2.5 py-1.5 rounded-lg
                     bg-gray-950/70 border border-gray-800
                     text-gray-200 placeholder-gray-600
                     focus:outline-none focus:border-brand-600
                     disabled:opacity-50"
        />
      )}
      {hint && <p className="text-[10px] text-gray-500 mt-1">{hint}</p>}
      {showEffective && (
        <p className="text-[10px] text-gray-500 mt-1 font-mono break-all">
          currently using: {effectiveDisplay}
        </p>
      )}
    </div>
  );
}

// Stepped slider for token-count fields. Snaps to a caller-supplied list of
// stops and writes the stop's exact integer value into drafts, so the save
// pipeline sees a clean numeric string (no rounding ambiguity from a
// continuous slider).
interface TokenSliderProps {
  cfg: AppConfig | null;
  drafts: Partial<Record<ConfigKey, string>>;
  onChange: (k: ConfigKey, v: string) => void;
  k: ConfigKey;
  label: string;
  stops: number[];
  hint?: string;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 1_048_576)}M`;
  if (n >= 1_000) return `${Math.round(n / 1024)}k`;
  return String(n);
}

function nearestStopIndex(value: number, stops: number[]): number {
  let best = 0;
  let bestDiff = Math.abs(stops[0] - value);
  for (let i = 1; i < stops.length; i++) {
    const d = Math.abs(stops[i] - value);
    if (d < bestDiff) { best = i; bestDiff = d; }
  }
  return best;
}

function TokenSlider({ cfg, drafts, onChange, k, label, stops, hint }: TokenSliderProps) {
  const field = cfg?.settings[k];
  const draft = drafts[k] ?? "";
  const overridden = field?.override !== null && field?.override !== undefined;
  const effective = field?.effective;

  // Prefer the draft (latest user intent); fall back to effective or default
  // so the slider has a sensible starting position even before the user
  // touches it.
  const currentNum = (() => {
    const fromDraft = draft ? Number(draft) : NaN;
    if (!Number.isNaN(fromDraft) && fromDraft > 0) return fromDraft;
    if (typeof effective === "number") return effective;
    if (typeof field?.default === "number") return field.default;
    return stops[0];
  })();

  const idx = nearestStopIndex(currentNum, stops);
  const snapped = stops[idx];
  // Surface when the running process is on a non-stop value (e.g. legacy
  // 220000); the slider alone can't fully represent it, so show the raw
  // number too so nothing looks silently wrong.
  const offStop =
    typeof effective === "number" && !stops.includes(effective);

  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider text-gray-500">{label}</span>
        {overridden ? (
          <span className="text-[9px] uppercase tracking-wider text-brand-400 px-1.5 py-0.5 rounded bg-brand-500/10 border border-brand-700/40">
            custom
          </span>
        ) : (
          <span className="text-[9px] uppercase tracking-wider text-gray-500 px-1.5 py-0.5 rounded bg-gray-800/60 border border-gray-700">
            default
          </span>
        )}
        <span className="ml-auto text-xs font-mono text-gray-200 tabular-nums">
          {fmtTokens(snapped)} <span className="text-gray-500">tokens</span>
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={stops.length - 1}
        step={1}
        value={idx}
        disabled={!cfg}
        onChange={(e) => onChange(k, String(stops[Number(e.target.value)]))}
        className="w-full accent-brand-500 disabled:opacity-50 cursor-pointer"
      />
      <div className="flex justify-between mt-1 px-0.5 text-[10px] font-mono text-gray-500 select-none">
        {stops.map((s, i) => (
          <span
            key={s}
            className={i === idx ? "text-brand-400" : ""}
          >
            {fmtTokens(s)}
          </span>
        ))}
      </div>
      {hint && <p className="text-[10px] text-gray-500 mt-2">{hint}</p>}
      {offStop && (
        <p className="text-[10px] text-gray-500 mt-1 font-mono">
          currently using: {effective} (not on a stop — save to snap to {fmtTokens(snapped)})
        </p>
      )}
    </div>
  );
}

function PathField({ label, value, onChange, placeholder, effective, overridden, disabled }: PathFieldProps) {
  // The "currently using" line surfaces the path the running sidecar is
  // actually reading/writing right now — which can diverge from `value`
  // after a save (until the user restarts). We hide the line when it
  // matches the input to keep the UI clean in the common case.
  const showEffective = effective !== undefined && effective !== value && value !== "";
  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider text-gray-500">{label}</span>
        {overridden ? (
          <span className="text-[9px] uppercase tracking-wider text-brand-400 px-1.5 py-0.5 rounded bg-brand-500/10 border border-brand-700/40">
            custom
          </span>
        ) : (
          <span className="text-[9px] uppercase tracking-wider text-gray-500 px-1.5 py-0.5 rounded bg-gray-800/60 border border-gray-700">
            default
          </span>
        )}
      </div>
      <input
        type="text"
        spellCheck={false}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className="w-full text-xs font-mono px-2.5 py-1.5 rounded-lg
                   bg-gray-950/70 border border-gray-800
                   text-gray-200 placeholder-gray-600
                   focus:outline-none focus:border-brand-600
                   disabled:opacity-50"
      />
      {showEffective && (
        <p className="text-[10px] text-gray-500 mt-1 font-mono break-all">
          currently using: {effective}
        </p>
      )}
      {!showEffective && value === "" && placeholder && (
        <p className="text-[10px] text-gray-600 mt-1 font-mono break-all">
          default: {placeholder}
        </p>
      )}
    </div>
  );
}
