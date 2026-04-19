// Deterministic avatar generator. Turns a name into a circle with initials
// and a stable background color drawn from a shared palette.
// No photos — we don't have them and don't want to fetch anything remote.
//
// Color assignment lives in lib/speakerColors.ts so the transcript
// bubbles, the meeting header chips, and Voices page all render the
// same person in the same color.

import { memo } from "react";
import { colorForSpeaker } from "../lib/speakerColors";

function initials(name: string): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

interface Props {
  name: string;
  size?: "xs" | "sm" | "md" | "lg";
  className?: string;
  // Override the name-derived color. Used when the caller has already
  // resolved a SpeakerColor (e.g. when rendering bubbles — they share
  // the same object with the bubble tint).
  gradient?: string;
}

const SIZE = {
  xs: "w-5 h-5 text-[9px]",
  sm: "w-6 h-6 text-[10px]",
  md: "w-8 h-8 text-xs",
  lg: "w-10 h-10 text-sm",
};

// Pure: rendered once per (name,size) tuple. TranscriptView renders many
// avatars per WS push, and the parent list re-renders on each — memo skips
// those re-renders since the props are stable.
export const Avatar = memo(function Avatar({ name, size = "md", className = "", gradient }: Props) {
  const resolved = gradient ?? colorForSpeaker(name).avatar;
  return (
    <div
      className={`flex-shrink-0 rounded-full bg-gradient-to-br ${resolved} flex items-center justify-center font-semibold text-white shadow-inner ${SIZE[size]} ${className}`}
      title={name}
    >
      {initials(name)}
    </div>
  );
});
