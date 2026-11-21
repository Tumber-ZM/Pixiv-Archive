import { useEffect, useState } from "react";

import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi, type ScanResult } from "../../stores/ui";
import { Dialog } from "../dialog/Dialog";

/**
 * 扫描确认框。
 *
 * 目录只在设置里维护，这里只确认「用这个目录扫」。包含子目录、生成缩略图
 * 都是固定行为（旧项目的默认做法），不再给用户勾选项。
 */
export function ScanDialog() {
  const open = useUi((state) => state.scanOpen);
  const scanTask = useUi((state) => state.scanTask);
  const setScanOpen = useUi((state) => state.setScanOpen);
  const setScanTask = useUi((state) => state.setScanTask);
  const setScanResult = useUi((state) => state.setScanResult);
  const setSettingsOpen = useUi((state) => state.setSettingsOpen);
  const bumpData = useUi((state) => state.bumpData);

  const [root, setRoot] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 每次打开重新读，避免显示被设置页改过之前的旧值
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    call<string | null>(CMD.defaultScanRoot)
      .then((value) => {
        if (!cancelled) setRoot(value);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(isAppError(reason) ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const start = async () => {
    if (!root) {
      setError("还没有设置图片目录，请先在设置里选择");
      return;
    }
    if (scanTask) {
      setError("已有扫描任务在运行");
      return;
    }

    setError(null);
    setScanTask({ current: 0, total: 0, imported: 0, message: "准备中…", kind: "scan" });
    setScanOpen(false);

    try {
      const report = await call<ScanResult>(CMD.scanDirectory, {
        path: root,
        recursive: true,
        generateThumbnails: true,
      });
      setScanTask(null);
      // 汇总交给结果面板，避免这里用 error 通道传递成功信息
      setScanResult(report);
      // 新入库的作品要让画廊立即重取
      bumpData();
    } catch (reason: unknown) {
      setScanTask(null);
      setError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  return (
    <Dialog
      open={open}
      title="开始扫描"
      confirmLabel="开始扫描"
      busy={loading}
      onCancel={() => {
        setScanOpen(false);
        setError(null);
      }}
      onConfirm={start}
    >
      <p className="text-body text-text">要从下面的目录扫描图片吗？</p>
      <p className="mt-3 break-all rounded-btn border border-line bg-surface-2 px-3 py-2 text-micro text-text-2">
        {loading ? "读取中…" : root ?? "还没有设置图片目录"}
      </p>

      <div className="mt-3 flex items-center gap-2 text-micro text-text-3">
        <span>目录在设置里维护</span>
        <button
          type="button"
          className="quiet-link"
          onClick={() => {
            setScanOpen(false);
            setError(null);
            setSettingsOpen(true);
          }}
        >
          打开设置
        </button>
      </div>

      {error && <p className="mt-2 text-micro text-danger">{error}</p>}
    </Dialog>
  );
}