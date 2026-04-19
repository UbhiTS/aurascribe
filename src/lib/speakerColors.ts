// Global color palette for speakers. Every speaker has the SAME color
// across Live Feed, Meeting Review, the Voices page, and anywhere else
// their avatar shows up.
//
// Two-tier resolution:
//   1. If the speaker is a registered Voice, use the palette *key*
//      persisted on `voice.color` by the sidecar (see routes/_shared.py
//      `next_voice_color` / `backfill_voice_colors`). The server picks
//      the least-used slot on voice creation, so every Voice gets a
//      distinct color up to the palette size — and the slot survives
//      deletion of other voices because it's stored on the row.
//   2. Otherwise (provisional "Speaker N" clusters, or names the user
//      hasn't turned into a Voice yet) fall back to a name hash. These
//      speakers are transient and collisions don't really matter.
//
// "Unknown" is reserved: always gray so it visually recedes.
//
// The PALETTE_KEYS order MUST stay in lock-step with the VOICE_PALETTE_KEYS
// tuple in routes/_shared.py — keys are persisted, and reordering would
// scramble every stored voice's color.
//
// All colors must have the 500/700 (gradient), 500/30 (border),
// 500/15 (bg), and 50 (text) shades in the Tailwind theme.

import { api } from "./api";
import type { Voice } from "./api";

export interface SpeakerColor {
  avatar: string; // `from-X-500 to-X-700` — consumed by Avatar
  bubble: string; // bg + border + text classes for the message pill
  border: string; // `border-X-500/40` — used by header chip rows
}

// Fixed reserved color for the "Unknown" placeholder — muted gray so it
// visually recedes against real tagged speakers.
export const UNKNOWN_COLOR: SpeakerColor = {
  avatar: "from-gray-500 to-gray-700",
  bubble: "bg-gray-500/15 border-gray-500/30 text-gray-200",
  border: "border-gray-500/40",
};

// Keyed palette — key order MUST match routes/_shared.py VOICE_PALETTE_KEYS.
// Hues chosen ~40° apart on the color wheel so no two slots read as
// "similar" at a glance.
export const PALETTE_KEYS = [
  "rose", "orange", "yellow", "lime", "emerald", "cyan", "blue", "violet", "fuchsia",
] as const;
export type PaletteKey = (typeof PALETTE_KEYS)[number];

export const SPEAKER_PALETTE: Record<PaletteKey, SpeakerColor> = {
  rose:    { avatar: "from-rose-500 to-rose-700",       bubble: "bg-rose-500/15 border-rose-500/30 text-rose-50",       border: "border-rose-500/40" },      // 350°
  orange:  { avatar: "from-orange-500 to-orange-700",   bubble: "bg-orange-500/15 border-orange-500/30 text-orange-50",   border: "border-orange-500/40" },    //  25°
  yellow:  { avatar: "from-yellow-500 to-yellow-700",   bubble: "bg-yellow-500/15 border-yellow-500/30 text-yellow-50",   border: "border-yellow-500/40" },    //  55°
  lime:    { avatar: "from-lime-500 to-lime-700",       bubble: "bg-lime-500/15 border-lime-500/30 text-lime-50",       border: "border-lime-500/40" },      //  85°
  emerald: { avatar: "from-emerald-500 to-emerald-700", bubble: "bg-emerald-500/15 border-emerald-500/30 text-emerald-50", border: "border-emerald-500/40" },  // 160°
  cyan:    { avatar: "from-cyan-500 to-cyan-700",       bubble: "bg-cyan-500/15 border-cyan-500/30 text-cyan-50",       border: "border-cyan-500/40" },      // 190°
  blue:    { avatar: "from-blue-500 to-blue-700",       bubble: "bg-blue-500/15 border-blue-500/30 text-blue-50",       border: "border-blue-500/40" },      // 220°
  violet:  { avatar: "from-violet-500 to-violet-700",   bubble: "bg-violet-500/15 border-violet-500/30 text-violet-50",   border: "border-violet-500/40" },    // 270°
  fuchsia: { avatar: "from-fuchsia-500 to-fuchsia-700", bubble: "bg-fuchsia-500/15 border-fuchsia-500/30 text-fuchsia-50", border: "border-fuchsia-500/40" },  // 300°
};

function hash(s: string): number {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) & 0xffffffff;
  return Math.abs(h);
}

function fallbackColor(name: string): SpeakerColor {
  return SPEAKER_PALETTE[PALETTE_KEYS[hash(name) % PALETTE_KEYS.length]];
}

export function colorForSpeaker(name: string, voices?: Voice[]): SpeakerColor {
  if (!name || name === "Unknown") return UNKNOWN_COLOR;
  // Voice-backed: read the persisted slot key off the Voice row. The
  // server guarantees uniqueness up to palette size, and the key never
  // shifts when other voices are added or removed.
  if (voices && voices.length > 0) {
    const v = voices.find((x) => x.name === name);
    if (v?.color && v.color in SPEAKER_PALETTE) {
      return SPEAKER_PALETTE[v.color as PaletteKey];
    }
  }
  // Non-voice speakers (Speaker N clusters, untagged names): name-hash
  // fallback. Collisions possible but they're transient speakers anyway.
  return fallbackColor(name);
}

// URL for the custom uploaded avatar of `name`, or null if the speaker
// has no Voice row or no uploaded image. `updated_at` is used as a
// cache-busting query param so that when the user swaps images mid-
// session, the WebView fetches the new file instead of replaying the
// cached one.
export function avatarSrcFor(name: string, voices?: Voice[]): string | null {
  if (!voices || !name) return null;
  const v = voices.find((x) => x.name === name);
  if (!v || !v.avatar_ext) return null;
  return api.voices.avatarUrl(v.id, v.updated_at);
}
