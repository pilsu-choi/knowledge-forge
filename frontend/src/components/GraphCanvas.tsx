import cytoscape, { type Core, type ElementDefinition, type StylesheetJsonBlock } from "cytoscape";
import cytoscapeExpandCollapse from "cytoscape-expand-collapse";
import { ZoomIn, ZoomOut, Maximize } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import * as api from "../lib/api";
import type { GraphData, GraphDocument, GraphObject } from "../lib/api";
import { GRAPH_DARK, GRAPH_LIGHT, graphDocColor, graphNodeColor, type GraphPalette } from "../lib/graphPalette";
import { truncate, typeColorVar } from "../lib/typeMeta";
import { useResolvedTheme } from "../hooks/useResolvedTheme";
import { TypeHallmark } from "./TypeBadge";
import { FrontmatterPanel } from "./FrontmatterPanel";

let extensionRegistered = false;

function graphStyle(p: GraphPalette): StylesheetJsonBlock[] {
  return [
    {
      selector: "node.doc",
      style: {
        shape: "round-rectangle",
        "background-opacity": 0.12,
        "background-color": (ele: cytoscape.NodeSingular) => graphDocColor(p, ele.data("source_type")),
        "border-width": 2,
        "border-color": (ele: cytoscape.NodeSingular) => graphDocColor(p, ele.data("source_type")),
        label: "data(label)",
        "text-valign": "top",
        "text-halign": "center",
        "text-margin-y": -10,
        "text-wrap": "wrap",
        "text-max-width": "100px",
        "font-family": '"IBM Plex Sans", sans-serif',
        "font-size": 10,
        "font-weight": 600,
        color: p.ink,
        padding: "22px",
        "compound-sizing-wrt-labels": "include",
      },
    },
    {
      selector: "node.obj",
      style: {
        shape: "ellipse",
        width: 22,
        height: 22,
        "background-color": (ele: cytoscape.NodeSingular) => graphNodeColor(p, ele.data("type")),
        "border-width": 2,
        "border-color": p.surface,
        label: "data(label)",
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 5,
        "font-family": '"IBM Plex Sans", sans-serif',
        "font-size": 8.5,
        color: p.ink,
        "text-wrap": "wrap",
        "text-max-width": "64px",
      },
    },
    {
      selector: "edge",
      style: {
        width: 1.4,
        "line-color": p.edge,
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "target-arrow-color": p.edge,
        "arrow-scale": 0.7,
        label: "data(type)",
        "font-family": '"IBM Plex Mono", monospace',
        "font-size": 7.5,
        color: p.muted,
        "text-background-color": p.bg,
        "text-background-opacity": 0.85,
        "text-background-padding": "2px",
      },
    },
    { selector: ".dim", style: { opacity: 0.12 } },
    { selector: "node.selected", style: { "border-width": 3, "border-color": p.ember } },
    { selector: "node.cy-expand-collapse-collapsed-node", style: { "background-opacity": 0.26 } },
  ];
}

interface Selection {
  isDoc: boolean;
  id: string;
  title: string;
  type: string | null;
}

const ZOOM_FACTOR = 1.4;
const ZOOM_ANIM_MS = 160;
const FOCUS_ANIM_MS = 350;

export function GraphCanvas({ project, data }: { project: string; data: GraphData }) {
  const navigate = useNavigate();
  const theme = useResolvedTheme();
  const palette = theme === "dark" ? GRAPH_DARK : GRAPH_LIGHT;
  const cyRef = useRef<Core | null>(null);
  const ecApiRef = useRef<ReturnType<Core["expandCollapse"]> | null>(null);
  const initializedRef = useRef(false);
  const [selection, setSelection] = useState<Selection | null>(null);

  const detailQuery = useQuery({
    queryKey: ["file", project, selection?.id],
    queryFn: () => api.getFile(project, selection!.id),
    enabled: !!selection,
  });

  const objById = useMemo(() => Object.fromEntries(data.objects.map((o) => [o.id, o])), [data.objects]);
  const docById = useMemo(() => Object.fromEntries(data.documents.map((d) => [d.id, d])), [data.documents]);

  const elements: ElementDefinition[] = useMemo(() => {
    const docLabel = (d: GraphDocument) => truncate(d.title || d.id, 20);
    const objLabel = (o: GraphObject) => truncate(o.title, 22);
    return [
      ...data.documents.map((d) => ({
        data: { id: d.id, label: docLabel(d), source_type: d.source_type },
        classes: "doc",
      })),
      ...data.objects.map((o) => ({
        data: {
          id: o.id,
          label: objLabel(o),
          type: o.type,
          ...(o.parent && docById[o.parent] ? { parent: o.parent } : {}),
        },
        classes: "obj",
      })),
      ...data.edges
        .filter((e) => (objById[e.source] || docById[e.source]) && (objById[e.target] || docById[e.target]))
        .map((e) => ({ data: { id: e.id, source: e.source, target: e.target, type: e.type ?? "" }, classes: "relation" })),
    ];
  }, [data, objById, docById]);

  const typesPresent = useMemo(() => [...new Set(data.objects.map((o) => o.type))].sort(), [data.objects]);

  const onCy = useCallback(
    (cy: Core) => {
      cyRef.current = cy;
      if (initializedRef.current) return;
      initializedRef.current = true;

      if (!extensionRegistered) {
        cytoscape.use(cytoscapeExpandCollapse);
        extensionRegistered = true;
      }

      // `layoutBy` is tuned for the *collapsed* document boxes (large), not
      // the small object circles inside them - otherwise collapsed docs end
      // up stacked on each other on every collapseAll()/expandAll().
      const ecApi = cy.expandCollapse({
        layoutBy: {
          name: "cose",
          padding: 36,
          nodeDimensionsIncludeLabels: true,
          animate: false,
          gravity: 80,
          idealEdgeLength: 260,
          nodeOverlap: 40,
          nodeRepulsion: 600000,
          numIter: 2000,
        } as cytoscape.LayoutOptions,
        fisheye: true,
        animate: true,
        undoable: false,
        cueEnabled: true,
      });
      ecApiRef.current = ecApi;
      ecApi.collapseAll();

      function neighborhoodOf(node: cytoscape.NodeSingular) {
        let scope = node.closedNeighborhood();
        if (node.isParent()) {
          const kids = node.descendants();
          scope = scope.union(kids).union(kids.connectedEdges());
        }
        return scope;
      }
      cy.on("mouseover", "node", (evt) => {
        cy.elements().difference(neighborhoodOf(evt.target)).addClass("dim");
      });
      cy.on("mouseout", "node", () => cy.elements().removeClass("dim"));

      cy.on("tap", "node", (evt) => {
        const node = evt.target;
        if (node.hasClass("cy-expand-collapse-collapsed-node")) return;
        cy.nodes().removeClass("selected");
        node.addClass("selected");
        const isDoc = node.hasClass("doc");
        const record = isDoc ? docById[node.id()] : objById[node.id()];
        if (!record) return;
        setSelection({
          isDoc,
          id: node.id(),
          title: record.title,
          type: isDoc ? null : (record as GraphObject).type,
        });

        // The graph can hold 1000+ nodes - center + zoom into exactly the
        // clicked node's relations instead of leaving it to manual pan/zoom.
        const scope = neighborhoodOf(node);
        cy.animate({ fit: { eles: scope, padding: 90 } }, { duration: FOCUS_ANIM_MS, easing: "ease-out" });
      });
      cy.on("tap", (evt) => {
        if (evt.target === cy) {
          cy.nodes().removeClass("selected");
          cy.elements().removeClass("dim");
          setSelection(null);
        }
      });
    },
    [docById, objById],
  );

  const zoomBy = useCallback((factor: number) => {
    const cy = cyRef.current;
    if (!cy) return;
    const level = Math.min(Math.max(cy.zoom() * factor, cy.minZoom()), cy.maxZoom());
    cy.animate(
      { zoom: { level, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } } },
      { duration: ZOOM_ANIM_MS },
    );
  }, []);

  const fitAll = useCallback(() => {
    cyRef.current?.animate({ fit: { eles: cyRef.current.elements(), padding: 40 } }, { duration: 300 });
  }, []);

  return (
    <div>
      <div className="graph-toolbar">
        <button type="button" className="btn" onClick={() => ecApiRef.current?.collapseAll()}>
          모두 접기
        </button>
        <button type="button" className="btn" onClick={() => ecApiRef.current?.expandAll()}>
          모두 펼치기
        </button>
        <div className="zoom-controls">
          <button type="button" className="icon-btn" title="축소" onClick={() => zoomBy(1 / ZOOM_FACTOR)}>
            <ZoomOut size={15} />
          </button>
          <button type="button" className="icon-btn" title="확대" onClick={() => zoomBy(ZOOM_FACTOR)}>
            <ZoomIn size={15} />
          </button>
          <button type="button" className="icon-btn" title="전체 보기" onClick={fitAll}>
            <Maximize size={15} />
          </button>
        </div>
        <div className="graph-legend">
          {typesPresent.map((t) => (
            <span key={t}>
              <span className="dot" style={{ background: typeColorVar(t) }} />
              {t}
            </span>
          ))}
        </div>
      </div>
      <div className="graph-wrap">
        <CytoscapeComponent
          key={project}
          className="cy-canvas"
          elements={elements}
          stylesheet={graphStyle(palette)}
          layout={{ name: "cose", padding: 36, nodeDimensionsIncludeLabels: true, animate: false, gravity: 200, idealEdgeLength: 80 } as cytoscape.LayoutOptions}
          minZoom={0.2}
          maxZoom={8}
          wheelSensitivity={4}
          cy={onCy}
        />
        <div className={`g-drawer ${selection ? "open" : ""}`}>
          <div className="g-drawer-head">
            {selection?.type && <TypeHallmark type={selection.type} size="sm" />}
            <h4>{selection?.title}</h4>
            <button type="button" className="g-drawer-close" onClick={() => setSelection(null)}>
              ✕
            </button>
          </div>
          <div className="g-drawer-body">
            {detailQuery.isLoading && <div className="empty">불러오는 중…</div>}
            {detailQuery.error && <div className="empty">불러오기 실패: {(detailQuery.error as Error).message}</div>}
            {detailQuery.data && <FrontmatterPanel frontmatter={detailQuery.data.frontmatter} />}
            {selection && (
              <a
                className="detail-link"
                onClick={() => navigate(`/${encodeURIComponent(project)}/f/${selection.id}`)}
              >
                본문 전체 보기 →
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
