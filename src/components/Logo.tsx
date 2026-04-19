// Feather mark served from public/logos/feather.svg. Two-tone recolour
// (soft lavender #b8a8e8 centre, dark lavender #6d5c95 accents) is baked
// into the SVG — no runtime transform.

interface Props {
  size?: number;
  className?: string;
}

export function Logo({ size = 36, className = "" }: Props) {
  return (
    <img
      src="/logos/feather.svg"
      width={size}
      alt="AuraScribe"
      className={className}
      style={{
        height: "auto",
        filter:
          "drop-shadow(0 0 10px rgba(184, 168, 232, 0.55)) drop-shadow(0 0 22px rgba(109, 92, 149, 0.35))",
      }}
    />
  );
}
