import { useCallback, useEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";

import { CMD, call, isAppError } from "../../lib/ipc";
import { useUi } from "../../stores/ui";
import type { AppError, ArtworkDetail } from "../../types/ipc";
import { ContextMenu } from "../common/ContextMenu";
import { Dialog } from "../dialog/Dialog";
import { FavoritePicker } from "./FavoritePicker";
import { ViewerImage } from "./ViewerImage";
import { ViewerInfo } from "./ViewerInfo";

interface ViewerProps {
  /** 当前列表上下文，用于作品间导航 */
  artworkIds: number[];
  initialIndex: number;
  onClose: () => void;
  /** 作品被删除或收藏夹变化后，让列表重新取数 */
  onChanged: () => void;
  /** 还有未加载的作品时为 true */
  hasMore?: boolean;
  /** 走到最后一件时请求下一批 */
  onLoadMore?: () => void;
}

export function Viewer({
  artworkIds,
  initialIndex,
  onClose,
  onChanged,
  hasMore = false,
  onLoadMore,
}: ViewerProps) {
  const [index, setIndex] = useState(initialIndex);
  const [page, setPage] = useState(0);
  const [detail, setDetail] = useState<ArtworkDetail | null>(null);
  const [error, setError] = useState<AppError | null>(null);
  const [favoriteOpen, setFavoriteOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);

  const openTag = useUi((state) => state.openTag);
  const openFavorite = useUi((state) => state.openFavorite);
  const openAuthor = useUi((state) => state.openAuthor);
  const cycleTheme = useUi((state) => state.cycleTheme);
  const setSettingsOpen = useUi((state) => state.setSettingsOpen);
  const settingsOpen = useUi((state) => state.settingsOpen);

  const currentId = artworkIds[index];

  useEffect(() => {
    if (currentId === undefined) return;
    let cancelled = false;
    setDetail(null);
    setPage(0);
    setError(null);

    call<ArtworkDetail>(CMD.getArtwork, { artworkId: currentId })
      .then((result) => {
        if (!cancelled) setDetail(result);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(isAppError(reason) ? reason : { code: "INTERNAL", message: String(reason) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [currentId, reloadKey]);

  const goPrev = useCallback(() => {
    setIndex((current) => Math.max(0, current - 1));
  }, []);

  const goNext = useCallback(() => {
    setIndex((current) => Math.min(artworkIds.length - 1, current + 1));
  }, [artworkIds.length]);

  // 停到最后一件且还有未加载的作品时，自动再取一批；
  // artworkIds 由父组件追加，index 不变，所以不会跳走
  useEffect(() => {
    if (!hasMore || !onLoadMore) return;
    if (index < artworkIds.length - 1) return;
    onLoadMore();
  }, [index, artworkIds.length, hasMore, onLoadMore]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // 弹层打开时键盘归弹层管，避免 Esc 直接关掉整个查看器
      if (favoriteOpen || deleteOpen || settingsOpen) return;

      switch (event.key) {
        case "Escape":
          onClose();
          break;
        case "ArrowLeft":
          goPrev();
          break;
        case "ArrowRight":
          goNext();
          break;
        case "ArrowUp":
        case ",":
          setPage((current) => Math.max(0, current - 1));
          break;
        case "ArrowDown":
        case ".":
          setPage((current) => Math.min((detail?.page_count ?? 1) - 1, current + 1));
          break;
        case "f":
        case "F":
          setFavoriteOpen(true);
          break;
        default:
          break;
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, goPrev, goNext, detail, favoriteOpen, deleteOpen, settingsOpen]);

  const handleDelete = async () => {
    setBusy(true);
    try {
      await call(CMD.deleteArtwork, { artworkId: currentId, deleteFiles });
      setDeleteOpen(false);
      onChanged();
      onClose();
    } catch (reason: unknown) {
      setError(isAppError(reason) ? reason : { code: "INTERNAL", message: String(reason) });
    } finally {
      setBusy(false);
    }
  };

  const imagePath = detail?.images[page]?.path ?? null;
  const src = imagePath ? convertFileSrc(imagePath) : null;

  return (
    <div
      className="pa-viewer fixed inset-0 z-40 flex"
      style={{ background: "var(--viewer-bg)" }}
      onContextMenu={(event) => {
        event.preventDefault();
        // 查看器挂在画廊内部，不拦住冒泡就会同时弹出画廊的空白菜单
        event.stopPropagation();
        setMenu({ x: event.clientX, y: event.clientY });
      }}
    >
      <ViewerInfo
        detail={detail}
        onFavorite={() => setFavoriteOpen(true)}
        onDelete={() => setDeleteOpen(true)}
        onOpenTag={(name) => {
          onClose();
          openTag(name);
        }}
        onOpenFavorite={(favorite) => {
          onClose();
          openFavorite(favorite.id, favorite.name);
        }}
        onOpenAuthor={(authorId, name) => {
          onClose();
          openAuthor(authorId, name);
        }}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-12 shrink-0 items-center gap-3 px-4">
          <button
            type="button"
            className="btn inline-flex items-center gap-2"
            style={{ color: "var(--viewer-text)", borderColor: "var(--viewer-line)" }}
            onClick={onClose}
          >
            <ArrowLeft size={14} />
            返回
          </button>

          <div className="ml-2 flex items-center gap-1">
            <button
              type="button"
              className="icon-btn"
              style={{ color: "var(--viewer-text-2)" }}
              onClick={goPrev}
              disabled={index <= 0}
              title="上一个作品（←）"
              aria-label="上一个作品"
            >
              <ChevronLeft size={16} />
            </button>
            <span
              className="min-w-[52px] text-center text-micro"
              style={{ color: "var(--viewer-text-2)" }}
            >
              {index + 1} / {artworkIds.length}
            </span>
            <button
              type="button"
              className="icon-btn"
              style={{ color: "var(--viewer-text-2)" }}
              onClick={goNext}
              disabled={index >= artworkIds.length - 1}
              title="下一个作品（→）"
              aria-label="下一个作品"
            >
              <ChevronRight size={16} />
            </button>
          </div>
          {error && (
            <span className="text-micro" style={{ color: "var(--color-danger)" }}>
              {error.message}
            </span>
          )}
        </div>

        <ViewerImage
          src={src}
          pageIndex={page}
          pageCount={detail?.page_count ?? 1}
          onPageChange={setPage}
          onPrevArtwork={goPrev}
          onNextArtwork={goNext}
          canPrevArtwork={index > 0}
          canNextArtwork={index < artworkIds.length - 1}
        />
      </div>

      {favoriteOpen && detail && (
        <FavoritePicker
          artworkIds={[detail.id]}
          onClose={() => setFavoriteOpen(false)}
          onSaved={() => {
            setFavoriteOpen(false);
            // 重新拉一次详情，让左栏的「已收藏」按钮和「所在收藏夹」同步
            setReloadKey((key) => key + 1);
            onChanged();
          }}
        />
      )}

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
          items={[
            { label: "上一作品", disabled: index <= 0, onSelect: goPrev },
            {
              label: "下一作品",
              disabled: index >= artworkIds.length - 1,
              onSelect: goNext,
            },
            {
              label: "在文件夹中定位",
              disabled: !(detail?.images[0]?.path ?? detail?.local_path),
              separatorBefore: true,
              onSelect: () => {
                const path = detail?.images[0]?.path ?? detail?.local_path;
                if (!path) return;
                call(CMD.openInFolder, { path }).catch(() => undefined);
              },
            },
            { label: "收藏", onSelect: () => setFavoriteOpen(true) },
            { label: "删除作品", danger: true, onSelect: () => setDeleteOpen(true) },
            { label: "切换主题", separatorBefore: true, onSelect: cycleTheme },
            { label: "打开设置", onSelect: () => setSettingsOpen(true) },
            { label: "关闭查看器", separatorBefore: true, onSelect: onClose },
          ]}
        />
      )}

      <Dialog
        open={deleteOpen}
        title="删除作品"
        danger
        busy={busy}
        confirmLabel="删除"
        onCancel={() => setDeleteOpen(false)}
        onConfirm={handleDelete}
      >
        <p>将从库中移除「{detail?.title ?? `Pixiv ${detail?.pixiv_id}`}」。</p>
        <label className="mt-3 flex items-center gap-2">
          <input
            type="checkbox"
            checked={deleteFiles}
            onChange={(event) => setDeleteFiles(event.currentTarget.checked)}
          />
          <span>同时删除本地源文件</span>
        </label>
      </Dialog>
    </div>
  );
}