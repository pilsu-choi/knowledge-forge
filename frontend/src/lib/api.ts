// Typed wrappers around GET /okf/* (api/routes/okf.py, backed by
// okf/reader.py). Read-only, file-tree-backed - no auth, no mutations.
// Shapes below mirror reader.py's return dicts field-for-field.

const API = "/okf";

export class ApiError extends Error {}

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export interface ManifestEntry {
  id: string;
  type: string;
  path: string;
}

export interface Manifest {
  generated_at?: string;
  pipeline_version?: string;
  resources: ManifestEntry[];
  objects: ManifestEntry[];
  relations: unknown[];
  links: unknown[];
}

export interface ObjectCard {
  id: string;
  path: string;
  title: string | null;
  description: string | null;
  tags: string[];
  status: string | null;
  resource: string | null;
}

export interface GraphDocument {
  id: string;
  source_type: string | null;
  title: string;
  description: string | null;
}

export interface GraphObject {
  id: string;
  type: string;
  title: string;
  parent: string | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string | null;
}

export interface GraphData {
  documents: GraphDocument[];
  objects: GraphObject[];
  edges: GraphEdge[];
}

export interface FileDetail {
  rel_path: string;
  frontmatter: Record<string, unknown>;
  body_html: string;
}

export function listProjects(): Promise<string[]> {
  return fetchJSON(`${API}/projects`);
}

export function getManifest(project: string): Promise<Manifest> {
  return fetchJSON(`${API}/projects/${encodeURIComponent(project)}/manifest`);
}

export function listObjectsByType(project: string, type: string): Promise<ObjectCard[]> {
  return fetchJSON(
    `${API}/projects/${encodeURIComponent(project)}/objects/${encodeURIComponent(type)}`,
  );
}

export function getGraph(project: string): Promise<GraphData> {
  return fetchJSON(`${API}/projects/${encodeURIComponent(project)}/graph`);
}

export function getFile(project: string, relPath: string): Promise<FileDetail> {
  return fetchJSON(
    `${API}/projects/${encodeURIComponent(project)}/file/${encodeURIComponent(relPath)}`,
  );
}

export function typeCounts(manifest: Manifest): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const o of manifest.objects) counts[o.type] = (counts[o.type] || 0) + 1;
  return counts;
}
