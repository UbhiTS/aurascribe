import { useEffect, useMemo, useState } from "react";
import {
  Cpu, Mic, FolderCheck, FolderX, Sparkles, FileText, ExternalLink,
  HardDrive, AlertTriangle, Languages, Users, Zap,
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
  speech: ["whisper_model", "whisper_language", "my_speaker_label"] as ConfigKey[],
  diarization: ["hf_token"] as ConfigKey[],
  obsidian: ["obsidian_vault"] as ConfigKey[],
  realtime: ["rt_highlights_debounce_sec", "rt_highlights_max_interval_sec", "rt_highlights_window_sec"] as ConfigKey[],
};

const NUMERIC_KEYS = new Set<ConfigKey>([
  "llm_context_tokens",
  "rt_highlights_debounce_sec",
  "rt_highlights_max_interval_sec",
  "rt_highlights_window_sec",
]);

// Stringifies a stored config value for display inside an <input>. Null /
// undefined map to empty string (the "no override" signal on save).
function valToDraft(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "";
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
            All settings persist in <code className="text-gray-300">config.json</code> inside
            the data directory — they move with your state folder across machines or reinstalls.
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
            One folder holds everything AuraScribe persists: transcript database,
            per-meeting <code>.opus</code> recordings, and the local Whisper model
            cache. Point a fresh install at the same folder on another machine — or
            after reinstalling — to pick up right where you left off.
          </p>

          <PathField
            label="State folder"
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
            User-editable templates that drive the live intelligence loop. Edits are picked
            up on the next call — no restart needed.
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
          title="LLM Provider"
          description="OpenAI-compatible endpoint used for summaries, live intelligence, and daily briefs. Works with LM Studio, OpenAI, OpenRouter, Gemini's OpenAI-compat endpoint, Anthropic via a compat proxy, etc."
          sectionId="llm"
          keys={SECTION_KEYS.llm}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("llm", SECTION_KEYS.llm)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_base_url" label="Base URL" hint="Root of the /v1/chat/completions endpoint. E.g. http://127.0.0.1:1234/v1 for LM Studio, https://api.openai.com/v1 for OpenAI." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_api_key" label="API key" type="password" hint="Provider's API key. LM Studio accepts any non-empty string." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_model" label="Model id" hint="The exact id the provider expects (e.g. gpt-4o, gemini-2.0-flash, claude-sonnet-4-6, or the local model id)." />
          <TokenSlider cfg={cfg} drafts={drafts} onChange={setDraft}
            k="llm_context_tokens" label="Context window"
            stops={[4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576]}
            hint="Total token budget of the chosen model. Used to size long-context calls like the Daily Brief." />
          <p className="text-[11px] text-gray-500 mt-1">
            Status: {lmModels.length > 0
              ? `${lmModels.length} model(s) reachable — first is ${lmModels[0]}`
              : "not reachable — check URL"}
          </p>
        </ConfigSection>

        {/* ── Speech & Transcription ───────────────────────────────── */}
        <ConfigSection
          icon={<Languages size={14} className="text-emerald-400" />}
          title="Speech & Transcription"
          description="faster-whisper model selection and language. Device / compute-type stay env-only."
          sectionId="speech"
          keys={SECTION_KEYS.speech}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("speech", SECTION_KEYS.speech)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="whisper_model" label="Whisper model" hint="e.g. large-v3-turbo, large-v3, medium, small.en" />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="whisper_language" label="Language" hint="ISO code. en, es, fr, de, …" />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="my_speaker_label" label="Your speaker label" hint="How you appear in transcripts." />
        </ConfigSection>

        {/* ── Diarization (HuggingFace token) ──────────────────────── */}
        <ConfigSection
          icon={<Users size={14} className="text-purple-400" />}
          title="Speaker Diarization"
          description="HuggingFace access token for downloading the pyannote speaker-diarization model."
          sectionId="diarization"
          keys={SECTION_KEYS.diarization}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("diarization", SECTION_KEYS.diarization)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="hf_token" label="HF access token" type="password"
            hint="Generate at huggingface.co/settings/tokens; needs read access to pyannote/* models." />
        </ConfigSection>

        {/* ── Obsidian (editable vault path) ───────────────────────── */}
        <ConfigSection
          icon={obsidianConfigured
            ? <FolderCheck size={14} className="text-emerald-400" />
            : <FolderX size={14} className="text-gray-500" />}
          title="Obsidian Integration"
          description="Optional: mirror meetings, people notes, and daily briefs into your vault as Markdown."
          sectionId="obsidian"
          keys={SECTION_KEYS.obsidian}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("obsidian", SECTION_KEYS.obsidian)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="obsidian_vault" label="Vault path"
            hint="Absolute path to your Obsidian vault. Leave empty to disable." />
          <p className="text-[11px] text-gray-500 mt-1">
            Meetings land under <code>&lt;vault&gt;\AuraScribe\Meetings\</code>; people notes under <code>People\</code>.
          </p>
        </ConfigSection>

        {/* ── Live Intelligence cadence ────────────────────────────── */}
        <ConfigSection
          icon={<Zap size={14} className="text-amber-400" />}
          title="Live Intelligence"
          description="How often the live-intel loop refires against the local LLM. Lower debounce = snappier panel, more load."
          sectionId="realtime"
          keys={SECTION_KEYS.realtime}
          isDirty={isDirty}
          savingSection={savingSection}
          sectionErrors={sectionErrors}
          sectionSavedAt={sectionSavedAt}
          onSave={() => saveSection("realtime", SECTION_KEYS.realtime)}
        >
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="rt_highlights_debounce_sec" label="Debounce (sec)" type="number"
            hint="Fire this many seconds after the last new utterance." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="rt_highlights_max_interval_sec" label="Max interval (sec)" type="number"
            hint="Hard cap between refreshes during nonstop speech." />
          <ConfigField cfg={cfg} drafts={drafts} onChange={setDraft}
            k="rt_highlights_window_sec" label="Context window (sec)" type="number"
            hint="Recent transcript window the LLM sees each call." />
        </ConfigSection>

        {cfg?.config_file && (
          <p className="text-[10px] text-gray-600 font-mono break-all text-center">
            config file: {cfg.config_file}
          </p>
        )}

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Mic size={14} className="text-gray-400" />
            <h2 className="text-sm font-semibold text-gray-100">Audio</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="Input devices detected">
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
              <span className="text-sm text-gray-200 font-mono">16 kHz (auto-resampled via soxr)</span>
            </Field>
          </div>
        </section>

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Cpu size={14} className="text-gray-400" />
            <h2 className="text-sm font-semibold text-gray-100">GPU</h2>
          </div>
          <Field label="Compute">
            <span className="text-sm text-gray-200 font-mono">CUDA 13 · RTX 5090 (Blackwell)</span>
            <span className="text-[11px] text-gray-500 block mt-1">
              Live VRAM/latency monitoring comes in Phase 2 (needs nvidia-smi poller).
            </span>
          </Field>
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
}

function ConfigField({ cfg, drafts, onChange, k, label, type = "text", hint }: ConfigFieldProps) {
  const field = cfg?.settings[k];
  const value = drafts[k] ?? "";
  const overridden = field?.override !== null && field?.override !== undefined;
  const effective = field?.effective;
  const defaultVal = field?.default;
  const placeholder = defaultVal !== null && defaultVal !== undefined ? String(defaultVal) : "";
  // "currently using" only adds signal when it differs from the draft.
  // Secrets stay redacted so screenshots of the settings page don't leak.
  const effectiveDisplay = type === "password"
    ? (effective ? "••••••" : "(empty)")
    : (effective !== null && effective !== undefined ? String(effective) : "(empty)");
  const showEffective = effective != null && String(effective) !== value;

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
