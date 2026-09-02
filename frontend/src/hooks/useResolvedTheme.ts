import { useEffect, useState } from "react";

// Cytoscape draws to <canvas>, so it can't read CSS variables like the rest
// of the app - anything it styles needs the resolved light/dark mode as a JS
// value. Watches the `dark` class on <html> (lib/theme.ts always resolves
// "system" to a real class, see its comment) via MutationObserver rather
// than a shared event bus.
export function useResolvedTheme(): "light" | "dark" {
  const [isDark, setIsDark] = useState(() => document.documentElement.classList.contains("dark"));

  useEffect(() => {
    const update = () => setIsDark(document.documentElement.classList.contains("dark"));
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  return isDark ? "dark" : "light";
}
