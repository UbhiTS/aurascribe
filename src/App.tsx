import { useCallback, useEffect, useRef, useState } from "react";
import { api, EMPTY_LIVE_INTEL, liveIntelFromMeeting } from "./lib/api";
import type {
  ActionItemOther,
  AppStatus,
  LiveIntel,
  Meeting,
  Person,
  Utterance,
} from "./lib/api";
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
  // Two completely separate meeting slices:
  //  - live*  — driven by recording start/stop + WebSocket utterance stream; owned by LiveFeed.
  //  - review*— driven by clicks in Meeting Library; owned by Review page.
  // Loading a library meeting must NEVER touch live* (or vice versa).
  const [liveMeetingId, setLiveMeetingId] = useState<string | null>(null);
  const [liveMeeting, setLiveMeeting] = useState<Meeting | null>(null);
  const [reviewMeetingId, setReviewMeetingId] = useState<string | null>(null);
  const [reviewMeeting, setReviewMeeting] = useState<Meeting | null>(null);
  const [liveUtterances, setLiveUtterances] = useState<Utterance[]>([]);
  const [livePartial, setLivePartial] = useState<{ speaker: string; text: string } | null>(null);
  const [liveIntel, setLiveIntel] = useState<LiveIntel>(EMPTY_LIVE_INTEL);
  // Bumped on each WS push so the UI can flash an indicator.
  const [intelTick, setIntelTick] = useState(0);
  const [statusMessage, setStatusMessage] = useState("Loading models...");
  const [systemStatus, setSystemStatus] = useState<StatusEvent>("loading");
  const [refreshKey, setRefreshKey] = useState(0);
  const [enrolled, setEnrolled] = useState<Person[]>([]);
  // WS utterance filter keys off the LIVE meeting only — library loads can't redirect the stream.
  const liveMeetingIdRef = useRef<string | null>(null);

  useEffect(() => { liveMeetingIdRef.current = liveMeetingId; }, [liveMeetingId]);

  useEffect(() => {
    if (!liveMeetingId) { setLiveMeeting(null); setLiveIntel(EMPTY_LIVE_INTEL); return; }
    api.meetings.get(liveMeetingId).then((m) => {
      setLiveMeeting(m);
      setLiveIntel(liveIntelFromMeeting(m));
    }).catch(console.error);
  }, [liveMeetingId]);

  // Re-adopt an in-flight recording if the sidecar outlived the frontend
  // (HMR reload, window refresh, dev restart). Without this, WS utterances
  // would be dropped because the meeting-id filter is still null.
  useEffect(() => {
    if (liveMeetingId) return;
    if (appStatus?.is_recording && appStatus.current_meeting_id) {
      setLiveMeetingId(appStatus.current_meeting_id);
      setLiveUtterances([]);
      setLivePartial(null);
    }
  }, [appStatus, liveMeetingId]);

  useEffect(() => {
    if (!reviewMeetingId) { setReviewMeeting(null); return; }
    api.meetings.get(reviewMeetingId).then(setReviewMeeting).catch(console.error);
  }, [reviewMeetingId]);

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
    if (msg.type === "partial_utterance" && msg.meeting_id === liveMeetingIdRef.current) {
      if (!msg.text) setLivePartial(null);
      else setLivePartial((prev) =>
        !prev || msg.text.length >= prev.text.length
          ? { speaker: msg.speaker, text: msg.text }
          : prev);
    }
    if (msg.type === "utterances" && msg.meeting_id === liveMeetingIdRef.current) {
      setLivePartial(null);
      setLiveUtterances((prev) => [...prev, ...msg.data]);
    }
    if (msg.type === "realtime_intelligence" && msg.meeting_id === liveMeetingIdRef.current) {
      setLiveIntel({
        highlights: Array.isArray(msg.highlights) ? msg.highlights : [],
        actionItemsSelf: Array.isArray(msg.action_items_self) ? msg.action_items_self : [],
        actionItemsOthers: Array.isArray(msg.action_items_others)
          ? (msg.action_items_others as ActionItemOther[])
          : [],
        supportIntelligence: typeof msg.support_intelligence === "string" ? msg.support_intelligence : "",
      });
      setIntelTick((t) => t + 1);
    }
    if (msg.type === "status") {
      setSystemStatus(msg.event as StatusEvent);
      setStatusMessage(msg.message ?? "");
      if (msg.event === "done" && msg.meeting_id) {
        setRefreshKey((k) => k + 1);
        // Refresh the live meeting card with its finalized data (summary, action items, vault_path).
        // Only the live pane gets updated — review's meeting is untouched.
        if (msg.meeting_id === liveMeetingIdRef.current) {
          api.meetings.get(msg.meeting_id).then(setLiveMeeting).catch(() => {});
        }
      }
    }
  }, []);
  useWebSocket(handleWsMessage);

  const handleMeetingStarted = (id: string) => {
    setLiveMeetingId(id);
    setLiveUtterances([]);
    setLivePartial(null);
    setLiveIntel(EMPTY_LIVE_INTEL);
    setAppStatus((s) => s ? { ...s, is_recording: true, current_meeting_id: id } : s);
    setRefreshKey((k) => k + 1);
  };

  const handleMeetingStopped = () => {
    setAppStatus((s) => s ? { ...s, is_recording: false, current_meeting_id: null } : s);
  };

  // Heuristic: if the current live or review meeting has a vault_path, Obsidian is writing.
  // (Full check would need a /api/settings/obsidian endpoint.)
  const obsidianConfigured = !!(liveMeeting?.vault_path || reviewMeeting?.vault_path);

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
          meeting={liveMeeting}
          setMeeting={setLiveMeeting}
          meetingId={liveMeetingId}
          liveUtterances={liveUtterances}
          livePartial={livePartial}
          liveIntel={liveIntel}
          intelTick={intelTick}
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
          // Load a library meeting into Review only. Live pane is untouched.
          onOpen={(id) => { setReviewMeetingId(id); setPage("review"); }}
          selectedId={reviewMeetingId}
        />
      )}
      {page === "review" && (
        <Review
          meeting={reviewMeeting}
          meetingId={reviewMeetingId}
          setMeeting={setReviewMeeting}
          enrolled={enrolled}
          onEnrolledChanged={refreshEnrolled}
          onBack={() => setPage("library")}
          onMeetingChanged={() => setRefreshKey((k) => k + 1)}
          onOpenMeeting={(id) => { setReviewMeetingId(id); }}
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
