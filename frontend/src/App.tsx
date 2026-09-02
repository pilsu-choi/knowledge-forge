import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import { PathSegmentsProvider } from "@/lib/pathSegments";
import { StateMessage } from "@/components/StateMessage";
import { ProjectPickerPage } from "./routes/ProjectPickerPage";
import { ProjectLayout } from "./routes/ProjectLayout";
import { OverviewPage } from "./routes/OverviewPage";
import { TypeListPage } from "./routes/TypeListPage";
import { ResourcesPage } from "./routes/ResourcesPage";
import { FileDetailPage } from "./routes/FileDetailPage";

// cytoscape + react-cytoscapejs + the expand-collapse plugin are the single
// heaviest dependency in this app - split out so opening a markdown-only
// session never pays for them.
const GraphPage = lazy(() => import("./routes/GraphPage").then((m) => ({ default: m.GraphPage })));

export default function App() {
  return (
    <PathSegmentsProvider>
      <Routes>
        <Route path="/" element={<ProjectPickerPage />} />
        <Route path="/:project" element={<ProjectLayout />}>
          <Route index element={<OverviewPage />} />
          <Route path="type/:type" element={<TypeListPage />} />
          <Route path="resources" element={<ResourcesPage />} />
          <Route
            path="graph"
            element={
              <Suspense fallback={<StateMessage>그래프 뷰 불러오는 중…</StateMessage>}>
                <GraphPage />
              </Suspense>
            }
          />
          <Route path="f/*" element={<FileDetailPage />} />
        </Route>
      </Routes>
    </PathSegmentsProvider>
  );
}
