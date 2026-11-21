import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { CMD, call } from "../../lib/ipc";
import { useUi } from "../../stores/ui";

const TITLES: Record<string, string> = {
  scan: "正在扫描",
  sync: "同步元数据",
  subs: "更新订阅",
};

/**
 * 长任务进度浮窗，固定在右下角。
 *
 * 扫描 / 同步元数据 / 订阅下载共用这个浮窗，关闭按钮统一调 cancel_scan
 * （后端 `TaskCancel` 对三类任务都生效）。点击后先显示「正在取消…」，
 * 等任务真的退出、事件停止后才消失。
 */
export function TaskProgress() {
  const task = useUi((state) => state.scanTask);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    setCancelling(false);
  }, [task]);

  if (!task) return null;

  const percent = task.total > 0 ? Math.round((task.current / task.total) * 100) : 0;
  const title = cancelling ? "正在取消…" : TITLES[task.kind ?? "scan"] ?? "处理中";

  const cancel = async () => {
    setCancelling(true);
    try {
      await call<boolean>(CMD.cancelScan);
    } catch {
      // 取消失败也不该把浮窗卡住，事件停止后它自然会消失
    }
  };

  return (
    <div className="fixed bottom-6 right-6 z-50 w-[320px] rounded-card border border-line bg-surface p-4">
      <div className="flex items-center gap-2">
        <span className="text-body text-text">{title}</span>
        <button
          type="button"
          className="icon-btn ml-auto"
          onClick={cancel}
          disabled={cancelling}
          title={cancelling ? "正在取消…" : "取消任务"}
          aria-label="取消任务"
        >
          <X size={14} />
        </button>
      </div>

      <div className="mt-3 h-[2px] w-full overflow-hidden bg-surface-2">
        <div
          className="h-full bg-accent transition-[width] duration-500 ease-pa"
          style={{ width: `${percent}%` }}
        />
      </div>

      <p className="mt-2 text-micro text-text-2">
        {task.current} / {task.total || "?"} · 已导入 {task.imported}
      </p>
      <p className="mt-0.5 truncate-1 text-micro text-text-3">{task.message}</p>
    </div>
  );
}