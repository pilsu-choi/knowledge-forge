// Cytoscape renders to <canvas>, so it can't read the CSS custom properties
// in styles/index.css - these mirror those hex values by hand. Keep the two
// in sync when the token palette changes.

export interface GraphPalette {
  ink: string;
  muted: string;
  surface: string;
  bg: string;
  edge: string;
  ember: string;
  types: Record<string, string>;
  docs: Record<string, string>;
}

export const GRAPH_LIGHT: GraphPalette = {
  ink: "#1c1b19",
  muted: "#726d63",
  surface: "#ffffff",
  bg: "#f7f5f1",
  edge: "#cfc8b8",
  ember: "#d1490f",
  types: {
    entity: "#4a5ec4",
    fact: "#1f8fae",
    concept: "#8a53c4",
    decision: "#1f9d74",
    event: "#b9821f",
    issue: "#c4502a",
    action_item: "#748a2a",
    policy: "#b4448f",
    requirement: "#1f9d7a",
  },
  docs: { slack: "#c4551f", gmail: "#9a5a26", other: "#8a8071" },
};

export const GRAPH_DARK: GraphPalette = {
  ink: "#e9e6e0",
  muted: "#9a9ca5",
  surface: "#131519",
  bg: "#0b0c0f",
  edge: "#3a3d47",
  ember: "#ff6a3d",
  types: {
    entity: "#7c8fe0",
    fact: "#5ec8e0",
    concept: "#b28ce0",
    decision: "#4fd1a5",
    event: "#f0b64d",
    issue: "#f2795a",
    action_item: "#a9c95e",
    policy: "#e07ec0",
    requirement: "#7fe0b0",
  },
  docs: { slack: "#ff8a5c", gmail: "#e0975a", other: "#c9a08a" },
};

export function graphNodeColor(p: GraphPalette, type: string): string {
  return p.types[type] || p.muted;
}

export function graphDocColor(p: GraphPalette, sourceType: string | null | undefined): string {
  return (sourceType && p.docs[sourceType]) || p.docs.other;
}
