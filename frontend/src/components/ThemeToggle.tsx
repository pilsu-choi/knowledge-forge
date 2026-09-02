import { useEffect, useState } from "react";
import { Moon, Sun, SunMoon } from "lucide-react";
import { applyTheme, getStoredTheme, type Theme } from "@/lib/theme";
import { Button } from "@/components/ui/button";

const CYCLE: Theme[] = ["system", "dark", "light"];
const ICON: Record<Theme, typeof Sun> = { system: SunMoon, dark: Moon, light: Sun };
const LABEL: Record<Theme, string> = { system: "시스템 설정 따름", dark: "다크 모드", light: "라이트 모드" };

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (theme === "system") applyTheme("system");
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [theme]);

  const Icon = ICON[theme];
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      title={`테마: ${LABEL[theme]} (클릭해 전환)`}
      onClick={() => setTheme(CYCLE[(CYCLE.indexOf(theme) + 1) % CYCLE.length])}
    >
      <Icon size={15} />
    </Button>
  );
}
