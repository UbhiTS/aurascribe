// Deterministic avatar generator. Turns a name into a circle with initials
// and a stable background color drawn from a curated palette.
// No photos — we don't have them and don't want to fetch anything remote.

const PALETTE = [
  "from-brand-500 to-brand-700",
  "from-emerald-500 to-emerald-700",
  "from-amber-500 to-amber-700",
  "from-pink-500 to-pink-700",
  "from-cyan-500 to-cyan-700",
  "from-purple-500 to-purple-700",
  "from-rose-500 to-rose-700",
  "from-teal-500 to-teal-700",
  "from-indigo-500 to-indigo-700",
];

function hash(s: string): number {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) & 0xffffffff;
  return Math.abs(h);
}

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
}

const SIZE = {
  xs: "w-5 h-5 text-[9px]",
  sm: "w-6 h-6 text-[10px]",
  md: "w-8 h-8 text-xs",
  lg: "w-10 h-10 text-sm",
};

export function Avatar({ name, size = "md", className = "" }: Props) {
  const gradient = PALETTE[hash(name) % PALETTE.length];
  return (
    <div
      className={`flex-shrink-0 rounded-full bg-gradient-to-br ${gradient} flex items-center justify-center font-semibold text-white shadow-inner ${SIZE[size]} ${className}`}
      title={name}
    >
      {initials(name)}
    </div>
  );
}
