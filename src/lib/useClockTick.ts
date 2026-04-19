import { useEffect, useState } from "react";

/** Re-render the calling component on a fixed interval so that derived
 *  reads of `Date.now()` / wall-clock time stay fresh without the component
 *  having to manage its own setInterval bookkeeping.
 *
 *  Usage:
 *    useClockTick(500, isRecording);    // tick while recording
 *    const now = Date.now();            // fresh every 500ms
 *
 *  The returned value is a monotonically increasing counter that also works
 *  as a dependency key for memos/effects that need to react to the tick
 *  (rarely needed — most callers just ignore it).
 *
 *  Behaviour:
 *    * `enabled=false` ⇒ interval is torn down, no renders scheduled.
 *    * Switching `enabled` resets the counter to 0 — downstream effects
 *      keyed on the tick can use that as a "fresh cycle" signal.
 */
export function useClockTick(intervalMs: number, enabled: boolean): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) {
      setTick(0);
      return;
    }
    const id = window.setInterval(() => setTick((c) => c + 1), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, enabled]);
  return tick;
}
