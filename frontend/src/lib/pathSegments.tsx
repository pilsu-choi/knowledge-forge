import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { PathSegment } from "@/components/PathBar";

const Ctx = createContext<{
  segments: PathSegment[];
  setSegments: (s: PathSegment[]) => void;
} | null>(null);

export function PathSegmentsProvider({ children }: { children: ReactNode }) {
  const [segments, setSegments] = useState<PathSegment[]>([]);
  const value = useMemo(() => ({ segments, setSegments }), [segments]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

// Each route page calls this with its own breadcrumb so the shared TopBar
// (rendered once by ProjectLayout) always reflects the active page - avoids
// prop-drilling breadcrumb state down through <Outlet/>.
export function usePathSegments(segments: PathSegment[]) {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("usePathSegments must be used within PathSegmentsProvider");
  const key = JSON.stringify(segments);
  useEffect(() => {
    ctx.setSegments(segments);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

export function useSegments(): PathSegment[] {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSegments must be used within PathSegmentsProvider");
  return ctx.segments;
}
