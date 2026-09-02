import { NavLink } from "react-router-dom";
import type { Manifest } from "../lib/api";
import { typeCounts } from "../lib/api";
import { TypeHallmark } from "./TypeBadge";

export function Rail({ project, manifest }: { project: string; manifest: Manifest }) {
  const counts = typeCounts(manifest);
  const resCount = manifest.resources.length;
  const orphanCount = manifest.relations.length + manifest.links.length;
  const base = `/${encodeURIComponent(project)}`;

  return (
    <aside className="rail">
      <div>
        <h2>구성</h2>
        <div className="stat-row">
          <div className="stat">
            <div className="n">{resCount}</div>
            <div className="l">resources</div>
          </div>
          <div className="stat">
            <div className="n">{manifest.objects.length}</div>
            <div className="l">objects</div>
          </div>
          <div className="stat">
            <div className="n">{orphanCount}</div>
            <div className="l">orphan edges</div>
          </div>
        </div>
      </div>

      <div>
        <h2>Objects</h2>
        <div className="nav-list">
          {Object.keys(counts)
            .sort()
            .map((type) => (
              <NavLink
                key={type}
                to={`${base}/type/${encodeURIComponent(type)}`}
                className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
              >
                <TypeHallmark type={type} size="sm" />
                {type}
                <span className="cnt">{counts[type]}</span>
              </NavLink>
            ))}
        </div>
      </div>

      <div>
        <h2>기타</h2>
        <div className="nav-list">
          <NavLink to={`${base}/graph`} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
            그래프
          </NavLink>
          <NavLink to={`${base}/resources`} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
            resources
            <span className="cnt">{resCount}</span>
          </NavLink>
          <NavLink
            to={`${base}/f/views/by-topic.md`}
            className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
          >
            주제별 보기
          </NavLink>
          <NavLink to={`${base}/f/log.md`} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
            변경 이력 (log)
          </NavLink>
          <NavLink to={`${base}/f/index.md`} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
            index.md
          </NavLink>
        </div>
      </div>
    </aside>
  );
}
