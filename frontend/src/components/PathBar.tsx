import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";

export interface PathSegment {
  label: string;
  to?: string;
}

// A literal, clickable `okf://` path mirroring the project's own philosophy
// that a human-readable file path *is* the search surface (wiki/okf.md).
export function PathBar({ segments }: { segments: PathSegment[] }) {
  const navigate = useNavigate();
  return (
    <nav
      className="flex min-w-0 items-center gap-0.5 overflow-x-auto whitespace-nowrap font-mono text-xs text-muted-foreground [scrollbar-width:none]"
      aria-label="경로"
    >
      <span className="text-muted-foreground/60">okf://</span>
      {segments.map((seg, i) => (
        <span key={i} className="contents">
          {i > 0 && <span className="mx-0.5 text-muted-foreground/60">/</span>}
          {seg.to ? (
            <span
              className="cursor-pointer rounded px-1 py-0.5 hover:bg-accent hover:text-accent-foreground"
              role="link"
              tabIndex={0}
              onClick={() => navigate(seg.to as string)}
              onKeyDown={(e) => e.key === "Enter" && navigate(seg.to as string)}
            >
              {seg.label}
            </span>
          ) : (
            <span className={cn("rounded px-1 py-0.5 font-medium text-foreground")}>{seg.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
