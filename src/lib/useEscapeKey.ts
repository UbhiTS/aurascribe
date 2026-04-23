import { useEffect } from "react";

/** Add an Escape-key listener while `active` is truthy.
 *
 *  Standard dialog dismiss affordance — every modal in the app should
 *  register one. Caller passes the same handler they pass to a Close /
 *  Dismiss button so Escape is equivalent.
 *
 *  `active` lets the caller gate the listener to when their modal is
 *  actually open, avoiding dozens of idle listeners in the tree.
 */
export function useEscapeKey(onEscape: () => void, active: boolean = true): void {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onEscape();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onEscape, active]);
}
