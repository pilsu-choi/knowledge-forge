import { useQuery } from "@tanstack/react-query";
import * as api from "@/lib/api";

export function useProjects() {
  return useQuery({ queryKey: ["projects"], queryFn: api.listProjects });
}

export function useManifest(project: string | undefined) {
  return useQuery({
    queryKey: ["manifest", project],
    queryFn: () => api.getManifest(project as string),
    enabled: !!project,
  });
}

export function useObjectsByType(project: string | undefined, type: string | undefined) {
  return useQuery({
    queryKey: ["objects", project, type],
    queryFn: () => api.listObjectsByType(project as string, type as string),
    enabled: !!project && !!type,
  });
}

export function useGraph(project: string | undefined) {
  return useQuery({
    queryKey: ["graph", project],
    queryFn: () => api.getGraph(project as string),
    enabled: !!project,
  });
}

export function useFile(project: string | undefined, relPath: string | undefined) {
  return useQuery({
    queryKey: ["file", project, relPath],
    queryFn: () => api.getFile(project as string, relPath as string),
    enabled: !!project && !!relPath,
  });
}
