// KnowledgeObject types (wiki/knowledge-object-schema.md) and Source
// document types (collectors/saas) - kept as a flat lookup so new types
// degrade gracefully (fall back to --muted) instead of crashing.

export const OBJECT_TYPES = [
  "entity",
  "fact",
  "concept",
  "decision",
  "event",
  "issue",
  "action_item",
  "policy",
  "requirement",
] as const;

export function typeColorVar(type: string): string {
  return `var(--t-${type}, var(--muted))`;
}

export function docColorVar(sourceType: string | null | undefined): string {
  const key = sourceType && ["slack", "gmail"].includes(sourceType) ? sourceType : "other";
  return `var(--doc-${key})`;
}

export function truncate(label: string, max: number): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}
