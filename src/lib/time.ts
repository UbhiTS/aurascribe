/**
 * Wall-clock time formatters used across the app for any user-facing
 * timestamp. Centralised so the format stays consistent — everywhere we
 * show a time we render `11:25:05 AM`, and any date-bearing variant
 * pairs the same clock fragment with a short date.
 *
 * The server stores timestamps as naive-local ISO strings
 * (`datetime.now().isoformat()`), which JavaScript's `new Date(iso)`
 * parses as local time — no UTC conversion needed.
 */

const TIME_OPTS: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
};

const DATE_OPTS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "short",
  day: "numeric",
};

/** "11:25:05 AM" — empty string if the input is null/undefined/invalid. */
export function fmtClockTime(value: Date | string | null | undefined): string {
  if (!value) return "";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], TIME_OPTS);
}

/** "Apr 26, 2026, 11:25:05 AM". */
export function fmtDateTime(value: Date | string | null | undefined): string {
  if (!value) return "";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString([], { ...DATE_OPTS, ...TIME_OPTS });
}
