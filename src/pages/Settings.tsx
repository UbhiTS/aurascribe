import { useEffect, useState } from "react";
import { Cpu, Mic, FolderCheck, FolderX, Sparkles } from "lucide-react";
import { api } from "../lib/api";
import type { AppStatus } from "../lib/api";

interface Props {
  appStatus: AppStatus | null;
  obsidianConfigured: boolean;
}

export function Settings({ appStatus, obsidianConfigured }: Props) {
  const [lmModels, setLmModels] = useState<string[]>([]);

  useEffect(() => {
    api.llm.models().then((r) => setLmModels(r.models)).catch(() => {});
  }, []);

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
