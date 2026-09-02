// Augments the real `cytoscape` types (module augmentation requires the
// target module to already have its own declarations, which cytoscape does -
// unlike cytoscape-expand-collapse, see cytoscape-expand-collapse.d.ts).
import type { LayoutOptions } from "cytoscape";

declare module "cytoscape" {
  interface Core {
    expandCollapse(options?: {
      layoutBy?: LayoutOptions;
      fisheye?: boolean;
      animate?: boolean;
      undoable?: boolean;
      cueEnabled?: boolean;
    }): {
      collapseAll(): void;
      expandAll(): void;
    };
  }
}
