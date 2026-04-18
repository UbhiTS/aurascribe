import { useState, useEffect, useCallback, useRef } from "react";
import { Users, Zap, Pencil, Sparkles, Loader } from "lucide-react";
import { api } from "./lib/api";
import type { AppStatus, Meeting, Person, Utterance } from "./lib/api";
import { useWebSocket } from "./lib/useWebSocket";
import { RecordingBar } from "./components/RecordingBar";
import { TranscriptView } from "./components/TranscriptView";
import { MeetingList } from "./components/MeetingList";
import { SummaryPanel } from "./components/SummaryPanel";
import { EnrollmentModal } from "./components/EnrollmentModal";

type StatusEvent = "loading" | "ready" | "recording" | "processing" | "done" | "error" | "enrolling";

export default function App() {
  const [appStatus, setAppStatus] = useState<AppStatus | null>(null);
  const [selectedMeetingId, setSelectedMeetingId] = useState<string | null>(null);
  const [selectedMeeting, setSelectedMeeting] = useState<Meeting | null>(null);
  const [liveUtterances, setLiveUtterances] = useState<Utterance[]>([]);
  const [livePartial, setLivePartial] = useState<{ speaker: string; text: string } | null>(null);
  const [showEnroll, setShowEnroll] = useState(false);
  const [enrolled, setEnrolled] = useState<Person[]>([]);
  const [statusMessage, setStatusMessage] = useState("Loading models...");
  const [systemStatus, setSystemStatus] = useState<StatusEvent>("loading");
  const [refreshKey, setRefreshKey] = useState(0);
  const [summarizing, setSummarizing] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const selectedMeetingIdRef = useRef<string | null>(null);

  useEffect(() => {
    selectedMeetingIdRef.current = selectedMeetingId;
  }, [selectedMeetingId]);

  // Bootstrap + poll until the engine reports ready. The `status:ready`
  // WebSocket broadcast fires once during the sidecar's lifespan, and if
  // the socket connects after that (e.g. cached-model fast boot), we'd be
  // stuck on "Loading models" forever without this fallback.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await api.status();
        if (cancelled) return;
        setAppStatus(s);
        if (s.engine_ready) {
          setSystemStatus((prev) => (prev === "loading" ? "ready" : prev));
          setStatusMessage((prev) => (prev === "Loading models..." ? "" : prev));
          return; // done polling
        }
      } catch {
        // sidecar not up yet; retry
      }
      if (!cancelled) setTimeout(tick, 1000);
    };
    tick();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!selectedMeetingId) { setSelectedMeeting(null); return; }
    api.meetings.get(selectedMeetingId).then(setSelectedMeeting).catch(console.error);
  }, [selectedMeetingId]);

  // Keep the enrolled-people list fresh on mount and whenever the
  // enrollment modal closes (may have added a new person).
  const refreshEnrolled = useCallback(() => {
    api.people.list().then(setEnrolled).catch(() => {});
  }, []);
  useEffect(() => { refreshEnrolled(); }, [refreshEnrolled]);

  const handleWsMessage = useCallback((msg: any) => {
    if (msg.type === "partial_utterance" && msg.meeting_id === selectedMeetingIdRef.current) {
      if (!msg.text) {
        setLivePartial(null);
      } else {
        // Grow-only: never replace a longer partial with a shorter one (ASR can vary slightly)
        setLivePartial((prev) =>
          !prev || msg.text.length >= prev.text.length
            ? { speaker: msg.speaker, text: msg.text }
            : prev
        );
      }
    }
    if (msg.type === "utterances" && msg.meeting_id === selectedMeetingIdRef.current) {
      setLivePartial(null); // finalized — clear the live typing line
      setLiveUtterances((prev) => [...prev, ...msg.data]);
    }
    if (msg.type === "status") {
      setSystemStatus(msg.event as StatusEvent);
      setStatusMessage(msg.message ?? "");
      if (msg.event === "done") {
        setRefreshKey((k) => k + 1);
        if (msg.meeting_id) {
          api.meetings.get(msg.meeting_id).then(setSelectedMeeting).catch(() => {});
        }
      }
    }
  }, []);

  useWebSocket(handleWsMessage);

  const handleMeetingStarted = (id: string) => {
    setSelectedMeetingId(id);
    setLiveUtterances([]);
    setLivePartial(null);
    setAppStatus((s) => s ? { ...s, is_recording: true, current_meeting_id: id } : s);
    setRefreshKey((k) => k + 1);
  };

  const handleMeetingStopped = () => {
    setAppStatus((s) => s ? { ...s, is_recording: false, current_meeting_id: null } : s);
  };

  const handleRenameTitle = async () => {
    if (!selectedMeetingId || !titleDraft.trim()) { setEditingTitle(false); return; }
    await api.meetings.rename(selectedMeetingId, titleDraft.trim());
    setSelectedMeeting((m) => m ? { ...m, title: titleDraft.trim() } : m);
    setRefreshKey((k) => k + 1);
    setEditingTitle(false);
  };

  const handleSummarize = async () => {
    if (!selectedMeetingId || summarizing) return;
    setSummarizing(true);
    try {
      const updated = await api.meetings.summarize(selectedMeetingId);
      setSelectedMeeting(updated);
    } finally {
      setSummarizing(false);
    }
  };

  const handleMeetingDeleted = (ids: string[]) => {
    if (selectedMeetingId !== null && ids.includes(selectedMeetingId)) {
      setSelectedMeetingId(null);
      setSelectedMeeting(null);
      setLiveUtterances([]);
      setLivePartial(null);
    }
  };

  const isRecording = appStatus?.is_recording ?? false;

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-gray-100 overflow-hidden">
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-gray-950/95 backdrop-blur-sm flex-shrink-0">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-brand-600 flex items-center justify-center">
            <Zap size={14} className="text-white" />
          </div>
          <span className="font-semibold text-gray-100 text-sm tracking-tight">AuraScribe</span>
        </div>

        <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border ${
          systemStatus === "loading" ? "border-amber-800/50 text-amber-400 bg-amber-950/30" :
          systemStatus === "recording" ? "border-red-800/50 text-red-400 bg-red-950/30" :
          systemStatus === "processing" ? "border-amber-800/50 text-amber-400 bg-amber-950/30" :
          "border-gray-800 text-gray-500 bg-gray-900/50"
        }`}>
          <div className={`w-1.5 h-1.5 rounded-full ${
            systemStatus === "loading" || systemStatus === "processing" ? "bg-amber-500 animate-pulse" :
            systemStatus === "recording" ? "bg-red-500 animate-pulse" :
            "bg-emerald-500"
          }`} />
          {systemStatus === "loading" ? "Loading models" :
           systemStatus === "recording" ? "Recording" :
           systemStatus === "processing" ? "Processing" :
           statusMessage || "Ready"}
        </div>

        <button
          onClick={() => setShowEnroll(true)}
          title={
            enrolled.length === 0
              ? "Voice Enrollment — not yet enrolled"
              : `Voice Enrollment — enrolled: ${enrolled.map((p) => p.name).join(", ")}`
          }
          className="relative p-2 text-gray-500 hover:text-gray-300 hover:bg-gray-800 rounded-lg transition-colors"
        >
          <Users size={16} />
          {enrolled.length > 0 && (
            <span
              className="absolute top-1 right-1 w-2 h-2 rounded-full bg-emerald-500 ring-2 ring-gray-950"
              aria-label="enrolled"
            />
          )}
        </button>
      </header>

      {/* Recording bar */}
      <div className="px-4 py-2 border-b border-gray-800/50 flex-shrink-0">
        <RecordingBar
          isRecording={isRecording}
          devices={appStatus?.audio_devices ?? []}
          onStarted={handleMeetingStarted}
          onStopped={handleMeetingStopped}
        />
      </div>

      {/* Main layout */}
      <div className="flex-1 flex min-h-0">
        <aside className="w-56 flex-shrink-0 border-r border-gray-800 flex flex-col min-h-0">
          <MeetingList
            selectedId={selectedMeetingId}
            activeMeetingId={appStatus?.current_meeting_id ?? null}
            onSelect={(id) => { setSelectedMeetingId(id); setLiveUtterances([]); }}
            onDeleted={handleMeetingDeleted}
            refreshKey={refreshKey}
          />
        </aside>

        <main className="flex-1 flex flex-col min-h-0 border-r border-gray-800">
          <div className="px-4 py-2.5 border-b border-gray-800 flex-shrink-0 flex items-center gap-3">
            {/* Title + date */}
            <div className="flex-1 min-w-0">
              {editingTitle ? (
                <input
                  autoFocus
                  value={titleDraft}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") handleRenameTitle(); if (e.key === "Escape") setEditingTitle(false); }}
                  onBlur={handleRenameTitle}
                  className="w-full text-sm font-semibold bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-gray-200 outline-none"
                />
              ) : (
                <div className="flex items-center gap-1.5 group/title min-w-0">
                  <h1 className="text-sm font-semibold text-gray-200 truncate">
                    {selectedMeeting?.title ?? (isRecording ? "Recording..." : "No meeting selected")}
                  </h1>
                  {selectedMeeting && (
                    <button
                      onClick={() => { setTitleDraft(selectedMeeting.title); setEditingTitle(true); }}
                      title="Rename meeting"
                      className="flex-shrink-0 opacity-0 group-hover/title:opacity-100 text-gray-600 hover:text-gray-300 transition-all"
                    >
                      <Pencil size={12} />
                    </button>
                  )}
                </div>
              )}
              {selectedMeeting?.started_at && (
                <p className="text-xs text-gray-500">
                  {new Date(selectedMeeting.started_at).toLocaleString()}
                </p>
              )}
            </div>

            {/* AI Summary button */}
            {selectedMeeting && selectedMeeting.status === "done" && (
              <button
                onClick={handleSummarize}
                disabled={summarizing}
                className="flex-shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 border-brand-700 text-brand-400 bg-brand-600/10 hover:bg-brand-600/20"
              >
                {summarizing
                  ? <Loader size={12} className="animate-spin" />
                  : <Sparkles size={12} />}
                AI Summary
              </button>
            )}
          </div>
          <TranscriptView
            meetingId={selectedMeetingId}
            liveUtterances={liveUtterances}
            livePartial={livePartial}
            isRecording={isRecording}
            enrolled={enrolled}
            onEnrolledChanged={refreshEnrolled}
          />
        </main>

        <aside className="w-80 flex-shrink-0 flex flex-col min-h-0">
          <div className="flex-1 min-h-0">
            <SummaryPanel meeting={selectedMeeting} />
          </div>
        </aside>
      </div>

      {showEnroll && (
        <EnrollmentModal
          enrolled={enrolled}
          onClose={() => {
            setShowEnroll(false);
            refreshEnrolled();
          }}
        />
      )}
    </div>
  );
}
