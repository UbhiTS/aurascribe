import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { api, EMPTY_LIVE_INTEL, liveIntelFromMeeting } from "./lib/api";
import type {
  ActionItemOther,
  AppStatus,
  LiveIntel,
  Meeting,
  Utterance,
  Voice,
} from "./lib/api";
import { useWebSocket } from "./lib/useWebSocket";
import { useLLMHealth } from "./lib/useLLMHealth";
import { Shell } from "./components/Shell";
import type { Page } from "./components/Sidebar";
import { WelcomeDialog } from "./components/WelcomeDialog";

// Pages lazy-loaded so the initial bundle only carries the Live page. Every
// other page is a separate chunk fetched when the user navigates to it.
const LiveFeed = lazy(() =>
  import("./pages/LiveFeed").then((m) => ({ default: m.LiveFeed })),
);
const MeetingLibrary = lazy(() =>
  import("./pages/MeetingLibrary").then((m) => ({ default: m.MeetingLibrary })),
);
const Review = lazy(() =>
  import("./pages/Review").then((m) => ({ default: m.Review })),
);
const Voices = lazy(() =>
  import("./pages/Voices").then((m) => ({ default: m.Voices })),
);
const DailyBrief = lazy(() =>
  import("./pages/DailyBrief").then((m) => ({ default: m.DailyBrief })),
);
const Settings = lazy(() =>
  import("./pages/Settings").then((m) => ({ default: m.Settings })),
);

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error";

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
  const [voices, setVoices] = useState<Voice[]>([]);
  // Pushed by WS whenever the daily brief for a given date changes state.
  // DailyBrief watches this and refetches when the date matches its view.
  const [dailyBriefSignal, setDailyBriefSignal] = useState<
    { date: string; status: "refreshing" | "ready" | "stale"; tick: number } | null
  >(null);
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

  // Two-phase /api/status polling:
  //   1. Startup: poll every 1s until engine_ready. WS "ready" broadcast
  //      can fire before the client's socket connects on cached-model
  //      fast boot, so we need the pull too.
  //   2. Heartbeat: once ready, keep a slow 30s poll running. If the
  //      sidecar crashes or is killed (OOM, user task-manager), we want
  //      the UI to notice and surface the disconnect state instead of
  //      looking healthy but being functionally dead.
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    let isReady = false;  // local, so the timing doesn't depend on React state flush

    const tick = async () => {
      try {
        const s = await api.status();
        if (cancelled) return;
        setAppStatus(s);
        if (s.engine_ready) {
          isReady = true;
          setSystemStatus((prev) => (prev === "loading" ? "ready" : prev));
          setStatusMessage((prev) => (prev === "Loading models..." ? "" : prev));
        }
      } catch {
        // Request failed — sidecar not up yet (startup) or crashed (post-ready).
        if (!cancelled && isReady) {
          setSystemStatus("error");
          setStatusMessage("Sidecar unreachable");
        }
      }
      if (!cancelled) {
        // Fast poll during startup; slow heartbeat after ready.
        timer = window.setTimeout(tick, isReady ? 30_000 : 1000);
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  const refreshVoices = useCallback(() => {
    api.voices.list().then(setVoices).catch(() => {});
  }, []);
  useEffect(() => { refreshVoices(); }, [refreshVoices]);

  // Dismiss the static HTML splash (rendered by index.html) once the engine
  // is ready. Done via DOM mutation rather than React state because the
  // splash element lives outside the React tree — keeping it in raw HTML
  // means it paints on the very first webview frame, long before Vite
  // optimises deps or React mounts.
  useEffect(() => {
    if (systemStatus === "loading") return;
    const el = document.getElementById("splash");
    if (!el) return;
    el.classList.add("hidden");
    const t = window.setTimeout(() => el.remove(), 500);
    return () => window.clearTimeout(t);
  }, [systemStatus]);

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
    if (msg.type === "daily_brief_updated" && typeof msg.date === "string") {
      setDailyBriefSignal((prev) => ({
        date: msg.date,
        status: (msg.status === "refreshing" || msg.status === "ready" || msg.status === "stale")
          ? msg.status
          : "ready",
        tick: (prev?.tick ?? 0) + 1,
      }));
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
  const { connected: wsConnected } = useWebSocket(handleWsMessage);
  const llm = useLLMHealth();

  const handleMeetingStarted = (id: string) => {
    setLiveMeetingId(id);
    setLiveUtterances([]);
    setLivePartial(null);
    setLiveIntel(EMPTY_LIVE_INTEL);
    setAppStatus((s) => s ? { ...s, is_recording: true, current_meeting_id: id } : s);
    // Hand-patching the local status above leaves `active_audio_device`
    // stale — the initial /api/status polling stopped once engine_ready.
    // Pull fresh so the header reflects the mic the sidecar actually opened.
    api.status().then(setAppStatus).catch(() => {});
    setRefreshKey((k) => k + 1);
  };

  const handleMeetingStopped = () => {
    setAppStatus((s) => s ? { ...s, is_recording: false, current_meeting_id: null, active_audio_device: null } : s);
  };

  // Authoritative — sourced from /api/status (config.OBSIDIAN_VAULT). This
  // doesn't wait for a vault_path to be stamped on a meeting after the first
  // markdown write, which used to make the header lie for the first ~15s of
  // a recording.
  const obsidianConfigured = appStatus?.obsidian_configured ?? false;

  const isRecording = appStatus?.is_recording ?? false;

  return (
    <Shell
      page={page}
      onNavigate={setPage}
      wsConnected={wsConnected}
      llm={llm}
      activeAudioDevice={appStatus?.active_audio_device ?? null}
      isRecording={isRecording}
      systemStatus={systemStatus}
      statusMessage={statusMessage}
      obsidianConfigured={obsidianConfigured}
      hardware={appStatus?.hardware ?? null}
      asr={appStatus?.asr ?? null}
      diarization={appStatus?.diarization ?? null}
    >
      {/* Only render once the engine has finished loading — the welcome
          dialog displays the detected hardware, so we want the status
          poll to have brought in appStatus.hardware first. */}
      {systemStatus !== "loading" && appStatus?.hardware && (
        <WelcomeDialog
          hardware={appStatus.hardware}
          onOpenSettings={() => setPage("settings")}
        />
      )}
      <Suspense fallback={null}>
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
            voices={voices}
            onVoicesChanged={refreshVoices}
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
            voices={voices}
            onVoicesChanged={refreshVoices}
            onBack={() => setPage("library")}
            onMeetingChanged={() => setRefreshKey((k) => k + 1)}
            onOpenMeeting={(id) => { setReviewMeetingId(id); }}
          />
        )}
        {page === "voices" && (
          <Voices voices={voices} onVoicesChanged={refreshVoices} />
        )}
        {page === "daily" && <DailyBrief signal={dailyBriefSignal} />}
        {page === "settings" && <Settings appStatus={appStatus} obsidianConfigured={obsidianConfigured} />}
      </Suspense>
    </Shell>
  );
}
