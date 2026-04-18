import { useEffect, useState } from "react";
import { api } from "./api";

const POLL_INTERVAL_MS = 15_000;

export interface LLMHealth {
  // true when /api/models returned a non-empty list.
  online: boolean;
  // Configured llm_model from config.json (the one live intel/summary use).
  configuredModel: string | null;
  // Models the provider reports as available right now. Empty when offline.
  loadedModels: string[];
}

export function useLLMHealth(): LLMHealth {
  const [configuredModel, setConfiguredModel] = useState<string | null>(null);
  const [loadedModels, setLoadedModels] = useState<string[]>([]);
  const [online, setOnline] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.settings.getConfig()
      .then((cfg) => {
        if (cancelled) return;
        const v = cfg.settings?.llm_model?.effective;
        if (typeof v === "string" && v) setConfiguredModel(v);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const poll = async () => {
      try {
        const { models } = await api.llm.models();
        if (cancelled) return;
        setLoadedModels(models ?? []);
        setOnline((models ?? []).length > 0);
      } catch {
        if (cancelled) return;
        setLoadedModels([]);
        setOnline(false);
      }
      if (!cancelled) timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    };
    poll();

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  return { online, configuredModel, loadedModels };
}
