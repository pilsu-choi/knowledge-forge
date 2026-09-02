import { typeColorVar } from "../lib/typeMeta";

function initials(type: string): string {
  return type
    .split("_")
    .map((p) => p[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function TypeHallmark({ type, size = "md" }: { type: string; size?: "sm" | "md" }) {
  return (
    <span
      className={`hallmark ${size === "sm" ? "sm" : ""}`}
      style={{ background: typeColorVar(type) }}
      title={type}
    >
      {initials(type)}
    </span>
  );
}

export function TypeBadgePill({ type }: { type: string }) {
  return (
    <span className="badge" style={{ background: typeColorVar(type) }}>
      {type}
    </span>
  );
}
