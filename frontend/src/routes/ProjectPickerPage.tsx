import { Navigate } from "react-router-dom";
import { useProjects } from "@/hooks/useOkf";

export function ProjectPickerPage() {
  const { data: projects, isLoading, error } = useProjects();

  if (isLoading) return null;

  if (error) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 px-6 text-center">
        <div className="text-sm text-muted-foreground">초기화 실패: {(error as Error).message}</div>
        <div className="text-xs text-muted-foreground">KF_OKF_DIR 환경 변수를 확인하세요.</div>
      </div>
    );
  }

  if (projects && projects.length > 0) {
    return <Navigate to={`/${encodeURIComponent(projects[0])}`} replace />;
  }

  return (
    <div className="flex h-screen flex-col items-center justify-center gap-2 px-6 text-center">
      <div className="text-base font-semibold">아직 OKF 프로젝트가 없습니다</div>
      <div className="max-w-md text-sm text-muted-foreground">
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">knowledge-forge extract --okf-dir &lt;dir&gt;</code> 또는{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">knowledge-forge schedule --okf-dir &lt;dir&gt;</code>로
        트리를 만든 뒤, 그 경로를 <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">KF_OKF_DIR</code>로 지정해
        서버를 다시 띄우세요.
      </div>
    </div>
  );
}
