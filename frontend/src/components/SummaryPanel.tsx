import { Loader } from "lucide-react";
import type { Meeting } from "../lib/api";

interface Props {
  meeting: Meeting | null;
}

export function SummaryPanel({ meeting }: Props) {
  if (!meeting) {
    return (
      <div className="flex items-center justify-center h-full text-gray-600 text-sm">
        Select a meeting to view summary
      </div>
    );
  }

  const actionItems: string[] = meeting.action_items
    ? JSON.parse(meeting.action_items)
    : [];

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto scrollbar-thin p-4">
        {meeting.status !== "done" ? (
          <div className="flex items-center gap-2 text-amber-400 text-sm">
            <Loader size={14} className="animate-spin" />
            {meeting.status === "processing" ? "Generating summary..." : "Recording in progress..."}
          </div>
        ) : (
          <div className="space-y-4">
            {meeting.summary && (
              <div
                className="prose prose-invert prose-sm max-w-none text-gray-300"
                dangerouslySetInnerHTML={{ __html: mdToHtml(meeting.summary) }}
              />
            )}

            {actionItems.length > 0 && (
              <div className="mt-4 p-3 bg-amber-950/30 border border-amber-800/30 rounded-lg">
                <h4 className="text-xs font-semibold text-amber-400 uppercase tracking-wider mb-2">
                  Action Items
                </h4>
                <ul className="space-y-1">
                  {actionItems.map((item, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm text-gray-300">
                      <span className="text-amber-500 mt-0.5">→</span>
                      {item}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {!meeting.summary && actionItems.length === 0 && (
              <p className="text-sm text-gray-600">No summary available. Enable AI summary when stopping the recording.</p>
            )}

            {meeting.vault_path && (
              <div className="text-xs text-gray-600 mt-2">
                Saved to Obsidian: <span className="text-gray-500 font-mono">{meeting.vault_path}</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function mdToHtml(md: string): string {
  return md
    .replace(/^## (.+)$/gm, '<h3 class="text-sm font-semibold text-gray-200 mt-4 mb-1">$1</h3>')
    .replace(/^### (.+)$/gm, '<h4 class="text-xs font-semibold text-gray-300 mt-3 mb-1">$1</h4>')
    .replace(/\*\*(.+?)\*\*/g, '<strong class="text-gray-200">$1</strong>')
    .replace(/^- \[ \] (.+)$/gm, '<li class="flex gap-2"><span class="text-amber-500">☐</span><span>$1</span></li>')
    .replace(/^- (.+)$/gm, '<li class="text-gray-300 ml-3 list-disc">$1</li>')
    .replace(/\n{2,}/g, '</p><p class="mt-2">')
    .replace(/^(?!<[hlu])/gm, '');
}
