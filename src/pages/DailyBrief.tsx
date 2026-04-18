import { Calendar, Sparkles } from "lucide-react";

export function DailyBrief() {
  const today = new Date();
  const dateLabel = today.toLocaleDateString(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });

  return (
    <div className="h-full overflow-y-auto scrollbar-thin">
      <div className="max-w-5xl mx-auto px-6 py-6">
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <Calendar size={13} />
          {dateLabel}
        </div>
        <h1 className="text-lg font-semibold text-gray-100 mt-1">Daily Brief</h1>

        <div className="mt-6 rounded-xl border border-dashed border-brand-800/40 bg-gradient-to-br from-brand-950/20 to-purple-950/10 p-8 text-center">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-gradient-to-br from-brand-500 to-purple-600 shadow-lg shadow-brand-500/30 mb-3">
            <Sparkles size={22} className="text-white" />
          </div>
          <h2 className="text-sm font-semibold text-gray-100">Daily brief aggregation — coming in Phase 2</h2>
          <p className="text-xs text-gray-500 mt-1 max-w-md mx-auto leading-relaxed">
            Once implemented, this page will aggregate every meeting from the day into key decisions,
            action items grouped by priority, and tomorrow's focus. It will also write a Daily note
            into your Obsidian vault under <code className="text-gray-400">AuraScribe/Daily/</code>.
          </p>
        </div>
      </div>
    </div>
  );
}
