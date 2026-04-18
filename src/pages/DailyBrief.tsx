import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Calendar, RefreshCw, Loader, Sparkles, Lightbulb, ChevronLeft, ChevronRight,
  CheckSquare, Square, AlertTriangle, Flame, Users, Tag, Compass, MessageSquare,
} from "lucide-react";
import { api } from "../lib/api";
import type { DailyBriefData, DailyBriefResponse } from "../lib/api";

interface Props {
  signal: { date: string; status: "refreshing" | "ready" | "stale"; tick: number } | null;
}

export function DailyBrief({ signal }: Props) {
  const [selectedDate, setSelectedDate] = useState<string>(todayIso());
  const [data, setData] = useState<DailyBriefResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (iso: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.dailyBrief.get(iso);
      setData(res);
    } catch (e: any) {
      setError(e?.message ?? "Failed to load brief");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(selectedDate); }, [selectedDate, load]);

  // React to WS-pushed brief updates that match the date we're viewing.
  // Refreshing mid-regen → show the spinner; ready → refetch.
  const lastSignalTick = useRef(0);
  useEffect(() => {
    if (!signal || signal.tick === lastSignalTick.current) return;
    lastSignalTick.current = signal.tick;
    if (signal.date !== selectedDate) return;
    if (signal.status === "refreshing") {
      setRefreshing(true);
    } else {
      setRefreshing(false);
      load(selectedDate);
    }
  }, [signal, selectedDate, load]);

  const handleRefresh = async () => {
    if (refreshing || loading) return;
    setRefreshing(true);
    setError(null);
    try {
      const res = await api.dailyBrief.refresh(selectedDate);
      setData(res);
    } catch (e: any) {
      setError(e?.message ?? "Refresh failed");
    } finally {
      setRefreshing(false);
    }
  };

  const shiftDay = (days: number) => {
    setSelectedDate(shiftIso(selectedDate, days));
  };

  const brief = data?.brief ?? null;
  const hasMeetings = (data?.meeting_count ?? 0) > 0;
  const hasBrief = !!brief;

  return (
    <div className="h-full overflow-y-auto scrollbar-thin">
      <div className="max-w-5xl mx-auto px-6 py-6 space-y-5">
        {/* Header: date selector + meta + refresh */}
        <Header
          selectedDate={selectedDate}
          onDateChange={setSelectedDate}
          onShift={shiftDay}
          onRefresh={handleRefresh}
          refreshing={refreshing}
          loading={loading}
          meetingCount={data?.meeting_count ?? 0}
          generatedAt={data?.generated_at ?? null}
          isStale={data?.is_stale ?? true}
          hasBrief={hasBrief}
        />

        {error && (
          <div className="rounded-xl border border-red-900/40 bg-red-950/20 p-3 text-xs text-red-300 flex items-start gap-2">
            <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {loading && !data && <SkeletonBrief />}

        {!loading && data && !hasMeetings && <EmptyDay date={selectedDate} />}

        {!loading && data && hasMeetings && !hasBrief && (
          <NoBriefYet onRefresh={handleRefresh} refreshing={refreshing} />
        )}

        {brief && hasMeetings && <BriefView brief={brief} />}
      </div>
    </div>
  );
}

// ── Header ──────────────────────────────────────────────────────────────────

function Header({
  selectedDate, onDateChange, onShift, onRefresh, refreshing, loading,
  meetingCount, generatedAt, isStale, hasBrief,
}: {
  selectedDate: string;
  onDateChange: (iso: string) => void;
  onShift: (days: number) => void;
  onRefresh: () => void;
  refreshing: boolean;
  loading: boolean;
  meetingCount: number;
  generatedAt: string | null;
  isStale: boolean;
  hasBrief: boolean;
}) {
  const dateLabel = useMemo(() => {
    const [y, m, d] = selectedDate.split("-").map(Number);
    const dt = new Date(y, (m ?? 1) - 1, d ?? 1);
    return dt.toLocaleDateString(undefined, {
      weekday: "long", year: "numeric", month: "long", day: "numeric",
    });
  }, [selectedDate]);

  const isToday = selectedDate === todayIso();
  const freshnessLabel = generatedAt ? relativeTime(generatedAt) : null;

  return (
    <div className="flex items-start justify-between gap-4 flex-wrap">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <Calendar size={13} />
          <span>{dateLabel}</span>
          {isToday && (
            <span className="text-[10px] uppercase tracking-wider text-brand-400 bg-brand-500/10 border border-brand-500/30 rounded px-1.5 py-0.5 font-semibold">
              Today
            </span>
          )}
        </div>
        <h1 className="text-2xl font-bold text-gray-100 tracking-tight mt-1">
          Daily Brief
        </h1>
        <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-500">
          <span>
            {meetingCount === 0 ? "No meetings" : meetingCount === 1 ? "1 meeting" : `${meetingCount} meetings`}
          </span>
          {hasBrief && freshnessLabel && (
            <>
              <span className="text-gray-700">·</span>
              <span className={isStale ? "text-amber-400" : ""}>
                {isStale ? "Stale · regenerating" : `Updated ${freshnessLabel}`}
              </span>
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => onShift(-1)}
          disabled={loading}
          title="Previous day"
          className="p-2 rounded-lg border border-gray-800 bg-gray-900/60 text-gray-300 hover:text-gray-100 hover:border-gray-700 transition-colors disabled:opacity-40"
        >
          <ChevronLeft size={15} />
        </button>
        <input
          type="date"
          value={selectedDate}
          onChange={(e) => onDateChange(e.target.value)}
          max={todayIso()}
          className="px-3 py-2 text-sm rounded-lg border border-gray-800 bg-gray-900/60 text-gray-200 outline-none focus:border-brand-500"
        />
        <button
          onClick={() => onShift(1)}
          disabled={loading || selectedDate >= todayIso()}
          title="Next day"
          className="p-2 rounded-lg border border-gray-800 bg-gray-900/60 text-gray-300 hover:text-gray-100 hover:border-gray-700 transition-colors disabled:opacity-40"
        >
          <ChevronRight size={15} />
        </button>
        <button
          onClick={onRefresh}
          disabled={refreshing || loading}
          title="Rebuild the brief from scratch"
          className="ml-1 flex items-center gap-1.5 px-3 py-2 text-xs font-medium rounded-lg border border-brand-700 text-brand-300 bg-brand-600/10 hover:bg-brand-600/20 transition-colors disabled:opacity-50"
        >
          {refreshing ? <Loader size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          {refreshing ? "Refreshing" : "Refresh"}
        </button>
      </div>
    </div>
  );
}

// ── Empty / loading states ──────────────────────────────────────────────────

function SkeletonBrief() {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="h-16 rounded-xl bg-gray-900/60 border border-gray-800" />
      <div className="grid grid-cols-2 gap-4">
        <div className="h-40 rounded-xl bg-gray-900/60 border border-gray-800" />
        <div className="h-40 rounded-xl bg-gray-900/60 border border-gray-800" />
      </div>
      <div className="h-32 rounded-xl bg-gray-900/60 border border-gray-800" />
    </div>
  );
}

function EmptyDay({ date }: { date: string }) {
  const isToday = date === todayIso();
  return (
    <div className="rounded-xl border border-dashed border-gray-800 bg-gray-900/40 p-10 text-center">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-gray-800/80 mb-3">
        <Calendar size={20} className="text-gray-500" />
      </div>
      <h2 className="text-sm font-semibold text-gray-300">
        {isToday ? "No meetings yet today" : "No meetings on this day"}
      </h2>
      <p className="text-xs text-gray-500 mt-1 max-w-md mx-auto leading-relaxed">
        {isToday
          ? "Start a recording on the Live Feed — this page fills in automatically as the day unfolds."
          : "Pick a different date to see a brief."}
      </p>
    </div>
  );
}

function NoBriefYet({ onRefresh, refreshing }: { onRefresh: () => void; refreshing: boolean }) {
  return (
    <div className="rounded-xl border border-dashed border-brand-800/40 bg-gradient-to-br from-brand-950/20 to-purple-950/10 p-8 text-center">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-gradient-to-br from-brand-500 to-purple-600 shadow-lg shadow-brand-500/30 mb-3">
        <Sparkles size={22} className="text-white" />
      </div>
      <h2 className="text-sm font-semibold text-gray-100">Brief hasn't been generated yet</h2>
      <p className="text-xs text-gray-500 mt-1 max-w-md mx-auto leading-relaxed">
        Meetings are on record for this day. Generate the brief to see the TL;DR,
        action items, open threads, and tomorrow's focus.
      </p>
      <button
        onClick={onRefresh}
        disabled={refreshing}
        className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 text-xs font-medium rounded-lg border border-brand-700 text-brand-300 bg-brand-600/10 hover:bg-brand-600/20 transition-colors disabled:opacity-50"
      >
        {refreshing ? <Loader size={12} className="animate-spin" /> : <Sparkles size={12} />}
        Generate brief
      </button>
    </div>
  );
}

// ── Brief view ──────────────────────────────────────────────────────────────

function BriefView({ brief }: { brief: DailyBriefData }) {
  return (
    <div className="space-y-4">
      {brief.tldr && <TldrCard text={brief.tldr} />}

      {brief.tomorrow_focus.length > 0 && (
        <FocusCard items={brief.tomorrow_focus} />
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {brief.action_items_self.length > 0 && (
          <Card title="Action Items — You" icon={<CheckSquare size={11} className="text-brand-400" />}>
            <ul className="space-y-2">
              {brief.action_items_self.map((a, i) => (
                <SelfActionRow key={i} action={a} />
              ))}
            </ul>
          </Card>
        )}

        {brief.action_items_others.length > 0 && (
          <Card title="Owed to You" icon={<Users size={11} className="text-brand-400" />}>
            <ul className="space-y-2">
              {brief.action_items_others.map((a, i) => (
                <li key={i} className="text-xs">
                  <div className="flex items-start gap-2">
                    <span className="font-semibold text-brand-300 flex-shrink-0">{a.speaker}:</span>
                    <span className="text-gray-200">{a.item}</span>
                  </div>
                  <MetaLine due={a.due} source={a.source} />
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>

      {brief.highlights.length > 0 && (
        <Card title="Highlights" icon={<Sparkles size={11} className="text-brand-400" />} gradient>
          <ul className="space-y-1.5">
            {brief.highlights.map((h, i) => (
              <li key={i} className="text-xs text-gray-200 leading-relaxed flex gap-2">
                <span className="text-brand-400 select-none">•</span>
                <span>{h}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {brief.decisions.length > 0 && (
        <Card title="Decisions" icon={<Flame size={11} className="text-brand-400" />}>
          <ul className="space-y-2.5">
            {brief.decisions.map((d, i) => (
              <li key={i} className="text-xs">
                <div className="text-gray-200 font-medium leading-snug">{d.decision}</div>
                {d.context && (
                  <div className="text-gray-500 leading-relaxed mt-0.5">{d.context}</div>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {brief.open_threads.length > 0 && (
        <Card title="Open Threads" icon={<AlertTriangle size={11} className="text-amber-400" />} accent="amber">
          <ul className="space-y-1.5">
            {brief.open_threads.map((t, i) => (
              <li key={i} className="text-xs text-gray-200 leading-relaxed flex gap-2">
                <span className="text-amber-400 select-none">→</span>
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {brief.people.length > 0 && (
        <Card title="People" icon={<Users size={11} className="text-brand-400" />}>
          <ul className="space-y-2">
            {brief.people.map((p, i) => (
              <li key={i} className="text-xs">
                <div className="font-semibold text-brand-300">{p.name}</div>
                {p.takeaway && (
                  <div className="text-gray-300 leading-relaxed mt-0.5">{p.takeaway}</div>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {brief.coaching.length > 0 && (
        <Card title="Coaching" icon={<Lightbulb size={11} className="text-amber-400" />} accent="amber">
          <ul className="space-y-1.5">
            {brief.coaching.map((c, i) => (
              <li key={i} className="text-xs text-gray-200 leading-relaxed flex gap-2">
                <span className="text-amber-400 select-none">→</span>
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {brief.themes.length > 0 && (
        <Card title="Themes" icon={<Tag size={11} className="text-brand-400" />}>
          <div className="flex flex-wrap gap-1.5">
            {brief.themes.map((t, i) => (
              <span
                key={i}
                className="text-[11px] px-2 py-0.5 rounded-full bg-gray-800/80 border border-gray-700 text-gray-300"
              >
                {t}
              </span>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

// ── Card primitives ─────────────────────────────────────────────────────────

function TldrCard({ text }: { text: string }) {
  return (
    <div className="rounded-xl border border-brand-800/40 bg-gradient-to-br from-brand-950/40 via-purple-950/20 to-gray-900/40 p-5 shadow-lg shadow-brand-500/5">
      <div className="flex items-center gap-2 mb-2">
        <MessageSquare size={12} className="text-brand-400" />
        <div className="text-[10px] uppercase tracking-wider text-brand-300 font-semibold">TL;DR</div>
      </div>
      <p className="text-sm text-gray-100 leading-relaxed">{text}</p>
    </div>
  );
}

function FocusCard({ items }: { items: string[] }) {
  return (
    <div className="rounded-xl border border-amber-800/40 bg-gradient-to-br from-amber-950/30 to-gray-900/40 p-4 shadow-md shadow-amber-500/5">
      <div className="flex items-center gap-1.5 mb-2.5">
        <Compass size={12} className="text-amber-400" />
        <div className="text-[10px] uppercase tracking-wider text-amber-300 font-semibold">
          Tomorrow's Focus
        </div>
      </div>
      <ul className="space-y-1.5">
        {items.map((t, i) => (
          <li key={i} className="text-xs text-gray-100 leading-relaxed flex gap-2">
            <span className="text-amber-400 font-bold w-4 flex-shrink-0">{i + 1}.</span>
            <span>{t}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Card({
  title, icon, children, gradient, accent,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
  gradient?: boolean;
  accent?: "amber";
}) {
  const base = accent === "amber"
    ? "border-amber-800/40 bg-gradient-to-br from-amber-950/20 to-gray-900/40"
    : gradient
    ? "border-brand-800/40 bg-gradient-to-br from-brand-950/30 to-purple-950/20 shadow-md shadow-brand-500/5"
    : "border-gray-800 bg-gray-900/60";
  return (
    <div className={`rounded-xl border p-3.5 ${base}`}>
      <div className="flex items-center gap-1.5 mb-2.5">
        {icon}
        <div className={`text-[10px] uppercase tracking-wider font-semibold ${
          accent === "amber" ? "text-amber-300" : "text-gray-400"
        }`}>
          {title}
        </div>
      </div>
      {children}
    </div>
  );
}

function SelfActionRow({ action }: { action: { item: string; due: string; source: string; priority: string } }) {
  const [done, setDone] = useState(false);
  const priorityStyle =
    action.priority === "high"
      ? "text-red-300 bg-red-500/10 border-red-500/30"
      : action.priority === "low"
      ? "text-gray-400 bg-gray-500/10 border-gray-500/20"
      : "text-brand-300 bg-brand-500/10 border-brand-500/30";
  return (
    <li className="text-xs">
      <div className="flex items-start gap-2">
        <button
          onClick={() => setDone((v) => !v)}
          className="mt-0.5 text-gray-500 hover:text-brand-400 flex-shrink-0"
        >
          {done ? <CheckSquare size={13} className="text-brand-400" /> : <Square size={13} />}
        </button>
        <div className="min-w-0 flex-1">
          <div className={`text-gray-200 ${done ? "line-through text-gray-600" : ""}`}>
            {action.item}
          </div>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border font-semibold ${priorityStyle}`}>
              {action.priority}
            </span>
            {action.due && (
              <span className="text-[10px] text-amber-300">Due {action.due}</span>
            )}
            {action.source && (
              <span className="text-[10px] text-gray-500 truncate">{action.source}</span>
            )}
          </div>
        </div>
      </div>
    </li>
  );
}

function MetaLine({ due, source }: { due?: string; source?: string }) {
  if (!due && !source) return null;
  return (
    <div className="flex items-center gap-2 mt-0.5 pl-4 flex-wrap">
      {due && <span className="text-[10px] text-amber-300">Due {due}</span>}
      {source && <span className="text-[10px] text-gray-500 truncate">{source}</span>}
    </div>
  );
}

// ── Date helpers ────────────────────────────────────────────────────────────

function todayIso(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function shiftIso(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(y, (m ?? 1) - 1, d ?? 1);
  dt.setDate(dt.getDate() + days);
  const yy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diffSec = Math.floor((Date.now() - t) / 1000);
  if (diffSec < 30) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const m = Math.floor(diffSec / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}
