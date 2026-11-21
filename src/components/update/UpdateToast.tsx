import { useEffect, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";

import { CMD, call } from "../../lib/ipc";
import type { SettingsView, UpdateInfo } from "../../types/ipc";

/** 启动后延迟多久检查：错开首屏加载，晚一点再联网。 */
const START_DELAY_MS = 4000;

/**
 * 启动更新检查。
 *
 * 有新版本且不是用户「忽略过」的版本时，在右下角显示一条轻提示；
 * 网络失败、限流、仓库还没发布 Release 时一律静默——检查更新不该打扰使用。
 */
export function UpdateToast() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const settings = await call<SettingsView>(CMD.getSettings);
          if (cancelled || !settings.check_update_on_start) return;
          const result = await call<UpdateInfo>(CMD.checkUpdate);
          if (cancelled || !result.has_update) return;
          if (settings.skipped_version && settings.skipped_version === result.latest) return;
          setInfo(result);
        } catch {
          // 静默：启动检查失败不弹任何东西
        }
      })();
    }, START_DELAY_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  if (!info) return null;

  const ignore = async () => {
    const version = info.latest;
    setInfo(null);
    try {
      await call(CMD.saveSettings, { input: { skipped_version: version } });
    } catch {
      // 保存失败也不影响本次关闭，下次仍会提示
    }
  };

  return (
    <div className="fixed bottom-4 right-4 z-50 w-[320px] rounded-card border border-line bg-surface p-4 shadow-lg">
      <p className="text-label text-text">发现新版本 {info.latest}</p>
      <p className="mt-1 text-micro text-text-2">当前版本 v{info.current}</p>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          className="btn"
          onClick={() => {
            void openUrl(info.url);
          }}
        >
          前往下载
        </button>
        <button type="button" className="btn" onClick={ignore}>
          忽略此版本
        </button>
        <button
          type="button"
          className="icon-btn ml-auto"
          onClick={() => setInfo(null)}
          aria-label="关闭"
        >
          ×
        </button>
      </div>
    </div>
  );
}