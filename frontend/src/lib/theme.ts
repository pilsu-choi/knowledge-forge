export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "kf-theme";

export function getStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // localStorage unavailable (private mode, etc.) - fall through to system
  }
  return "system";
}

export function resolvedTheme(theme: Theme): "light" | "dark" {
  if (theme !== "system") return theme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// Tailwind's `dark:` variant here is class-based (`@custom-variant dark
// (&:is(.dark *))` in styles/index.css), so "system" is resolved once in JS
// and applied as a real class - not left to a CSS media query - which is
// also why callers need to re-run this on a matchMedia change (ThemeToggle).
export function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", resolvedTheme(theme) === "dark");
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // ignore - theme just won't persist across reloads
  }
}
