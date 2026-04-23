import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2, Loader, Pause, Pencil, Play, Trash2, Users, X,
  ArrowRightLeft, AlertCircle, Upload,
} from "lucide-react";
import { api } from "../lib/api";
import type { Voice, VoiceDetail, VoiceSnippet } from "../lib/api";
import { Avatar } from "../components/Avatar";
import { avatarSrcFor, colorForSpeaker, PALETTE_KEYS, SPEAKER_PALETTE, type PaletteKey } from "../lib/speakerColors";

// Min embeddings before a Voice participates in auto-matching. Mirrors the
// backend gate in whisper.py — keep in sync if that changes.
const MIN_ACTIVE_SAMPLES = 3;

interface Props {
  voices: Voice[];
  onVoicesChanged: () => void;
}

export function Voices({ voices, onVoicesChanged }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<VoiceDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Auto-select the first voice so the right pane isn't empty on load.
  useEffect(() => {
    if (selectedId && voices.some((v) => v.id === selectedId)) return;
    if (voices.length > 0) setSelectedId(voices[0].id);
    else setSelectedId(null);
  }, [voices, selectedId]);

  const loadDetail = useCallback(async (id: string) => {
    setLoadingDetail(true);
    try {
      const d = await api.voices.get(id);
      setDetail(d);
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const refresh = useCallback(async () => {
    onVoicesChanged();
    if (selectedId) await loadDetail(selectedId);
  }, [selectedId, loadDetail, onVoicesChanged]);

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-5 py-4 border-b border-gray-800/60">
        <div className="flex items-center gap-2.5">
          <Users size={16} className="text-brand-400" />
          <h1 className="text-xl font-bold text-gray-100 tracking-tight">Voices</h1>
          <span className="text-xs text-gray-500">
            {voices.length} total · {voices.filter((v) => v.snippet_count >= MIN_ACTIVE_SAMPLES).length} active
          </span>
        </div>
        <p className="text-xs text-gray-500 mt-1">
          Tag speaker pills during a meeting to add their voice here. Once a Voice has {MIN_ACTIVE_SAMPLES} samples, it starts auto-matching future meetings.
        </p>
      </div>

      {/* Stacked on narrow screens (<md) so the 280px sidebar doesn't
          crush the detail pane; side-by-side from md+ where there's
          room for both. */}
      <div className="flex-1 min-h-0 grid grid-cols-1 md:grid-cols-[240px_minmax(0,1fr)] lg:grid-cols-[280px_minmax(0,1fr)] gap-4 p-4">
        <aside className="min-h-0 rounded-xl border border-gray-800 bg-gray-900/40 overflow-y-auto scrollbar-thin">
          {voices.length === 0 ? (
            <div className="p-6 text-xs text-gray-500 italic text-center">
              No voices yet. Open any meeting and tag a speaker pill to get started.
            </div>
          ) : (
            <ul className="p-2 space-y-1">
              {voices.map((v) => {
                const active = v.snippet_count >= MIN_ACTIVE_SAMPLES;
                const selected = selectedId === v.id;
                return (
                  <li key={v.id}>
                    <button
                      onClick={() => setSelectedId(v.id)}
                      className={`w-full flex items-center gap-3 px-2.5 py-2 rounded-lg transition-colors text-left ${
                        selected
                          ? "bg-brand-500/15 ring-1 ring-brand-500/40"
                          : "hover:bg-gray-900/70"
                      }`}
                    >
                      <Avatar name={v.name} size="sm" gradient={colorForSpeaker(v.name, voices).avatar} src={avatarSrcFor(v.name, voices)} />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-gray-200 truncate">{v.name}</div>
                        <div className="flex items-center gap-1.5 text-[10px]">
                          {active ? (
                            <span className="flex items-center gap-0.5 text-emerald-400">
                              <CheckCircle2 size={9} /> active
                            </span>
                          ) : (
                            <span className="flex items-center gap-0.5 text-amber-400">
                              <AlertCircle size={9} /> {v.snippet_count}/{MIN_ACTIVE_SAMPLES}
                            </span>
                          )}
                          <span className="text-gray-500">
                            · {v.snippet_count} {v.snippet_count === 1 ? "sample" : "samples"}
                          </span>
                        </div>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <section className="min-h-0 rounded-xl border border-gray-800 bg-gray-900/20 overflow-y-auto scrollbar-thin">
          {!selectedId ? (
            <div className="p-8 text-center text-sm text-gray-500">
              Select a voice to view its samples.
            </div>
          ) : loadingDetail || !detail ? (
            <div className="p-8 flex items-center justify-center text-sm text-gray-500">
              <Loader size={14} className="animate-spin mr-2" /> Loading...
            </div>
          ) : (
            <VoiceDetailPane
              detail={detail}
              allVoices={voices}
              onChanged={refresh}
              onDeleted={() => { setSelectedId(null); onVoicesChanged(); }}
            />
          )}
        </section>
      </div>
    </div>
  );
}

// ── Detail pane ─────────────────────────────────────────────────────────────

interface DetailProps {
  detail: VoiceDetail;
  allVoices: Voice[];
  onChanged: () => Promise<void> | void;
  onDeleted: () => void;
}

function VoiceDetailPane({ detail, allVoices, onChanged, onDeleted }: DetailProps) {
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(detail.name);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [uploadingAvatar, setUploadingAvatar] = useState(false);
  const [colorOpen, setColorOpen] = useState(false);
  const avatarInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => { setNameDraft(detail.name); }, [detail.name, detail.id]);

  const active = detail.snippet_count >= MIN_ACTIVE_SAMPLES;
  const totalSec = useMemo(() =>
    detail.snippets.reduce(
      (acc, s) => acc + Math.max(0, (s.end_time ?? 0) - (s.start_time ?? 0)),
      0,
    ),
    [detail.snippets],
  );

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stopAtRef = useRef<number | null>(null);
  const [playingSnippetId, setPlayingSnippetId] = useState<string | null>(null);
  // Keeps the id → error-message map so a failed .play() (typically a
  // missing .opus file on disk) can surface inline next to THAT snippet,
  // instead of silently going to console.warn. Cleared on next successful
  // play of the same snippet. Only one error is shown at a time because
  // the user can only play one snippet at a time.
  const [playError, setPlayError] = useState<{ id: string; message: string } | null>(null);

  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onTime = () => {
      const stopAt = stopAtRef.current;
      if (stopAt != null && el.currentTime >= stopAt) el.pause();
    };
    const onPause = () => { stopAtRef.current = null; setPlayingSnippetId(null); };
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("pause", onPause);
    el.addEventListener("ended", onPause);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("ended", onPause);
    };
  }, []);

  const playSnippet = useCallback(async (snippet: VoiceSnippet) => {
    if (!snippet.meeting_id || snippet.audio_start == null) return;
    const el = audioRef.current;
    if (!el) return;
    if (playingSnippetId === snippet.id && !el.paused) {
      el.pause();
      return;
    }
    const desired = api.meetings.audioUrl(snippet.meeting_id);
    if (!el.src.endsWith(desired)) el.src = desired;
    const duration = Math.max(0.5, (snippet.end_time ?? 0) - (snippet.start_time ?? 0));
    stopAtRef.current = snippet.audio_start + duration;
    setPlayingSnippetId(snippet.id);
    setPlayError(null);
    try {
      el.currentTime = snippet.audio_start;
      await el.play();
    } catch (e) {
      // Most common cause: the .opus recording has been deleted outside
      // the app (user cleaned up %APPDATA% manually, or a meeting that
      // predates audio capture). Previously this went to console.warn
      // only and the Play button just did nothing — no signal to the
      // user about what's wrong.
      console.warn("snippet playback failed", e);
      setPlayingSnippetId(null);
      stopAtRef.current = null;
      setPlayError({
        id: snippet.id,
        message: "Audio file not found — it may have been deleted.",
      });
    }
  }, [playingSnippetId]);

  const handleRename = async () => {
    const next = nameDraft.trim();
    if (!next || next === detail.name) { setEditingName(false); return; }
    try {
      await api.voices.update(detail.id, { name: next });
      setEditingName(false);
      await onChanged();
    } catch (e: any) {
      alert(`Rename failed: ${e.message ?? e}`);
    }
  };

  const handleDeleteSnippet = async (snippetId: string) => {
    try {
      await api.voices.deleteSnippet(detail.id, snippetId);
      await onChanged();
    } catch (e: any) {
      alert(`Delete failed: ${e.message ?? e}`);
    }
  };

  const handleDeleteVoice = async () => {
    try {
      await api.voices.delete(detail.id);
      onDeleted();
    } catch (e: any) {
      alert(`Delete failed: ${e.message ?? e}`);
    }
  };

  const handleMerge = async (intoId: string) => {
    const into = allVoices.find((v) => v.id === intoId);
    if (!into) return;
    try {
      await api.voices.merge(detail.id, intoId);
      setMergeOpen(false);
      onDeleted();
    } catch (e: any) {
      alert(`Merge failed: ${e.message ?? e}`);
    }
  };

  const handlePickColor = async (key: PaletteKey) => {
    if (detail.color === key) return;
    try {
      await api.voices.update(detail.id, { color: key });
      await onChanged();
    } catch (e: any) {
      alert(`Color update failed: ${e.message ?? e}`);
    }
  };

  const handleAvatarUpload = async (file: File) => {
    setUploadingAvatar(true);
    try {
      await api.voices.uploadAvatar(detail.id, file);
      await onChanged();
    } catch (e: any) {
      alert(`Avatar upload failed: ${e.message ?? e}`);
    } finally {
      setUploadingAvatar(false);
      // Reset the input so selecting the same file again re-fires onChange.
      if (avatarInputRef.current) avatarInputRef.current.value = "";
    }
  };

  const handleAvatarRemove = async () => {
    try {
      await api.voices.deleteAvatar(detail.id);
      await onChanged();
    } catch (e: any) {
      alert(`Avatar remove failed: ${e.message ?? e}`);
    }
  };

  return (
    <div className="p-5 space-y-4">
      <audio ref={audioRef} preload="none" style={{ display: "none" }} />

      {/* Header — wraps the Merge / Delete row under the name block
          on narrow widths so they don't get pushed off-screen. */}
      <div className="flex flex-wrap items-start gap-3">
        <div className="relative group/avatar flex-shrink-0">
          <button
            onClick={() => avatarInputRef.current?.click()}
            disabled={uploadingAvatar}
            title={detail.avatar_ext ? "Replace avatar image" : "Upload avatar image"}
            className="block rounded-full overflow-hidden ring-1 ring-gray-700 hover:ring-gray-500 transition disabled:opacity-60"
          >
            <Avatar
              name={detail.name}
              size="lg"
              gradient={colorForSpeaker(detail.name, allVoices).avatar}
              src={avatarSrcFor(detail.name, allVoices)}
            />
          </button>
          {/* Upload / remove overlay only appears on hover for a clean default look. */}
          <div className="absolute inset-0 rounded-full pointer-events-none opacity-0 group-hover/avatar:opacity-100 transition flex items-end justify-center">
            <div className="pointer-events-auto flex items-center gap-1 px-1.5 py-0.5 mb-0.5 rounded-full bg-gray-900/80 text-[9px] text-gray-200 border border-gray-700">
              {uploadingAvatar ? (
                <Loader size={9} className="animate-spin" />
              ) : (
                <Upload size={9} />
              )}
              {detail.avatar_ext && (
                <button
                  onClick={(e) => { e.stopPropagation(); handleAvatarRemove(); }}
                  title="Remove uploaded image"
                  className="text-rose-300 hover:text-rose-100"
                >
                  <X size={9} />
                </button>
              )}
            </div>
          </div>
          <input
            ref={avatarInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleAvatarUpload(f);
            }}
          />
        </div>
        <div className="flex-1 min-w-0">
          {editingName ? (
            <input
              autoFocus
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleRename();
                if (e.key === "Escape") { setEditingName(false); setNameDraft(detail.name); }
              }}
              onBlur={handleRename}
              className="w-full text-xl font-bold bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-gray-100 outline-none focus:border-brand-500"
            />
          ) : (
            <div className="flex items-center gap-2">
              <h2 className="text-xl font-bold text-gray-100 truncate">{detail.name}</h2>
              <button
                onClick={() => setEditingName(true)}
                title="Rename"
                className="text-gray-500 hover:text-gray-200"
              >
                <Pencil size={13} />
              </button>
            </div>
          )}
          <div className="mt-1 flex items-center gap-3 text-[11px]">
            {active ? (
              <span className="flex items-center gap-1 text-emerald-400">
                <CheckCircle2 size={10} /> Active in auto-match
              </span>
            ) : (
              <span className="flex items-center gap-1 text-amber-400">
                <AlertCircle size={10} />
                Needs {MIN_ACTIVE_SAMPLES - detail.snippet_count} more {detail.snippet_count === MIN_ACTIVE_SAMPLES - 1 ? "sample" : "samples"} to activate
              </span>
            )}
            <span className="text-gray-500">·</span>
            <span className="text-gray-400">{detail.snippet_count} {detail.snippet_count === 1 ? "sample" : "samples"}</span>
            <span className="text-gray-500">·</span>
            <span className="text-gray-400">{fmtSeconds(totalSec)} total</span>
          </div>
          {/* Color picker — single swatch showing the current slot; click
              opens a 16-color popover. Persists to voices.color so every
              surface in the app picks up the new color after refresh. */}
          <div className="mt-2 relative inline-block">
            <button
              onClick={() => setColorOpen((v) => !v)}
              title="Change color"
              className={`w-5 h-5 rounded-full bg-gradient-to-br ${colorForSpeaker(detail.name, allVoices).avatar} ring-1 ring-gray-600 hover:ring-gray-400 transition`}
            />
            {colorOpen && (
              <ColorPopover
                current={(detail.color as PaletteKey | null) ?? null}
                onClose={() => setColorOpen(false)}
                onPick={(key) => { setColorOpen(false); handlePickColor(key); }}
              />
            )}
          </div>
        </div>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          <div className="relative">
            <button
              onClick={() => setMergeOpen((v) => !v)}
              disabled={allVoices.length < 2}
              title="Merge into another voice"
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border border-gray-700 text-gray-300 bg-gray-800/60 hover:border-gray-500 hover:bg-gray-800 disabled:opacity-40 disabled:hover:border-gray-700"
            >
              <ArrowRightLeft size={12} />
              Merge
            </button>
            {mergeOpen && (
              <MergePopover
                currentId={detail.id}
                voices={allVoices}
                onClose={() => setMergeOpen(false)}
                onPick={handleMerge}
              />
            )}
          </div>
          <button
            onClick={handleDeleteVoice}
            title="Delete this voice"
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border border-red-900/60 text-red-400 bg-red-950/30 hover:bg-red-950/60 hover:border-red-800"
          >
            <Trash2 size={12} />
            Delete
          </button>
        </div>
      </div>

      {/* Descriptive metadata. All optional — email drives People-note
          filename disambiguation when two voices share a display name;
          all three surface in the People note's frontmatter. */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
        <MetaField
          label="Email"
          placeholder="jane@acme.com"
          value={detail.email}
          onSave={async (next) => {
            await api.voices.update(detail.id, { email: next });
            await onChanged();
          }}
        />
        <MetaField
          label="Organization"
          placeholder="Acme Corp"
          value={detail.org}
          onSave={async (next) => {
            await api.voices.update(detail.id, { org: next });
            await onChanged();
          }}
        />
        <MetaField
          label="Role"
          placeholder="Engineering Lead"
          value={detail.role}
          onSave={async (next) => {
            await api.voices.update(detail.id, { role: next });
            await onChanged();
          }}
        />
      </div>

      {/* Snippets grid */}
      <div>
        <div className="text-[10px] uppercase tracking-wider text-gray-400 font-semibold mb-2">
          Samples
        </div>
        {detail.snippets.length === 0 ? (
          <div className="text-xs text-gray-500 italic p-6 text-center border border-dashed border-gray-800 rounded-lg">
            No samples yet. Tag a transcript pill to add one.
          </div>
        ) : (
          <ul className="space-y-2">
            {detail.snippets.map((s) => (
              <li key={s.id}>
                <SnippetRow
                  snippet={s}
                  playing={playingSnippetId === s.id}
                  onPlay={() => playSnippet(s)}
                  onDelete={() => handleDeleteSnippet(s.id)}
                />
                {playError?.id === s.id && (
                  <p className="mt-1 ml-2 text-[11px] text-amber-300">
                    {playError.message}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

interface SnippetRowProps {
  snippet: VoiceSnippet;
  playing: boolean;
  onPlay: () => void;
  onDelete: () => void;
}

function SnippetRow({ snippet, playing, onPlay, onDelete }: SnippetRowProps) {
  const duration = Math.max(0, (snippet.end_time ?? 0) - (snippet.start_time ?? 0));
  const canPlay = snippet.meeting_id != null && snippet.audio_start != null;
  return (
    <li className="flex items-start gap-3 px-3 py-2.5 rounded-lg border border-gray-800 bg-gray-950/40 hover:bg-gray-950/70">
      <button
        onClick={onPlay}
        disabled={!canPlay}
        title={canPlay ? (playing ? "Stop" : "Play sample") : "No audio available for this sample"}
        className={`flex-shrink-0 flex items-center justify-center w-8 h-8 rounded-full transition-colors ${
          playing
            ? "bg-brand-500 text-white"
            : "bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-40 disabled:hover:bg-gray-800"
        }`}
      >
        {playing ? <Pause size={13} /> : <Play size={13} />}
      </button>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-gray-200 line-clamp-2 leading-snug">
          {snippet.utterance_text ?? <span className="italic text-gray-500">(utterance removed)</span>}
        </div>
        <div className="mt-1 flex items-center gap-1.5 text-[10px] text-gray-500">
          {snippet.meeting_title && (
            <span className="truncate max-w-[200px]">{snippet.meeting_title}</span>
          )}
          {snippet.meeting_started_at && (
            <>
              <span>·</span>
              <span>{new Date(snippet.meeting_started_at).toLocaleDateString()}</span>
            </>
          )}
          {duration > 0 && (
            <>
              <span>·</span>
              <span className="font-mono">{duration.toFixed(1)}s</span>
            </>
          )}
          <span>·</span>
          <span className="uppercase tracking-wider">{snippet.source}</span>
        </div>
      </div>
      <button
        onClick={onDelete}
        title="Remove this sample"
        className="flex-shrink-0 text-gray-600 hover:text-red-400 transition-colors"
      >
        <X size={14} />
      </button>
    </li>
  );
}

interface MergePopoverProps {
  currentId: string;
  voices: Voice[];
  onClose: () => void;
  onPick: (id: string) => void;
}

function MergePopover({ currentId, voices, onClose, onPick }: MergePopoverProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (!t.closest("[data-merge-popover]")) onClose();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [onClose]);

  const targets = voices.filter((v) => v.id !== currentId);
  return (
    <div
      data-merge-popover
      className="absolute z-30 top-full right-0 mt-1.5 bg-gray-900 border border-gray-700 rounded-lg shadow-xl min-w-[220px] p-1.5 text-gray-100"
    >
      <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500">
        Merge into...
      </div>
      {targets.length === 0 ? (
        <div className="px-2 py-2 text-xs text-gray-500 italic">No other voices</div>
      ) : targets.map((v) => (
        <button
          key={v.id}
          onClick={() => onPick(v.id)}
          className="w-full text-left px-2 py-1.5 text-sm rounded hover:bg-gray-800 flex items-center gap-2"
        >
          <Avatar name={v.name} size="xs" gradient={colorForSpeaker(v.name, voices).avatar} src={avatarSrcFor(v.name, voices)} />
          {v.name}
          <span className="ml-auto text-[10px] text-gray-500">{v.snippet_count}</span>
        </button>
      ))}
    </div>
  );
}

interface ColorPopoverProps {
  current: PaletteKey | null;
  onClose: () => void;
  onPick: (key: PaletteKey) => void;
}

function ColorPopover({ current, onClose, onPick }: ColorPopoverProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (!t.closest("[data-color-popover]")) onClose();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [onClose]);

  return (
    <div
      data-color-popover
      className="absolute z-30 top-full left-0 mt-2 p-3 rounded-lg border border-gray-700 bg-gray-900/95 backdrop-blur shadow-xl"
    >
      <div className="flex items-center gap-3">
        {PALETTE_KEYS.map((key) => {
          const c = SPEAKER_PALETTE[key];
          const selected = current === key;
          return (
            <button
              key={key}
              onClick={() => onPick(key)}
              title={key}
              className={`w-5 h-5 rounded-full bg-gradient-to-br ${c.avatar} transition
                ${selected ? "ring-2 ring-offset-2 ring-offset-gray-900 ring-gray-200 scale-110" : "opacity-80 hover:opacity-100 hover:scale-110"}`}
            />
          );
        })}
      </div>
    </div>
  );
}

function fmtSeconds(s: number): string {
  if (s < 60) return `${s.toFixed(0)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.floor(s % 60);
  return `${m}m ${rem.toString().padStart(2, "0")}s`;
}

// ── MetaField ──────────────────────────────────────────────────────────────
//
// Inline-editable text field for Voice descriptive metadata
// (email / organization / role). Click to edit, Enter / blur to save,
// Escape to cancel. Empty input clears the field.

interface MetaFieldProps {
  label: string;
  placeholder: string;
  value: string | null;
  onSave: (next: string) => Promise<void>;
}

function MetaField({ label, placeholder, value, onSave }: MetaFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);

  useEffect(() => { setDraft(value ?? ""); }, [value]);

  const commit = async () => {
    const next = draft.trim();
    if (next === (value ?? "")) { setEditing(false); return; }
    setSaving(true);
    try {
      await onSave(next);
      setEditing(false);
    } catch (e: any) {
      alert(`Update failed: ${e.message ?? e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold mb-1">
        {label}
      </div>
      {editing ? (
        <input
          autoFocus
          disabled={saving}
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") { setEditing(false); setDraft(value ?? ""); }
          }}
          onBlur={commit}
          className="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-gray-100 outline-none focus:border-brand-500 disabled:opacity-60"
        />
      ) : (
        <button
          onClick={() => setEditing(true)}
          className="w-full text-left px-2 py-1 rounded border border-transparent hover:border-gray-700 hover:bg-gray-900/40 transition-colors"
        >
          {value ? (
            <span className="text-gray-200">{value}</span>
          ) : (
            <span className="text-gray-500 italic">{placeholder}</span>
          )}
        </button>
      )}
    </div>
  );
}
