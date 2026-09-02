import type { ReactNode } from "react";

export function StateMessage({ children }: { children: ReactNode }) {
  return <div className="py-5 text-sm text-muted-foreground">{children}</div>;
}
