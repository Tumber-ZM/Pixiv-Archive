import { useEffect } from "react";
import { listen } from "@tauri-apps/api/event";

import { TaskProgress } from "./components/scan/TaskProgress";
import { UpdateToast } from "./components/update/UpdateToast";
import { ScanDialog } from "./components/scan/ScanDialog";
import { ScanResultDialog } from "./components/scan/ScanResultDialog";
import { AppShell } from "./components/layout/AppShell";
import { applyTheme, watchSystemTheme } from "./lib/theme";
import { useUi, type ScanTask } from "./stores/ui";

export default function App() {
  const theme = useUi((state) => state.theme);
  const safeMode = useUi((state) => state.safeMode);
  const setScanTask = useUi((state) => state.setScanTask);

  useEffect(() => {
    applyTheme(theme);
    if (theme !== "system") return undefined;
    return watchSystemTheme(() => applyTheme("system"));
  }, [theme]);

  // 安全模式挂到 <html>，让所有 img（卡片缩略图、查看器大图）统一模糊
  useEffect(() => {
    document.documentElement.dataset.safeMode = safeMode ? "true" : "false";
  }, [safeMode]);

  // 扫描 / 同步进度都由后端推事件，这里只负责落到 store
  useEffect(() => {
    let disposed = false;
    const stops: Array<() => void> = [];

    const track = (promise: Promise<() => void>) => {
      promise
        .then((stop) => {
          if (disposed) stop();
          else stops.push(stop);
        })
        .catch(() => undefined);
    };

    track(
      listen<ScanTask>("scan-progress", (event) => {
        setScanTask({ ...event.payload, kind: "scan" });
      }),
    );

    // 同步进度复用同一个浮窗：字段名对齐 ScanTask
    track(
      listen<{ current: number; total: number; synced: number; message: string }>(
        "sync-progress",
        (event) => {
          const payload = event.payload;
          setScanTask({
            current: payload.current,
            total: payload.total,
            imported: payload.synced,
            message: payload.message,
            kind: "sync",
          });
        },
      ),
    );

    // 订阅检查进度
    track(
      listen<{ current: number; total: number; message: string }>(
        "sub-progress",
        (event) => {
          const payload = event.payload;
          setScanTask({
            current: payload.current,
            total: payload.total,
            imported: 0,
            message: payload.message,
            kind: "subs",
          });
        },
      ),
    );

    // 同步我的 Pixiv 收藏（只读作品 ID）
    track(
      listen<{ current: number; total: number; message: string }>(
        "favorites-progress",
        (event) => {
          const payload = event.payload;
          setScanTask({
            current: payload.current,
            total: payload.total,
            imported: 0,
            message: payload.message,
            kind: "favorites",
          });
        },
      ),
    );

    // 校验缺页并补下
    track(
      listen<{ current: number; total: number; message: string }>(
        "repair-progress",
        (event) => {
          const payload = event.payload;
          setScanTask({
            current: payload.current,
            total: payload.total,
            imported: 0,
            message: payload.message,
            kind: "repair",
          });
        },
      ),
    );

    return () => {
      disposed = true;
      stops.splice(0).forEach((stop) => stop());
    };
  }, [setScanTask]);

  return (
    <>
      <AppShell />
      <ScanDialog />
      <ScanResultDialog />
      <TaskProgress />
      <UpdateToast />
    </>
  );
}