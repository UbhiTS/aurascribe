import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./lib/api";
import type { AppStatus, Meeting, Person, Utterance } from "./lib/api";
import { useWebSocket } from "./lib/useWebSocket";
import { Shell } from "./components/Shell";
import type { Page } from "./components/Sidebar";
import { LiveFeed } from "./pages/LiveFeed";
import { MeetingLibrary } from "./pages/MeetingLibrary";
import { Review } from "./pages/Review";
import { Enrollment } from "./pages/Enrollment";
import { DailyBrief } from "./pages/DailyBrief";
import { Settings } from "./pages/Settings";

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error" | "enrolling";

export default function App() {
  const [page, setPage] = useState<Page>("live");
  const [appStatus, setAppStatus] = useState<AppStatus | null>(null);
  const [selectedMeetingId, setSelectedMeetingId] = useState<string | null>(null);
  const [selectedMeeting, setSelectedMeeting] = useState<Meeting | null>(null);
  const [liveUtterances, setLiveUtterances] = useState<Utterance[]>([]);
  const [livePartial, setLivePartial] = useState<{ speaker: string; text: string } | null>(null);
  const [statusMessage, setStatusMessage] = useState("Loading models...");
  const [systemStatus, setSystemStatus] = useState<StatusEvent>("loading");
  const [refreshKey, setRefreshKey] = useState(0);
  const [enrolled, setEnrolled] = useState<Person[]>([]);
  const selectedMeetingIdRef = useRef<string | null>(null);

  useEffect(() => { selectedMeetingIdRef.current = selectedMeetingId; }, [selectedMeetingId]);

  useEffect(() => {
    if (!selectedMeetingId) { setSelectedMeeting(null); return; }
    api.meetings.get(selectedMeetingId).then(setSelectedMeeting).catch(console.error);
  }, [selectedMeetingId]);

  // Poll /api/status until engine_ready — WS "ready" broadcast can fire before
  // the client's socket connects on cached-model fast boot.
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
          return;
        }
      } catch {
        // sidecar not up yet
      }
      if (!cancelled) setTimeout(tick, 1000);
    };
    tick();
    return () => { cancelled = true; };
  }, []);

  const refreshEnrolled = useCallback(() => {
    api.people.list().then(setEnrolled).catch(() => {});
  }, []);
  useEffect(() => { refreshEnrolled(); }, [refreshEnrolled]);

  const handleWsMessage = useCallback((msg: any) => {
    if (msg.type === "partial_utterance" && msg.meeting_id === selectedMeetingIdRef.current) {
      if (!msg.text) setLivePartial(null);
      else setLivePartial((prev) =>
        !prev || msg.text.length >= prev.text.length
          ? { speaker: msg.speaker, text: msg.text }
          : prev);
    }
    if (msg.type === "utterances" && msg.meeting_id === selectedMeetingIdRef.current) {
      setLivePartial(null);
      setLiveUtterances((prev) => [...prev, ...msg.data]);
    }
    if (msg.type === "status") {
      setSystemStatus(msg.event as StatusEvent);
      setStatusMessage(msg.message ?? "");
      if (msg.event === "done") {
        setRefreshKey((k) => k + 1);
        if (msg.meeting_id) api.meetings.get(msg.meeting_id).then(setSelectedMeeting).catch(() => {});
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

  // Heuristic: if any meeting has a vault_path, Obsidian is writing.
  // (Full check would need a /api/settings/obsidian endpoint.)
  const obsidianConfigured = !!selectedMeeting?.vault_path;

  // Device name lookup for the header.
  const activeDeviceName = appStatus?.audio_devices[0]?.name ?? null;

  return (
    <Shell
      page={page}
      onNavigate={setPage}
      selectedDeviceName={activeDeviceName}
      systemStatus={systemStatus}
      statusMessage={statusMessage}
      obsidianConfigured={obsidianConfigured}
    >
      {page === "live" && (
        <LiveFeed
          appStatus={appStatus}
          selectedMeeting={selectedMeeting}
          setSelectedMeeting={setSelectedMeeting}
          selectedMeetingId={selectedMeetingId}
          setSelectedMeetingId={setSelectedMeetingId}
          liveUtterances={liveUtterances}
          livePartial={livePartial}
          enrolled={enrolled}
          onEnrolledChanged={refreshEnrolled}
          onMeetingStarted={handleMeetingStarted}
          onMeetingStopped={handleMeetingStopped}
          bumpRefreshKey={() => setRefreshKey((k) => k + 1)}
        />
      )}
      {page === "library" && (
        <MeetingLibrary
          activeMeetingId={appStatus?.current_meeting_id ?? null}
          refreshKey={refreshKey}
          onOpen={(id) => { setSelectedMeetingId(id); setLiveUtterances([]); setPage("review"); }}
          selectedId={selectedMeetingId}
        />
      )}
      {page === "review" && (
        <Review
          meeting={selectedMeeting}
          meetingId={selectedMeetingId}
          setMeeting={setSelectedMeeting}
          enrolled={enrolled}
          onEnrolledChanged={refreshEnrolled}
          onBack={() => setPage("library")}
          onMeetingChanged={() => setRefreshKey((k) => k + 1)}
          onOpenMeeting={(id) => { setSelectedMeetingId(id); }}
        />
      )}
      {page === "enrollment" && (
        <Enrollment enrolled={enrolled} onEnrolledChanged={refreshEnrolled} />
      )}
      {page === "daily" && <DailyBrief />}
      {page === "settings" && <Settings appStatus={appStatus} obsidianConfigured={obsidianConfigured} />}
    </Shell>
  );
}
