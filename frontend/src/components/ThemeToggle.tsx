import { useEffect, useState } from "react";
import { Moon, Sun, SunMoon } from "lucide-react";
import { applyTheme, getStoredTheme, type Theme } from "../lib/theme";

const CYCLE: Theme[] = ["system", "dark", "light"];
const ICON: Record<Theme, typeof Sun> = { system: SunMoon, dark: Moon, light: Sun };
const LABEL: Record<Theme, string> = { system: "시스템 설정 따름", dark: "다크 모드", light: "라이트 모드" };

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const Icon = ICON[theme];
  return (
    <button
      type="button"
      className="icon-btn"
      title={`테마: ${LABEL[theme]} (클릭해 전환)`}
      onClick={() => setTheme(CYCLE[(CYCLE.indexOf(theme) + 1) % CYCLE.length])}
    >
      <Icon size={15} />
    </button>
  );
}
