import { cn } from "@/lib/utils";

function renderValue(v: unknown) {
  if (v === null || v === undefined || v === "") return <span className="text-muted-foreground/60">—</span>;
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x === "string")) {
      return (
        <div className="flex flex-wrap gap-1">
          {v.map((x, i) => (
            <span key={i} className="rounded-full bg-muted px-2 py-0.5 font-mono text-[0.7rem] text-muted-foreground">
              {x}
            </span>
          ))}
        </div>
      );
    }
    return <pre className="font-mono text-xs whitespace-pre-wrap break-all">{JSON.stringify(v, null, 2)}</pre>;
  }
  if (typeof v === "object") return <pre className="font-mono text-xs whitespace-pre-wrap break-all">{JSON.stringify(v, null, 2)}</pre>;
  // Long unbroken strings (URLs, hashes, ids) otherwise overflow the panel
  // instead of wrapping, most visibly in the graph drawer's narrow column.
  return <span className="break-all">{String(v)}</span>;
}

// Obsidian renders YAML frontmatter as a compact "Properties" block directly
// above the note body - this mirrors that. Also reused in the graph drawer
// (a ~300px sidebar), where the label-left/value-right layout below doesn't
// fit - `compact` stacks the label above the value instead, since a fixed
// label column has no width left to give the value column at that size.
export function FrontmatterPanel({
  frontmatter,
  compact = false,
}: {
  frontmatter: Record<string, unknown>;
  compact?: boolean;
}) {
  const entries = Object.entries(frontmatter);
  if (!entries.length) return null;
  return (
    <div className={cn("rounded-lg border border-border bg-card/50 px-4 py-2 text-sm", !compact && "mb-6")}>
      {entries.map(([k, v], i) => (
        <div key={k}>
          <div className={compact ? "flex flex-col gap-1 py-2" : "flex items-start gap-4 py-1.5"}>
            <div
              className={cn(
                "flex-none pt-px font-mono text-[0.7rem] tracking-wide text-muted-foreground uppercase",
                !compact && "w-32",
              )}
            >
              {k}
            </div>
            <div className="min-w-0 flex-1">{renderValue(v)}</div>
          </div>
          {i < entries.length - 1 && <div className="h-px bg-border/70" />}
        </div>
      ))}
    </div>
  );
}
