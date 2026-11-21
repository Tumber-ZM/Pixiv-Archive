import { useState, type ReactNode } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { ChevronDown, ChevronRight, FolderOpen, Lock, Star, Trash2 } from "lucide-react";

import { CMD, call } from "../../lib/ipc";
import { sanitizeRichText } from "../../lib/richText";
import { useUi } from "../../stores/ui";
import type { ArtworkDetail, FavoriteRef } from "../../types/ipc";

interface ViewerInfoProps {
  detail: ArtworkDetail | null;
  onFavorite: () => void;
  onDelete: () => void;
  onOpenTag: (name: string) => void;
  onOpenFavorite: (favorite: FavoriteRef) => void;
  /** 点击作者名跳到作者页 */
  onOpenAuthor: (authorId: number, name: string) => void;
}

function formatDate(raw: string | null): string {
  if (!raw) return "—";
  return raw.replace("T", " ").slice(0, 19);
}

/** 左栏分区：靠 1px 线与留白分段，不用卡片框（05-设计系统.md 全局规则） */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section
      className="mt-4 border-t pt-4"
      style={{ borderColor: "var(--viewer-line)" }}
    >
      <p className="mb-2 text-micro" style={{ color: "var(--viewer-text-2)" }}>
        {title}
      </p>
      {children}
    </section>
  );
}

/** 描述里的链接要交给系统浏览器打开，不能在 WebView 里导航走 */
function handleRichLink(event: React.MouseEvent<HTMLDivElement>) {
  const anchor = (event.target as HTMLElement).closest("a");
  if (!anchor) return;
  event.preventDefault();
  const href = anchor.getAttribute("href");
  if (href) openUrl(href).catch(() => undefined);
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="shrink-0 text-micro" style={{ color: "var(--viewer-text-2)" }}>
        {label}
      </span>
      <span
        className="truncate-1 text-right text-micro"
        style={{ color: "var(--viewer-text)" }}
      >
        {value}
      </span>
    </div>
  );
}

function Chip({
  children,
  onClick,
  title,
}: {
  children: ReactNode;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button type="button" className="viewer-chip" onClick={onClick} title={title}>
      {children}
    </button>
  );
}

/** 查看器左栏：320px，可滚动，底部操作固定（`06-界面规格.md` §8）。 */
export function ViewerInfo({
  detail,
  onFavorite,
  onDelete,
  onOpenTag,
  onOpenFavorite,
  onOpenAuthor,
}: ViewerInfoProps) {
  const [descOpen, setDescOpen] = useState(false);
  const safeMode = useUi((state) => state.safeMode);
  const toggleSafeMode = useUi((state) => state.toggleSafeMode);

  if (!detail) {
    return (
      <aside
        className="flex w-[320px] shrink-0 flex-col border-r p-6"
        style={{ background: "var(--viewer-panel)", borderColor: "var(--viewer-line)" }}
      >
        <p className="text-body" style={{ color: "var(--viewer-text-2)" }}>
          加载中…
        </p>
      </aside>
    );
  }

  const title = detail.title ?? `Pixiv ${detail.pixiv_id}`;
  const authorName = detail.author_name ?? "未知作者";
  const isFaved = detail.favorites.length > 0;
  const sourcePath = detail.images[0]?.path ?? detail.local_path ?? null;

  return (
    <aside
      className="flex w-[320px] shrink-0 flex-col border-r"
      style={{ background: "var(--viewer-panel)", borderColor: "var(--viewer-line)" }}
    >
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {/* 标题区 */}
        <h2 className="text-heading leading-snug" style={{ color: "var(--viewer-text)" }}>
          {title}
        </h2>
        {detail.author_id !== null ? (
          <button
            type="button"
            className="mt-2 block text-left text-body transition-opacity duration-500 hover:opacity-75"
            style={{ color: "var(--viewer-text-2)" }}
            title={`查看作者「${authorName}」的作品`}
            onClick={() => onOpenAuthor(detail.author_id as number, authorName)}
          >
            {authorName}
          </button>
        ) : (
          <p className="mt-2 text-body" style={{ color: "var(--viewer-text-2)" }}>
            {authorName}
          </p>
        )}
        <div className="mt-2 flex flex-wrap gap-1.5">
          {detail.pixiv_id < 0 && <span className="badge badge-local">本地</span>}
          {detail.is_r18 && <span className="badge badge-r18">R18</span>}
          {detail.ai_type === 2 && <span className="badge badge-ai">AI</span>}
          {detail.is_gif && <span className="badge badge-gif">GIF</span>}
          {detail.pixiv_status === "deleted" && (
            <span className="badge badge-deleted">已删除</span>
          )}
        </div>

        {/* 标签：可点，跳到该标签的作品 */}
        {detail.tags.length > 0 && (
          <Section title="标签">
            <div className="flex flex-wrap gap-1.5">
              {detail.tags.map((tag) => (
                <Chip
                  key={tag.id}
                  onClick={() => onOpenTag(tag.name)}
                  title={`查看 #${tag.translated_name ?? tag.name} 的作品`}
                >
                  {tag.translated_name ?? tag.name}
                </Chip>
              ))}
            </div>
          </Section>
        )}

        {/* 收藏夹：可点，跳到该收藏夹 */}
        {detail.favorites.length > 0 && (
          <Section title="所在收藏夹">
            <div className="flex flex-wrap gap-1.5">
              {detail.favorites.map((favorite) => (
                <Chip
                  key={favorite.id}
                  onClick={() => onOpenFavorite(favorite)}
                  title={`打开收藏夹「${favorite.name}」`}
                >
                  {favorite.name}
                </Chip>
              ))}
            </div>
          </Section>
        )}

        {/* 描述：默认折叠 */}
        {detail.description && (
          <Section title="描述">
            <button
              type="button"
              className="flex items-center gap-1 text-micro"
              style={{ color: "var(--viewer-text-2)" }}
              onClick={() => setDescOpen((open) => !open)}
            >
              {descOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              {descOpen ? "收起" : "展开全文"}
            </button>
            {descOpen && (
              <div
                className="viewer-rich mt-2"
                style={{ color: "var(--viewer-text)" }}
                onClick={handleRichLink}
                // 内容已经过 sanitizeRichText 白名单重建
                dangerouslySetInnerHTML={{ __html: sanitizeRichText(detail.description ?? "") }}
              />
            )}
          </Section>
        )}

        {/* 元信息 */}
        <Section title="元信息">
          <div className="flex flex-col gap-1">
            <MetaRow
              label="Pixiv ID"
              value={detail.pixiv_id > 0 ? String(detail.pixiv_id) : "本地文件"}
            />
            <MetaRow label="页数" value={String(detail.page_count)} />
            <MetaRow
              label="尺寸"
              value={detail.width && detail.height ? `${detail.width}×${detail.height}` : "—"}
            />
            <MetaRow label="发布" value={formatDate(detail.create_date)} />
            <MetaRow
              label="状态"
              value={detail.pixiv_status === "deleted" ? "已删除" : "活跃"}
            />
            <MetaRow label="首次扫描" value={formatDate(detail.first_seen)} />
            <MetaRow label="最后同步" value={formatDate(detail.last_synced)} />
            {detail.sync_error && <MetaRow label="同步失败" value={detail.sync_error} />}
          </div>
        </Section>
      </div>

      <div
        className="flex shrink-0 items-center gap-2 border-t px-6 py-4"
        style={{ borderColor: "var(--viewer-line)" }}
      >
        <button
          type="button"
          className="btn inline-flex items-center gap-2"
          style={{
            color: isFaved ? "var(--color-accent)" : "var(--viewer-text)",
            borderColor: isFaved ? "var(--color-accent)" : "var(--viewer-line)",
          }}
          onClick={onFavorite}
          title={isFaved ? "已收藏，点击管理所属收藏夹" : "收藏（F）"}
        >
          <Star size={14} fill={isFaved ? "currentColor" : "none"} />
          {isFaved ? "已收藏" : "收藏"}
        </button>
        <button
          type="button"
          className="icon-btn ml-auto"
          style={{ color: safeMode ? "var(--color-accent)" : "var(--viewer-text-2)" }}
          onClick={toggleSafeMode}
          data-active={safeMode}
          aria-pressed={safeMode}
          title={safeMode ? "关闭安全模式" : "开启安全模式（模糊所有图片）"}
          aria-label={safeMode ? "关闭安全模式" : "开启安全模式"}
        >
          <Lock size={14} />
        </button>
        <button
          type="button"
          className="icon-btn"
          style={{ color: "var(--viewer-text-2)" }}
          disabled={!sourcePath}
          title={sourcePath ? "在文件夹中定位" : "该作品没有本地文件"}
          aria-label="在文件夹中定位"
          onClick={() => {
            if (!sourcePath) return;
            call(CMD.openInFolder, { path: sourcePath }).catch(() => undefined);
          }}
        >
          <FolderOpen size={14} />
        </button>
        <button
          type="button"
          className="icon-btn"
          style={{ color: "var(--color-danger)" }}
          onClick={onDelete}
          title="删除作品"
          aria-label="删除作品"
        >
          <Trash2 size={14} />
        </button>
      </div>
    </aside>
  );
}