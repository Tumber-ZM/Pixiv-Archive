import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { CMD, call } from "../../lib/ipc";
import { useUi } from "../../stores/ui";

const TITLES: Record<string, string> = {
  scan: "正在扫描",
  sync: "同步元数据",
  subs: "更新订阅",
  favorites: "同步收藏",
  repair: "校验图片",
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
  const setScanTask = useUi((state) => state.setScanTask);
  const [cancelling, setCancelling] = useState(false);

  // 进度事件会不断重建 task 对象，不能跟着 task 重置取消状态，
  // 否则「正在取消…」会被下一条迟到的事件冲掉。
  useEffect(() => {
    if (!task) setCancelling(false);
  }, [task]);

  // 取消后兜底：后端任务槽全部释放就收起浮窗。发起任务的组件可能没清
  // scanTask（或完成事件丢失），不能让一个已结束的任务把浮窗和后续扫描一直挡住。
  useEffect(() => {
    if (!cancelling) return undefined;
    let disposed = false;
    const timer = window.setInterval(() => {
      void call<boolean>(CMD.scanStatus)
        .then((running) => {
          if (!disposed && !running) setScanTask(null);
        })
        .catch(() => undefined);
    }, 400);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [cancelling, setScanTask]);

  // 完成事件可能比命令返回更晚到，把已经收起的浮窗重新打开。稍后确认后端
  // 是否真的有任务：已结束且期间没有新的进度事件（task 还是同一个对象）就收起。
  useEffect(() => {
    if (!task || cancelling) return undefined;
    const timer = window.setTimeout(() => {
      void call<boolean>(CMD.scanStatus)
        .then((running) => {
          if (running) return;
          if (useUi.getState().scanTask === task) setScanTask(null);
        })
        .catch(() => undefined);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [task, cancelling, setScanTask]);

  if (!task) return null;

  const percent = task.total > 0 ? Math.round((task.current / task.total) * 100) : 0;
  const title = cancelling ? "正在取消…" : TITLES[task.kind ?? "scan"] ?? "处理中";

  const cancel = async () => {
    setCancelling(true);
    try {
      await call<boolean>(CMD.cancelScan);
    } catch {
      // IPC 都失败就别一直卡在「正在取消…」，允许用户再点一次
      setCancelling(false);
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