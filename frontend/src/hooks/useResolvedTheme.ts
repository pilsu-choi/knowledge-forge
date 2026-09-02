import { useEffect, useState } from "react";
import { resolvedTheme, getStoredTheme } from "../lib/theme";

// Cytoscape draws to <canvas>, so it can't read CSS var() like the rest of
// the app - anything it styles needs the resolved light/dark mode as a JS
// value. Watches both the OS preference and the `data-theme` attribute
// ThemeToggle sets, via MutationObserver rather than a shared event bus.
export function useResolvedTheme(): "light" | "dark" {
  const [theme, setTheme] = useState<"light" | "dark">(() => resolvedTheme(getStoredTheme()));

  useEffect(() => {
    const update = () => setTheme(resolvedTheme(getStoredTheme()));
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    mql.addEventListener("change", update);
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      mql.removeEventListener("change", update);
      observer.disconnect();
    };
  }, []);

  return theme;
}
