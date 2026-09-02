// cytoscape-expand-collapse ships no types at all. This file must stay a
// global script (no top-level import/export) - a `declare module "x"` block
// inside a file that itself has imports is treated as *augmenting* an
// existing module named "x" rather than declaring a new one, which is a
// no-op for an untyped package (see cytoscape-core-augment.d.ts for the
// augmentation case, where the target module already has real types).
declare module "cytoscape-expand-collapse" {
  const registerExpandCollapse: (cy: unknown) => void;
  export default registerExpandCollapse;
}
