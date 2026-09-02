import { useNavigate } from "react-router-dom";

export interface PathSegment {
  label: string;
  to?: string;
}

// The signature nav element: a literal, clickable `okf://` path mirroring
// the project's own philosophy that a human-readable file path *is* the
// search surface (wiki/okf.md) - so the breadcrumb looks like the tree.
export function PathBar({ segments }: { segments: PathSegment[] }) {
  const navigate = useNavigate();
  return (
    <nav className="pathbar" aria-label="경로">
      <span className="scheme">okf://</span>
      {segments.map((seg, i) => (
        <span key={i} style={{ display: "contents" }}>
          {i > 0 && <span className="sep">/</span>}
          {seg.to ? (
            <span
              className="seg"
              role="link"
              tabIndex={0}
              onClick={() => navigate(seg.to as string)}
              onKeyDown={(e) => e.key === "Enter" && navigate(seg.to as string)}
            >
              {seg.label}
            </span>
          ) : (
            <span className="seg current">{seg.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
