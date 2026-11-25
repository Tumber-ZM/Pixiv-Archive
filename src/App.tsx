import { useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";

import { TaskProgress } from "./components/scan/TaskProgress";
import { UpdateToast } from "./components/update/UpdateToast";
import { ScanDialog } from "./components/scan/ScanDialog";
import { ScanResultDialog } from "./components/scan/ScanResultDialog";
import { Dialog } from "./components/dialog/Dialog";
import { AppShell } from "./components/layout/AppShell";
import { applyTheme, watchSystemTheme } from "./lib/theme";
import { CMD, call } from "./lib/ipc";
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
      listen<{
        current: number;
        total: number;
        imported?: number;
        backlog?: number;
        message: string;
      }>("sub-progress",
        (event) => {
          const payload = event.payload;
          setScanTask({
            current: payload.current,
            total: payload.total,
            imported: payload.imported ?? 0,
            backlog: payload.backlog ?? 0,
            message: payload.message,
            kind: "subs",
          });
        },
      ),
    );

    // 图片校验（补缺页）进度
    track(
      listen<{ current: number; total: number; imported: number; message: string }>(
        "repair-progress",
        (event) => {
          const payload = event.payload;
          setScanTask({
            current: payload.current,
            total: payload.total,
            imported: payload.imported ?? 0,
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

  // 启动迁移提示：旧库被修掉「按内容去重」的索引时，提醒跑一次校验
  const [startupNotice, setStartupNotice] = useState<string | null>(null);
  useEffect(() => {
    void call<string | null>(CMD.takeStartupNotice)
      .then((notice) => {
        if (notice) setStartupNotice(notice);
      })
      .catch(() => undefined);
  }, []);

  return (
    <>
      <AppShell />
      <ScanDialog />
      <ScanResultDialog />
      <TaskProgress />
      <UpdateToast />
      <Dialog
        open={startupNotice !== null}
        title="数据库已升级"
        confirmLabel="知道了"
        onCancel={() => setStartupNotice(null)}
        onConfirm={() => setStartupNotice(null)}
      >
        <p className="text-body text-text-2">{startupNotice}</p>
      </Dialog>
    </>
  );
}