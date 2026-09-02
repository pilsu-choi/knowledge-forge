import cytoscape, { type Core, type ElementDefinition, type StylesheetJsonBlock } from "cytoscape";
import cytoscapeExpandCollapse from "cytoscape-expand-collapse";
import { ZoomIn, ZoomOut, Maximize, X } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import * as api from "@/lib/api";
import type { GraphData, GraphDocument, GraphObject } from "@/lib/api";
import { GRAPH_DARK, GRAPH_LIGHT, graphDocColor, graphNodeColor, type GraphPalette } from "@/lib/graphPalette";
import { truncate, typeColorVar } from "@/lib/typeMeta";
import { useResolvedTheme } from "@/hooks/useResolvedTheme";
import { TypeDot } from "@/components/TypeBadge";
import { FrontmatterPanel } from "@/components/FrontmatterPanel";
import { StateMessage } from "@/components/StateMessage";
import { Button } from "@/components/ui/button";

let extensionRegistered = false;

function graphStyle(p: GraphPalette): StylesheetJsonBlock[] {
  return [
    {
      selector: "node.doc",
      style: {
        shape: "round-rectangle",
        "background-opacity": 0.1,
        "background-color": (ele: cytoscape.NodeSingular) => graphDocColor(p, ele.data("source_type")),
        "border-width": 1.5,
        "border-color": (ele: cytoscape.NodeSingular) => graphDocColor(p, ele.data("source_type")),
        label: "data(label)",
        "text-valign": "top",
        "text-halign": "center",
        "text-margin-y": -10,
        "text-wrap": "wrap",
        "text-max-width": "100px",
        "font-family": "Inter, sans-serif",
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
        width: 9,
        height: 9,
        "background-color": (ele: cytoscape.NodeSingular) => graphNodeColor(p, ele.data("type")),
        "border-width": 0,
        label: "data(label)",
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 4,
        "font-family": "Inter, sans-serif",
        "font-size": 8,
        color: p.muted,
        "text-wrap": "wrap",
        "text-max-width": "64px",
      },
    },
    {
      selector: "edge",
      style: {
        width: 1,
        "line-color": p.edge,
        "curve-style": "bezier",
        "target-arrow-shape": "none",
        opacity: 0.6,
        label: "data(type)",
        "font-family": "ui-monospace, monospace",
        "font-size": 7,
        color: p.muted,
        "text-background-color": p.bg,
        "text-background-opacity": 0.85,
        "text-background-padding": "2px",
      },
    },
    { selector: ".dim", style: { opacity: 0.1 } },
    {
      selector: "node.selected",
      style: { "border-width": 2, "border-color": p.accent, "background-opacity": 1, "border-opacity": 1 },
    },
    { selector: "node.cy-expand-collapse-collapsed-node", style: { "background-opacity": 0.22 } },
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
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" onClick={() => ecApiRef.current?.collapseAll()}>
          모두 접기
        </Button>
        <Button variant="outline" size="sm" onClick={() => ecApiRef.current?.expandAll()}>
          모두 펼치기
        </Button>
        <div className="flex gap-1">
          <Button variant="ghost" size="icon" title="축소" onClick={() => zoomBy(1 / ZOOM_FACTOR)}>
            <ZoomOut size={15} />
          </Button>
          <Button variant="ghost" size="icon" title="확대" onClick={() => zoomBy(ZOOM_FACTOR)}>
            <ZoomIn size={15} />
          </Button>
          <Button variant="ghost" size="icon" title="전체 보기" onClick={fitAll}>
            <Maximize size={15} />
          </Button>
        </div>
        <div className="flex flex-wrap gap-3 font-mono text-xs text-muted-foreground">
          {typesPresent.map((t) => (
            <span key={t} className="flex items-center gap-1.5">
              <span className="inline-block size-2 rounded-full" style={{ background: typeColorVar(t) }} />
              {t}
            </span>
          ))}
        </div>
      </div>
      <div className="graph-wrap h-[min(72vh,660px)] rounded-xl border border-border">
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
        <div
          // z-[1000]: cytoscape-expand-collapse renders its cue/fisheye
          // overlay canvas at z-index:999 (hardcoded by the plugin) -
          // without a higher z-index here that canvas sits on top of the
          // drawer and silently eats every click, wheel, and even the close
          // button while the drawer is open.
          className={`absolute top-0 right-0 bottom-0 z-[1000] flex w-[360px] flex-col border-l border-border bg-popover shadow-2xl transition-transform duration-200 ${
            selection ? "translate-x-0" : "translate-x-full"
          }`}
        >
          <div className="flex items-start gap-2 border-b border-border px-4 py-3">
            {selection?.type && <TypeDot type={selection.type} className="mt-1.5" />}
            <h4 className="text-sm leading-snug font-semibold">{selection?.title}</h4>
            <Button variant="ghost" size="icon" className="ml-auto -mt-1 -mr-1 size-6" onClick={() => setSelection(null)}>
              <X className="size-3.5" />
            </Button>
          </div>
          {/* min-h-0: flex children default to min-height:auto, which lets
              content overflow past the drawer's bounds instead of scrolling
              inside it. */}
          <div className="min-h-0 flex-1 overflow-y-auto p-4 text-sm">
            {detailQuery.isLoading && <StateMessage>불러오는 중…</StateMessage>}
            {detailQuery.error && <StateMessage>불러오기 실패: {(detailQuery.error as Error).message}</StateMessage>}
            {detailQuery.data && <FrontmatterPanel frontmatter={detailQuery.data.frontmatter} compact />}
            {selection && (
              <button
                type="button"
                className="text-sm text-primary hover:underline"
                onClick={() => navigate(`/${encodeURIComponent(project)}/f/${selection.id}`)}
              >
                본문 전체 보기 →
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
