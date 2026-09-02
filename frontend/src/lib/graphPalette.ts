// Cytoscape renders to <canvas>, so it can't read the CSS custom properties
// in styles/index.css - these mirror those hex values by hand. Keep the two
// in sync when the token palette changes.

export interface GraphPalette {
  ink: string;
  muted: string;
  surface: string;
  bg: string;
  edge: string;
  accent: string;
  types: Record<string, string>;
  docs: Record<string, string>;
}

export const GRAPH_LIGHT: GraphPalette = {
  ink: "#2e3338",
  muted: "#6c6f76",
  surface: "#ffffff",
  bg: "#ffffff",
  edge: "#d8d5cf",
  accent: "#7c5cdb",
  types: {
    entity: "#4d78c9",
    fact: "#2f9b8f",
    concept: "#8a5cd6",
    decision: "#3f9a5c",
    event: "#b8862f",
    issue: "#c2564a",
    action_item: "#7a8a3a",
    policy: "#b0509a",
    requirement: "#2f9a7a",
  },
  docs: { slack: "#b8632f", gmail: "#9a6a3a", other: "#8a8478" },
};

export const GRAPH_DARK: GraphPalette = {
  ink: "#dcddde",
  muted: "#999999",
  surface: "#202020",
  bg: "#1e1e1e",
  edge: "#33333a",
  accent: "#8b6cf0",
  types: {
    entity: "#7fa8f0",
    fact: "#5fc9bb",
    concept: "#b79aef",
    decision: "#7fca94",
    event: "#e0b463",
    issue: "#e58a7e",
    action_item: "#b3c273",
    policy: "#e08cca",
    requirement: "#6fd6ae",
  },
  docs: { slack: "#e0975c", gmail: "#cf9d6e", other: "#999488" },
};

export function graphNodeColor(p: GraphPalette, type: string): string {
  return p.types[type] || p.muted;
}

export function graphDocColor(p: GraphPalette, sourceType: string | null | undefined): string {
  return (sourceType && p.docs[sourceType]) || p.docs.other;
}
