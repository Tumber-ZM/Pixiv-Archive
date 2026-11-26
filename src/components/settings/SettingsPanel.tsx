import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";

import { Dialog } from "../dialog/Dialog";
import { Tooltip } from "../common/Tooltip";
import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi } from "../../stores/ui";
import type {
  AppInfo,
  BundleImportReport,
  DataSizes,
  LegacyImportReport,
  RelinkReport,
  SelfBookmarkCheck,
  SettingsInput,
  SettingsView,
  UpdateInfo,
} from "../../types/ipc";

const TABS = ["基础", "网络", "数据迁移", "关于"] as const;
type Tab = (typeof TABS)[number];

/** 已保存的设置快照，用来判断有没有未保存的修改 */
interface SettingsSnapshot {
  dir: string;
  mode: string;
  proxy: string;
  mirror: string;
  delay: string;
  concurrency: string;
  checkOnStart: boolean;
}

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
 *
 * 所有页签的字段统一在底部「保存」提交；有未保存修改时关闭会先确认。
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
  /** 已保存的快照；为 null 表示还没加载完 */
  const [snapshot, setSnapshot] = useState<SettingsSnapshot | null>(null);
  /** 有未保存修改时点关闭，先弹确认框 */
  const [closePrompt, setClosePrompt] = useState(false);
  /** 「检查私密收藏」的诊断输出 */
  const [selfCheck, setSelfCheck] = useState<string | null>(null);
  const [selfChecking, setSelfChecking] = useState(false);

  // 网络
  const [mode, setMode] = useState("direct");
  const [proxy, setProxy] = useState("");
  const [mirror, setMirror] = useState("");
  const [token, setToken] = useState("");
  const [hasToken, setHasToken] = useState(false);
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
        const dirValue = view.image_source_dir ?? "";
        const modeValue = ["direct", "proxy", "system"].includes(view.mode)
          ? view.mode
          : "direct";
        const proxyValue = view.proxy ?? "";
        const mirrorValue = view.image_mirror ?? "";
        const delayValue = String(view.sync_delay_ms);
        const concurrencyValue = String(view.image_concurrency);
        setDir(dirValue);
        setMode(modeValue);
        setProxy(proxyValue);
        setMirror(mirrorValue);
        setDelay(delayValue);
        setConcurrency(concurrencyValue);
        setCheckOnStart(view.check_update_on_start);
        setHasToken(view.has_token);
        setToken("");
        setError(null);
        setSnapshot({
          dir: dirValue.trim(),
          mode: modeValue,
          proxy: proxyValue.trim(),
          mirror: mirrorValue.trim(),
          delay: delayValue,
          concurrency: concurrencyValue,
          checkOnStart: view.check_update_on_start,
        });
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

  /** 统一保存：所有页签的字段一次提交 */
  const saveAll = async (): Promise<boolean> => {
    setBusy(true);
    setError(null);
    const nextDir = dir.trim();
    const nextDelay = Math.max(0, Math.floor(Number(delay) || 0));
    const nextConcurrency = Math.min(8, Math.max(1, Math.floor(Number(concurrency) || 2)));
    const nextToken = token.trim();
    try {
      const input: SettingsInput = {
        image_source_dir: nextDir,
        mode,
        proxy: proxy.trim(),
        image_mirror: mirror.trim(),
        // 同步 / 订阅下载共用的请求间隔，0 = 不限速
        sync_delay_ms: nextDelay,
        image_concurrency: nextConcurrency,
        check_update_on_start: checkOnStart,
      };
      // token 留空表示「不修改」，避免误清空
      if (nextToken) input.refresh_token = nextToken;

      await call(CMD.saveSettings, { input });

      setDir(nextDir);
      setDelay(String(nextDelay));
      setConcurrency(String(nextConcurrency));
      setToken("");
      setSnapshot({
        dir: nextDir,
        mode,
        proxy: proxy.trim(),
        mirror: mirror.trim(),
        delay: String(nextDelay),
        concurrency: String(nextConcurrency),
        checkOnStart,
      });
      if (nextToken) {
        setHasToken(true);
        // 后端保存 token 成功后会自动把当前账号加进「收藏订阅」，通知列表重新取数
        bumpData();
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
      return true;
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason.message : String(reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  /** 有没有未保存的修改（token 输入框非空也算，因为它会被提交） */
  const dirty =
    snapshot !== null &&
    (dir.trim() !== snapshot.dir ||
      mode !== snapshot.mode ||
      proxy.trim() !== snapshot.proxy ||
      mirror.trim() !== snapshot.mirror ||
      delay.trim() !== snapshot.delay ||
      concurrency.trim() !== snapshot.concurrency ||
      checkOnStart !== snapshot.checkOnStart ||
      token.trim() !== "");

  /** 关闭入口统一走这里：有未保存修改先确认 */
  const requestClose = () => {
    if (busy) return;
    if (dirty) {
      setClosePrompt(true);
      return;
    }
    setOpen(false);
  };

  const discardAndClose = () => {
    setClosePrompt(false);
    setToken("");
    setOpen(false);
  };

  const saveAndClose = async () => {
    const ok = await saveAll();
    setClosePrompt(false);
    if (ok) setOpen(false);
  };

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      // 关闭确认框 / 导入确认框自己处理 Esc，这里不要再抢
      if (event.key !== "Escape" || closePrompt || pendingZip !== null || pendingLegacy) return;
      requestClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // requestClose 依赖 open / closePrompt / dirty，这几项已列入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, closePrompt, dirty, pendingZip, pendingLegacy]);

  // 诊断「我的私密收藏」：认不出账号 / 接口不给数据，一次说清
  const diagnoseSelf = async () => {
    setSelfChecking(true);
    setSelfCheck(null);
    try {
      const result = await call<SelfBookmarkCheck>(CMD.diagnoseBookmarks);
      if (result.error) {
        const shape = result.auth_shape
          ? ` · 换票响应字段：${result.auth_shape.response_keys.join(",") || "空"}` +
            `（${result.auth_shape.has_user ? "含 user" : "无 user"}）`
          : "";
        setSelfCheck(`账号识别失败：${result.error}${shape}`);
      } else {
        const parts = [`当前账号：${result.name ?? "未知"}（ID ${result.user_id}）`];
        parts.push(
          result.private_error
            ? `私密收藏失败：${result.private_error}`
            : `私密收藏首页 ${result.private_count ?? 0} 件`,
        );
        parts.push(
          result.public_error
            ? `公开收藏失败：${result.public_error}`
            : `公开收藏首页 ${result.public_count ?? 0} 件`,
        );
        const privateSubs = result.subs.filter((sub) => sub.restrict === "private");
        parts.push(`订阅 ${result.subs.length} 条，私密 ${privateSubs.length} 条`);
        setSelfCheck(parts.join(" · "));
      }
    } catch (reason: unknown) {
      setSelfCheck(isAppError(reason) ? reason.message : String(reason));
    } finally {
      setSelfChecking(false);
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
        onClick={requestClose}
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
                onClick={requestClose}
                aria-label="关闭设置"
              >
                <X size={16} />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
              {tab === "基础" ? (
                <div className="flex max-w-[480px] flex-col gap-5">
                  <label className="flex flex-col gap-2">
                    <Tooltip text="扫描图片时直接从这个目录读取；「收藏订阅」下载的图片也落到这里。文件名需包含 7–10 位 Pixiv 作品 ID，多图作品请用 123123_p0、123123_p1 这样的页码后缀命名，否则会被当作本地文件单独入库。">
                      <span className="text-body text-text">图片目录</span>
                    </Tooltip>
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
                </div>
              ) : tab === "网络" ? (
                <div className="flex max-w-[480px] flex-col gap-6">
                  <section className="flex flex-col gap-4">
                    <p className="text-micro text-text-3">连接</p>
                    <label className="flex flex-col gap-2">
                      <span className="text-body text-text">连接模式</span>
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
                        <span className="text-body text-text">代理地址</span>
                        <input
                          className="line-input"
                          placeholder="http://127.0.0.1:7890"
                          value={proxy}
                          onChange={(event) => setProxy(event.currentTarget.value)}
                        />
                      </label>
                    )}

                    <label className="flex flex-col gap-2">
                      <span className="text-body text-text">图片镜像（可选）</span>
                      <input
                        className="line-input"
                        placeholder="如 i.example.com，留空则直连官方域名"
                        value={mirror}
                        onChange={(event) => setMirror(event.currentTarget.value)}
                      />
                    </label>
                  </section>

                  <section className="flex flex-col gap-4">
                    <p className="text-micro text-text-3">下载</p>
                    <div className="grid grid-cols-2 gap-4">
                      <label className="flex flex-col gap-2">
                        <Tooltip text="同步元数据与订阅下载每次请求之间的等待时间，默认 200 毫秒；0 表示不限速。">
                          <span className="text-body text-text">请求间隔（毫秒）</span>
                        </Tooltip>
                        <input
                          className="line-input"
                          type="number"
                          min={0}
                          step={100}
                          value={delay}
                          onChange={(event) => setDelay(event.currentTarget.value)}
                        />
                      </label>
                      <label className="flex flex-col gap-2">
                        <Tooltip text="同时下载的图片数量（1–8），默认 2。数值越大越快，也越容易触发 Pixiv 限流。">
                          <span className="text-body text-text">同时下载数</span>
                        </Tooltip>
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
                    </div>
                    <p className="text-micro text-text-3">
                      数值越大下载越快，也越容易触发 Pixiv 限流。
                    </p>
                  </section>

                  <section className="flex flex-col gap-4">
                    <p className="text-micro text-text-3">账号</p>
                    <label className="flex flex-col gap-2">
                      <Tooltip text="token 只保存在本机 settings.json；同步元数据与「收藏订阅」下载都需要它。">
                        <span className="text-body text-text">Pixiv Refresh Token</span>
                      </Tooltip>
                      <input
                        className="line-input"
                        type="password"
                        placeholder={hasToken ? "已保存；留空则不修改" : "粘贴 refresh_token"}
                        value={token}
                        onChange={(event) => setToken(event.currentTarget.value)}
                      />
                    </label>

                    <div className="flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        className="btn"
                        disabled={selfChecking || busy}
                        onClick={diagnoseSelf}
                        title="用已保存的 token 查一下我是谁、私密收藏能读到多少"
                      >
                        {selfChecking ? "检查中…" : "检查私密收藏"}
                      </button>
                    </div>
                    {selfCheck && <p className="text-micro text-text-2">{selfCheck}</p>}
                  </section>
                </div>
              ) : tab === "数据迁移" ? (
                <div className="flex max-w-[480px] flex-col gap-5">
                  <p className="text-micro text-text-2">
                    当前数据：库 {formatSize(sizes?.db)} · 缩略图{" "}
                    {formatSize(sizes?.thumbnails)} · 元数据 {formatSize(sizes?.metadata)}
                  </p>

                  <div className="flex flex-col gap-3">
                    <div className="flex items-center justify-between gap-4 rounded-card border border-line px-4 py-3">
                      <div className="min-w-0">
                        <Tooltip text="把数据库、缩略图、元数据打成一个 zip，换机器时导入即可还原。">
                          <span className="text-body text-text">导出数据包</span>
                        </Tooltip>
                        <p className="mt-0.5 text-micro text-text-3">数据库、缩略图与元数据打包成 zip</p>
                      </div>
                      <button
                        type="button"
                        className="btn shrink-0"
                        disabled={migrating}
                        onClick={exportBundle}
                      >
                        选择目录并导出…
                      </button>
                    </div>

                    <div className="flex items-center justify-between gap-4 rounded-card border border-line px-4 py-3">
                      <div className="min-w-0">
                        <Tooltip text="导入前会自动备份现有库；缩略图/元数据按同名合并覆盖。">
                          <span className="text-body text-text">导入数据包</span>
                        </Tooltip>
                        <p className="mt-0.5 text-micro text-text-3">替换当前库，执行前自动备份</p>
                      </div>
                      <button
                        type="button"
                        className="btn shrink-0"
                        disabled={migrating}
                        onClick={chooseBundle}
                      >
                        选择数据包…
                      </button>
                    </div>

                    <div className="flex items-center justify-between gap-4 rounded-card border border-line px-4 py-3">
                      <div className="min-w-0">
                        <p className="text-body text-text">重新关联图片路径</p>
                        <p className="mt-0.5 text-micro text-text-3">图片搬家后按新位置重新关联</p>
                      </div>
                      <button
                        type="button"
                        className="btn shrink-0"
                        disabled={migrating}
                        onClick={relinkNow}
                      >
                        重新关联…
                      </button>
                    </div>

                    <div className="flex items-center justify-between gap-4 rounded-card border border-line px-4 py-3">
                      <div className="min-w-0">
                        <Tooltip text="先选择旧项目所在目录，再复制其中的 archive.db、缩略图与元数据（旧目录只读）。">
                          <span className="text-body text-text">导入旧项目数据</span>
                        </Tooltip>
                        <p className="mt-0.5 text-micro text-text-3">
                          从旧目录复制 archive.db、缩略图与元数据
                        </p>
                      </div>
                      <button
                        type="button"
                        className="btn shrink-0"
                        disabled={migrating}
                        onClick={pickLegacySource}
                      >
                        选择目录…
                      </button>
                    </div>
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
                <div className="flex max-w-[480px] flex-col gap-6">
                  <div>
                    <p className="text-subheading text-text">Pixiv Archive</p>
                    <p className="mt-1 text-micro text-text-2">
                      版本 {about ? `v${about.version}` : "…"}
                    </p>
                  </div>

                  <div className="grid grid-cols-[72px_minmax(0,1fr)] items-start gap-x-4 gap-y-3">
                    <span className="text-micro text-text-3">数据目录</span>
                    <span className="break-all text-micro text-text-2">
                      {about?.data_dir ?? "…"}
                    </span>
                    <span className="text-micro text-text-3">许可证</span>
                    <span className="text-micro text-text-2">GPL-3.0</span>
                  </div>

                  <div className="flex flex-col gap-3">
                    <div className="flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        className="btn"
                        disabled={checking}
                        onClick={checkNow}
                      >
                        {checking ? "检查中…" : "检查更新"}
                      </button>
                      <button type="button" className="btn" onClick={openRepo}>
                        打开仓库
                      </button>
                      {updateMessage && (
                        <span className="text-micro text-text-2">{updateMessage}</span>
                      )}
                    </div>
                    <label className="flex items-center gap-2 text-micro text-text-2">
                      <input
                        type="checkbox"
                        checked={checkOnStart}
                        onChange={(event) => setCheckOnStart(event.currentTarget.checked)}
                      />
                      启动时自动检查更新
                    </label>
                    <p className="break-all text-micro text-text-3">
                      {about?.repo_url ?? "https://github.com/Tumber-ZM/Pixiv-Archive"}
                    </p>
                  </div>
                </div>
              )}
            </div>
            <div className="flex items-center justify-end gap-3 border-t border-line px-6 py-4">
              {error && <span className="mr-auto break-all text-micro text-danger">{error}</span>}
              {saved && <span className="text-micro text-text-2">已保存</span>}
              <button
                type="button"
                className="btn"
                disabled={busy || !dirty}
                onClick={() => void saveAll()}
              >
                {busy ? "保存中…" : "保存"}
              </button>
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

      <Dialog
        open={closePrompt}
        title="有未保存的修改"
        confirmLabel="保存并关闭"
        secondaryLabel="不保存"
        onSecondary={discardAndClose}
        onCancel={() => setClosePrompt(false)}
        onConfirm={saveAndClose}
        busy={busy}
      >
        <p className="text-body text-text">当前设置还没保存，直接关闭会丢掉这次的修改。</p>
      </Dialog>
    </>
  );
}