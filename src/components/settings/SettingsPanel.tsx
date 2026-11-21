import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";

import { Dialog } from "../dialog/Dialog";
import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi } from "../../stores/ui";
import type {
  AppInfo,
  BundleImportReport,
  DataSizes,
  LegacyImportReport,
  RelinkReport,
  SettingsInput,
  SettingsView,
  UpdateInfo,
} from "../../types/ipc";

const TABS = ["基础", "网络", "数据迁移", "关于"] as const;
type Tab = (typeof TABS)[number];

function formatSize(bytes: number | undefined): string {
  if (!bytes) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/**
 * 设置面板（`06-界面规格.md` §9）。
 *
 * 基础：图片目录；网络：连接模式 / 代理 / 镜像 / 请求间隔 / token；
 * 数据迁移：导出/导入数据包、导入旧项目数据、重新关联图片路径。
 */
export function SettingsPanel() {
  const open = useUi((state) => state.settingsOpen);
  const setOpen = useUi((state) => state.setSettingsOpen);
  const bumpData = useUi((state) => state.bumpData);

  const [tab, setTab] = useState<Tab>("基础");
  const [dir, setDir] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 网络
  const [mode, setMode] = useState("direct");
  const [proxy, setProxy] = useState("");
  const [mirror, setMirror] = useState("");
  const [token, setToken] = useState("");
  const [hasToken, setHasToken] = useState(false);
  const [netSaved, setNetSaved] = useState(false);
  const [delay, setDelay] = useState("200");
  const [concurrency, setConcurrency] = useState("2");

  // 数据迁移
  const [sizes, setSizes] = useState<DataSizes | null>(null);
  const [migrating, setMigrating] = useState(false);
  const [migrationNotice, setMigrationNotice] = useState<string | null>(null);
  const [migrationError, setMigrationError] = useState<string | null>(null);
  const [pendingZip, setPendingZip] = useState<string | null>(null);
  const [pendingLegacy, setPendingLegacy] = useState(false);
  // 导入旧项目时用户选中的目录；确认框里回显，导入命令用它替代默认路径
  const [legacySource, setLegacySource] = useState<string | null>(null);

  // 关于页
  const [about, setAbout] = useState<AppInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);
  const [checkOnStart, setCheckOnStart] = useState(true);

  // 每次打开都重新读一遍，避免显示被别的窗口改过的旧值
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    call<SettingsView>(CMD.getSettings)
      .then((view) => {
        if (cancelled) return;
        setDir(view.image_source_dir ?? "");
        setMode(["direct", "proxy", "system"].includes(view.mode) ? view.mode : "direct");
        setProxy(view.proxy ?? "");
        setMirror(view.image_mirror ?? "");
        setDelay(String(view.sync_delay_ms));
        setConcurrency(String(view.image_concurrency));
        setCheckOnStart(view.check_update_on_start);
        setHasToken(view.has_token);
        setToken("");
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(isAppError(reason) ? reason.message : String(reason));
      });
    call<DataSizes>(CMD.dataSizes)
      .then((value) => {
        if (!cancelled) setSizes(value);
      })
      .catch(() => undefined);
    call<AppInfo>(CMD.appInfo)
      .then((value) => {
        if (!cancelled) setAbout(value);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, setOpen]);

  // 打开系统目录选择器；用户取消就保持原值
  const pick = async () => {
    try {
      const picked = await call<string | null>(CMD.pickDirectory);
      if (picked) {
        setDir(picked);
        setError(null);
      }
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const trimmed = dir.trim();
      await call(CMD.saveSettings, { input: { image_source_dir: trimmed } });
      setDir(trimmed);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  // token 留空表示「不修改」，避免用户在别的页签点保存时被误清空
  const saveNetwork = async () => {
    setBusy(true);
    setError(null);
    try {
      const input: SettingsInput = {
        mode,
        proxy: proxy.trim(),
        image_mirror: mirror.trim(),
        // 同步 / 订阅下载共用的请求间隔，0 = 不限速
        sync_delay_ms: Math.max(0, Math.floor(Number(delay) || 0)),
        image_concurrency: Math.min(8, Math.max(1, Math.floor(Number(concurrency) || 2))),
      };
      if (token.trim()) input.refresh_token = token.trim();

      await call(CMD.saveSettings, { input });
      if (token.trim()) setHasToken(true);
      setToken("");
      setNetSaved(true);
      setTimeout(() => setNetSaved(false), 1500);
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const refreshSizes = () => {
    call<DataSizes>(CMD.dataSizes)
      .then(setSizes)
      .catch(() => undefined);
  };

  const exportBundle = async () => {
    setMigrating(true);
    setMigrationError(null);
    setMigrationNotice(null);
    try {
      const target = await call<string | null>(CMD.pickDirectory);
      if (!target) return;
      const zip = await call<string>(CMD.exportDataBundle, { destDir: target });
      setMigrationNotice(`已导出：${zip}`);
      refreshSizes();
    } catch (reason: unknown) {
      setMigrationError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setMigrating(false);
    }
  };

  const chooseBundle = async () => {
    try {
      const zip = await call<string | null>(CMD.pickFile);
      if (zip) setPendingZip(zip);
    } catch (reason: unknown) {
      setMigrationError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const doImportBundle = async () => {
    if (!pendingZip) return;
    setMigrating(true);
    setMigrationError(null);
    setMigrationNotice(null);
    try {
      const report = await call<BundleImportReport>(CMD.importDataBundle, {
        zipPath: pendingZip,
        relink: true,
      });
      const backup = report.backed_up_to ? ` · 旧库备份：${report.backed_up_to}` : "";
      const relinked = report.relinked ? ` · 重关联 ${report.relinked.relinked} 条` : "";
      setMigrationNotice(
        `导入完成：作品 ${report.artworks} · 图片 ${report.images} · 缩略图 ${report.thumbs_restored}${relinked}${backup}`,
      );
      setPendingZip(null);
      bumpData();
      refreshSizes();
    } catch (reason: unknown) {
      setMigrationError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setMigrating(false);
    }
  };

  // 点「导入旧项目数据」先选目录：不再依赖编译时写死的开发机路径
  const pickLegacySource = async () => {
    setMigrationError(null);
    setMigrationNotice(null);
    try {
      const picked = await call<string | null>(CMD.pickDirectory);
      if (!picked) return;
      setLegacySource(picked);
      setPendingLegacy(true);
    } catch (reason: unknown) {
      setMigrationError(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const doImportLegacy = async () => {
    if (!legacySource) return;
    setMigrating(true);
    setMigrationError(null);
    setMigrationNotice(null);
    try {
      const report = await call<LegacyImportReport>(CMD.importLegacy, {
        legacyDir: legacySource,
        includeThumbs: true,
        includeMetadata: true,
      });
      const backup = report.backed_up_to ? ` · 旧库备份：${report.backed_up_to}` : "";
      setMigrationNotice(
        `已导入旧项目数据：作品 ${report.artworks} · 图片 ${report.images} · 缩略图 ${report.thumbs_copied}${backup}`,
      );
      setPendingLegacy(false);
      setLegacySource(null);
      bumpData();
      refreshSizes();
    } catch (reason: unknown) {
      setMigrationError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setMigrating(false);
    }
  };

  const relinkNow = async () => {
    setMigrating(true);
    setMigrationError(null);
    setMigrationNotice(null);
    try {
      const report = await call<RelinkReport>(CMD.relinkImages);
      setMigrationNotice(
        `重新关联：更新 ${report.relinked} 条 · 仍缺失 ${report.missing} · 同名多份未动 ${report.ambiguous}`,
      );
      bumpData();
    } catch (reason: unknown) {
      setMigrationError(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setMigrating(false);
    }
  };

  // 手动检查更新：结果与错误都直接显示在按钮旁边
  const checkNow = async () => {
    setChecking(true);
    setUpdateMessage(null);
    try {
      const info = await call<UpdateInfo>(CMD.checkUpdate);
      if (info.has_update) {
        setUpdateMessage(`发现新版本 ${info.latest}（当前 v${info.current}）`);
      } else if (info.notes === "暂无发布版本") {
        setUpdateMessage("仓库还没有发布版本");
      } else {
        setUpdateMessage(`已是最新版本（v${info.current}）`);
      }
    } catch (reason: unknown) {
      setUpdateMessage(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setChecking(false);
    }
  };

  const toggleCheckOnStart = async (value: boolean) => {
    setCheckOnStart(value);
    try {
      await call(CMD.saveSettings, { input: { check_update_on_start: value } });
    } catch (reason: unknown) {
      setUpdateMessage(isAppError(reason) ? reason.message : String(reason));
    }
  };

  const openRepo = async () => {
    const url = about?.repo_url ?? "https://github.com/Tumber-ZM/Pixiv-Archive";
    try {
      await openUrl(url);
    } catch {
      // 浏览器打不开时静默，地址本身页面上也能复制
    }
  };

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-40 flex items-center justify-center"
        style={{ background: "var(--color-overlay)" }}
        onClick={() => setOpen(false)}
        role="dialog"
        aria-modal="true"
        aria-label="设置"
      >
        <div
          className="flex h-[80vh] w-[720px] max-w-[92vw] overflow-hidden rounded-card border border-line bg-surface"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="w-[160px] shrink-0 border-r border-line p-3">
            <p className="group-label mb-3 px-2">设置</p>
            {TABS.map((name) => (
              <button
                key={name}
                type="button"
                className="nav-item w-full"
                data-active={tab === name}
                onClick={() => setTab(name)}
              >
                <span>{name}</span>
              </button>
            ))}
          </div>

          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex items-center justify-between border-b border-line px-6 py-4">
              <p className="text-subheading text-text">{tab}</p>
              <button
                type="button"
                className="icon-btn"
                onClick={() => setOpen(false)}
                aria-label="关闭设置"
              >
                <X size={16} />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
              {tab === "基础" ? (
                <div className="flex max-w-[420px] flex-col gap-4">
                  <label className="flex flex-col gap-2">
                    <span className="text-label text-text-2">图片目录</span>
                    <div className="flex items-center gap-2">
                      <input
                        className="line-input flex-1"
                        placeholder="例如 D:\\Pictures\\pixiv"
                        value={dir}
                        onChange={(event) => setDir(event.currentTarget.value)}
                      />
                      <button type="button" className="btn shrink-0" onClick={pick}>
                        浏览…
                      </button>
                    </div>
                  </label>
                  <p className="text-micro text-text-3">
                    扫描图片时直接从这个目录读取；「订阅列表」下载的图片也落到这里。
                  </p>
                  <p className="text-micro text-text-3">
                    请确保文件名包含 7–10 位 Pixiv 作品 ID；多图作品请用
                    123123_p0、123123_p1、123123_p2 这样的页码后缀命名，否则会被当作本地文件单独入库。
                  </p>
                  <div className="flex items-center gap-3">
                    <button type="button" className="btn" disabled={busy} onClick={save}>
                      {busy ? "保存中…" : "保存"}
                    </button>
                    {saved && <span className="text-micro text-text-2">已保存</span>}
                    {error && <span className="text-micro text-danger">{error}</span>}
                  </div>
                </div>
              ) : tab === "网络" ? (
                <div className="flex max-w-[520px] flex-col gap-5">
                  <label className="flex flex-col gap-2">
                    <span className="text-label text-text-2">连接模式</span>
                    <select
                      className="line-input"
                      value={mode}
                      onChange={(event) => setMode(event.currentTarget.value)}
                    >
                      <option value="direct">直连（普通 HTTPS）</option>
                      <option value="proxy">指定代理</option>
                      <option value="system">跟随系统代理</option>
                    </select>
                  </label>

                  {mode === "proxy" && (
                    <label className="flex flex-col gap-2">
                      <span className="text-label text-text-2">代理地址</span>
                      <input
                        className="line-input"
                        placeholder="http://127.0.0.1:7890"
                        value={proxy}
                        onChange={(event) => setProxy(event.currentTarget.value)}
                      />
                    </label>
                  )}

                  <label className="flex flex-col gap-2">
                    <span className="text-label text-text-2">图片镜像（可选）</span>
                    <input
                      className="line-input"
                      placeholder="如 i.example.com，留空则直连官方域名"
                      value={mirror}
                      onChange={(event) => setMirror(event.currentTarget.value)}
                    />
                  </label>

                  <label className="flex flex-col gap-2">
                    <span className="text-label text-text-2">请求间隔（毫秒）</span>
                    <input
                      className="line-input"
                      type="number"
                      min={0}
                      step={100}
                      value={delay}
                      onChange={(event) => setDelay(event.currentTarget.value)}
                    />
                  </label>
                  <p className="text-micro text-text-3">
                    同步元数据与订阅下载每次请求之间的等待时间，0 表示不限速，默认 200。
                  </p>

                  <label className="flex flex-col gap-2">
                    <span className="text-label text-text-2">同时下载数</span>
                    <input
                      className="line-input"
                      type="number"
                      min={1}
                      max={8}
                      step={1}
                      value={concurrency}
                      onChange={(event) => setConcurrency(event.currentTarget.value)}
                    />
                  </label>
                  <p className="text-micro text-text-3">
                    同时下载的图片数量（1–8），默认 2。越大越快，也越容易触发 Pixiv 限流。
                  </p>

                  <label className="flex flex-col gap-2">
                    <span className="text-label text-text-2">Pixiv Refresh Token</span>
                    <input
                      className="line-input"
                      type="password"
                      placeholder={hasToken ? "已保存；留空则不修改" : "粘贴 refresh_token"}
                      value={token}
                      onChange={(event) => setToken(event.currentTarget.value)}
                    />
                  </label>
                  <p className="text-micro text-text-3">
                    token 只保存在本机 settings.json；同步元数据、「订阅列表」下载和「同步收藏」都需要它。
                  </p>

                  <div className="flex items-center gap-3">
                    <button type="button" className="btn" disabled={busy} onClick={saveNetwork}>
                      {busy ? "保存中…" : "保存网络设置"}
                    </button>
                    {netSaved && <span className="text-micro text-text-2">已保存</span>}
                    {error && <span className="text-micro text-danger">{error}</span>}
                  </div>
                </div>
              ) : tab === "数据迁移" ? (
                <div className="flex max-w-[560px] flex-col gap-5">
                  <p className="text-micro text-text-2">
                    当前数据：库 {formatSize(sizes?.db)} · 缩略图{" "}
                    {formatSize(sizes?.thumbnails)} · 元数据 {formatSize(sizes?.metadata)}
                  </p>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">导出数据包</span>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        className="btn"
                        disabled={migrating}
                        onClick={exportBundle}
                      >
                        选择目录并导出…
                      </button>
                    </div>
                    <p className="text-micro text-text-3">
                      把数据库、缩略图、元数据打成一个 zip，换机器时导入即可还原。
                    </p>
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">导入数据包</span>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        className="btn"
                        disabled={migrating}
                        onClick={chooseBundle}
                      >
                        选择数据包…
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={migrating}
                        onClick={relinkNow}
                      >
                        重新关联图片路径
                      </button>
                    </div>
                    <p className="text-micro text-text-3">
                      导入前会自动备份现有库；缩略图/元数据按同名合并覆盖。
                    </p>
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">旧项目数据</span>
                    <button
                      type="button"
                      className="btn self-start"
                      disabled={migrating}
                      onClick={pickLegacySource}
                    >
                      导入旧项目数据…
                    </button>
                    <p className="text-micro text-text-3">
                      先选择旧项目所在目录，再复制其中的 archive.db、缩略图与元数据（旧目录只读）。
                    </p>
                  </div>

                  {migrating && <p className="text-micro text-text-2">处理中…</p>}
                  {migrationError && (
                    <p className="break-all text-micro text-danger">{migrationError}</p>
                  )}
                  {migrationNotice && (
                    <p className="break-all text-micro text-text-2">{migrationNotice}</p>
                  )}
                </div>
              ) : (
                <div className="flex max-w-[520px] flex-col gap-5">
                  <div>
                    <p className="text-subheading text-text">Pixiv Archive</p>
                    <p className="mt-1 text-micro text-text-2">
                      版本 {about ? `v${about.version}` : "…"}
                    </p>
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">GitHub 仓库</span>
                    <button type="button" className="btn self-start" onClick={openRepo}>
                      打开仓库
                    </button>
                    <p className="break-all text-micro text-text-3">
                      {about?.repo_url ?? "https://github.com/Tumber-ZM/Pixiv-Archive"}
                    </p>
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">检查更新</span>
                    <div className="flex items-center gap-3">
                      <button
                        type="button"
                        className="btn"
                        disabled={checking}
                        onClick={checkNow}
                      >
                        {checking ? "检查中…" : "检查更新"}
                      </button>
                      {updateMessage && (
                        <span className="text-micro text-text-2">{updateMessage}</span>
                      )}
                    </div>
                    <label className="flex items-center gap-2 text-micro text-text-2">
                      <input
                        type="checkbox"
                        checked={checkOnStart}
                        onChange={(event) => toggleCheckOnStart(event.currentTarget.checked)}
                      />
                      启动时自动检查更新
                    </label>
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">数据目录</span>
                    <p className="break-all text-micro text-text-2">
                      {about?.data_dir ?? "…"}
                    </p>
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="text-label text-text-2">许可证</span>
                    <p className="text-micro text-text-2">GPL-3.0</p>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <Dialog
        open={pendingZip !== null}
        title="导入数据包"
        confirmLabel="导入"
        busy={migrating}
        onCancel={() => setPendingZip(null)}
        onConfirm={doImportBundle}
      >
        <p className="text-body text-text">
          将用数据包里的数据库替换当前库，缩略图与元数据按同名合并。
        </p>
        <p className="mt-2 break-all text-micro text-text-3">{pendingZip}</p>
        <p className="mt-2 text-micro text-text-2">
          现有库会先自动备份为 library.db.bak-*，不会丢数据。
        </p>
      </Dialog>

      <Dialog
        open={pendingLegacy}
        title="导入旧项目数据"
        confirmLabel="导入"
        busy={migrating}
        onCancel={() => {
          setPendingLegacy(false);
          setLegacySource(null);
          setMigrationError(null);
        }}
        onConfirm={doImportLegacy}
      >
        <p className="text-body text-text">
          从下面的旧项目目录复制 archive.db、缩略图与元数据到当前数据目录：
        </p>
        <p className="mt-2 break-all rounded bg-surface-2 px-3 py-2 text-micro text-text-2">
          {legacySource}
        </p>
        <p className="mt-2 text-micro text-text-2">
          导入前会自动备份现有库；旧项目目录只读，不会被修改。
        </p>
        {migrationError && (
          <p className="mt-2 break-all text-micro text-danger">{migrationError}</p>
        )}
      </Dialog>
    </>
  );
}