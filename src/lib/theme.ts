import type { Theme } from "../stores/ui";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function prefersDark(): boolean {
  return window.matchMedia(DARK_QUERY).matches;
}

export function resolveTheme(theme: Theme): "light" | "dark" {
  if (theme === "dark") return "dark";
  if (theme === "system") return prefersDark() ? "dark" : "light";
  return "light";
}

/**
 * 写到 `<html data-theme>`。tokens.css 用 `:root` 做浅色、
 * `[data-theme="dark"]` 做墨纸深色，所以浅色时也要显式写成 light，
 * 否则深色切浅色会残留 dark。
 */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = resolveTheme(theme);
}

/** `system` 模式下跟随系统主题变化，返回取消订阅函数。 */
export function watchSystemTheme(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}