import { useEffect, useState } from "react";
import { Cpu, Mic, FolderCheck, FolderX, Sparkles, FileText, ExternalLink } from "lucide-react";
import { api } from "../lib/api";
import type { AppStatus } from "../lib/api";

interface Props {
  appStatus: AppStatus | null;
  obsidianConfigured: boolean;
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

  useEffect(() => {
    api.llm.models().then((r) => setLmModels(r.models)).catch(() => {});
    api.intel.prompts()
      .then((r) => { setPrompts(r.prompts); setPromptsDir(r.dir); })
      .catch(() => {});
  }, []);

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
            Configuration currently loads from <code className="text-gray-300">.env</code> on startup. Editing
            via UI lands in Phase 2.
          </p>
        </div>

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Sparkles size={14} className="text-brand-400" />
            <h2 className="text-sm font-semibold text-gray-100">AI Engine</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="ASR model (faster-whisper)">
              <span className="text-sm text-gray-200 font-mono">large-v3-turbo</span>
              <span className="text-[11px] text-gray-500 block">CUDA · float16 · CTranslate2</span>
            </Field>
            <Field label="Local LLM (LM Studio)">
              <span className="text-sm text-gray-200 font-mono">
                {lmModels.length > 0 ? lmModels[0] : "(not reachable)"}
              </span>
              <span className="text-[11px] text-gray-500 block">
                {lmModels.length > 0 ? `${lmModels.length} model(s) loaded` : "Check LM_STUDIO_URL in .env"}
              </span>
            </Field>
          </div>
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

        <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
          <div className="flex items-center gap-2 mb-3">
            {obsidianConfigured
              ? <FolderCheck size={14} className="text-emerald-400" />
              : <FolderX size={14} className="text-gray-500" />}
            <h2 className="text-sm font-semibold text-gray-100">Obsidian Integration</h2>
          </div>
          <Field label="Vault path">
            <span className="text-sm text-gray-200 font-mono break-all">
              {obsidianConfigured ? "Configured (see OBSIDIAN_VAULT in .env)" : "Not set"}
            </span>
            <span className="text-[11px] text-gray-500 block mt-1">
              Meetings land under <code>&lt;vault&gt;\AuraScribe\Meetings\</code>; people notes under <code>People\</code>.
            </span>
          </Field>
        </section>

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
