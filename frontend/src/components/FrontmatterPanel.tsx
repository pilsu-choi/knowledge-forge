function renderValue(v: unknown) {
  if (v === null || v === undefined || v === "") return <span style={{ color: "var(--muted-2)" }}>—</span>;
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x === "string")) {
      return (
        <div className="chips">
          {v.map((x, i) => (
            <span className="chip" key={i}>
              {x}
            </span>
          ))}
        </div>
      );
    }
    return <pre>{JSON.stringify(v, null, 2)}</pre>;
  }
  if (typeof v === "object") return <pre>{JSON.stringify(v, null, 2)}</pre>;
  return <span>{String(v)}</span>;
}

export function FrontmatterPanel({
  frontmatter,
  sticky = false,
}: {
  frontmatter: Record<string, unknown>;
  sticky?: boolean;
}) {
  const entries = Object.entries(frontmatter);
  if (!entries.length) return null;
  return (
    <div className={`fm-box ${sticky ? "sticky" : ""}`}>
      {entries.map(([k, v]) => (
        <div className="fm-row" key={k}>
          <div className="k">{k}</div>
          <div className="v">{renderValue(v)}</div>
        </div>
      ))}
    </div>
  );
}
