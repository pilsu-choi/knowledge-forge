import { Navigate } from "react-router-dom";
import { useProjects } from "../hooks/useOkf";

export function ProjectPickerPage() {
  const { data: projects, isLoading, error } = useProjects();

  if (isLoading) return null;

  if (error) {
    return (
      <div className="empty-screen">
        <div>초기화 실패: {(error as Error).message}</div>
        <div className="hint">KF_OKF_DIR 환경 변수를 확인하세요.</div>
      </div>
    );
  }

  if (projects && projects.length > 0) {
    return <Navigate to={`/${encodeURIComponent(projects[0])}`} replace />;
  }

  return (
    <div className="empty-screen">
      <div className="headline">아직 OKF 프로젝트가 없습니다</div>
      <div className="hint">
        <code>knowledge-forge extract --okf-dir &lt;dir&gt;</code> 또는{" "}
        <code>knowledge-forge schedule --okf-dir &lt;dir&gt;</code>로 트리를 만든 뒤,
        그 경로를 <code>KF_OKF_DIR</code>로 지정해 서버를 다시 띄우세요.
      </div>
    </div>
  );
}
