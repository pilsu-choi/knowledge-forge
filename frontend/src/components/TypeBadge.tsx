import { typeColorVar } from "@/lib/typeMeta";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function TypeDot({ type, className }: { type: string; className?: string }) {
  return (
    <span
      className={cn("inline-block size-2 shrink-0 rounded-full", className)}
      style={{ background: typeColorVar(type) }}
      title={type}
    />
  );
}

export function TypeBadgePill({ type }: { type: string }) {
  return (
    <Badge variant="outline" className="gap-1.5 font-mono font-normal">
      <TypeDot type={type} />
      {type}
    </Badge>
  );
}
