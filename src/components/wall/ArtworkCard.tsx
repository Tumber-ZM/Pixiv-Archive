import { convertFileSrc } from "@tauri-apps/api/core";
import { Check, Layers, Star, Trash2 } from "lucide-react";
import type { KeyboardEvent, MouseEvent } from "react";

import type { Layout } from "../../stores/ui";
import type { ArtworkCard as Card } from "../../types/ipc";

interface ArtworkCardProps {
  card: Card;
  layout?: Layout;
  selected?: boolean;
  onOpen?: (card: Card) => void;
  onToggle?: (card: Card) => void;
  onFavorite?: (card: Card) => void;
  onDelete?: (card: Card) => void;
  onContextMenu?: (card: Card, event: MouseEvent<HTMLElement>) => void;
}

/**
 * 本地图片必须经 `convertFileSrc` 转换，WebView 不允许直接读 `file://`。
 * 路径不在 assetProtocol scope 内时会静默返回空白，所以拿不到就退化成占位。
 */
function mediaSrc(path: string | null): string | null {
  if (!path) return null;
  try {
    return convertFileSrc(path);
  } catch {
    return null;
  }
}

function Badges({ card }: { card: Card }) {
  return (
    <div className="absolute left-1.5 top-1.5 flex flex-wrap gap-1">
      {card.pixiv_id < 0 && <span className="badge badge-local">本地</span>}
      {card.is_r18 && <span className="badge badge-r18">R18</span>}
      {card.ai_type === 2 && <span className="badge badge-ai">AI</span>}
      {card.is_gif && <span className="badge badge-gif">GIF</span>}
      {card.page_count > 1 && (
        <span className="badge badge-page">
          <Layers size={8} />
          {card.page_count}
        </span>
      )}
      {card.pixiv_status === "deleted" && (
        <span className="badge badge-deleted">已隐藏</span>
      )}
    </div>
  );
}

/** 右上角浮层：悬停出现，含收藏 / 删除 / 复选框（旧项目 .tile-actions + .tile-check）。 */
function CardActions({
  favorited,
  selected,
  onToggle,
  onFavorite,
  onDelete,
}: {
  favorited: boolean;
  selected: boolean;
  onToggle?: () => void;
  onFavorite?: () => void;
  onDelete?: () => void;
}) {
  const stop = (event: MouseEvent<HTMLElement>) => {
    event.stopPropagation();
    event.preventDefault();
  };

  return (
    <div className="absolute right-1.5 top-1.5 z-[2] flex items-center gap-1.5">
      {(onFavorite || onDelete) && (
        <div className="wall-actions flex gap-1.5">
          {onFavorite && (
            <button
              type="button"
              className="wall-action"
              title={favorited ? "已收藏" : "收藏"}
              aria-label={favorited ? "已收藏" : "收藏"}
              style={
                favorited
                  ? { color: "var(--color-accent)", borderColor: "var(--color-accent)" }
                  : undefined
              }
              onClick={(event) => {
                stop(event);
                onFavorite();
              }}
            >
              <Star size={11} fill={favorited ? "currentColor" : "none"} />
            </button>
          )}
          {onDelete && (
            <button
              type="button"
              className="wall-action wall-action-danger"
              title="删除"
              aria-label="删除"
              onClick={(event) => {
                stop(event);
                onDelete();
              }}
            >
              <Trash2 size={11} />
            </button>
          )}
        </div>
      )}

      {onToggle && (
        <button
          type="button"
          className="wall-check"
          data-checked={selected}
          role="checkbox"
          aria-checked={selected}
          aria-label="选择此作品"
          onClick={(event) => {
            stop(event);
            onToggle();
          }}
        >
          {selected && <Check size={12} />}
        </button>
      )}
    </div>
  );
}

export function ArtworkCard({
  card,
  layout = "grid",
  selected = false,
  onOpen,
  onToggle,
  onFavorite,
  onDelete,
  onContextMenu,
}: ArtworkCardProps) {
  const src = mediaSrc(card.cover_path);
  const title = card.title ?? `Pixiv ${card.pixiv_id}`;

  const handleClick = (event: MouseEvent<HTMLDivElement>) => {
    // Ctrl / Cmd 点击 = 切换选中；普通点击 = 打开查看器
    if (event.ctrlKey || event.metaKey) {
      onToggle?.(card);
      return;
    }
    onOpen?.(card);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === " ") {
      event.preventDefault();
      onToggle?.(card);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      onOpen?.(card);
    }
  };

  const shared = {
    role: "button" as const,
    tabIndex: 0,
    "data-selected": selected,
    onClick: handleClick,
    onKeyDown: handleKeyDown,
    onContextMenu: (event: MouseEvent<HTMLElement>) => onContextMenu?.(card, event),
    title,
  };

  const actions = (
    <CardActions
      favorited={card.is_favorited}
      selected={selected}
      onToggle={onToggle ? () => onToggle(card) : undefined}
      onFavorite={onFavorite ? () => onFavorite(card) : undefined}
      onDelete={onDelete ? () => onDelete(card) : undefined}
    />
  );

  // 瀑布流：图片按原始比例铺满，标题默认隐藏、悬停浮出（旧项目 .tile 的行为）
  if (layout === "masonry") {
    return (
      <div className="wall-tile" {...shared}>
        {src ? (
          <img src={src} alt="" loading="lazy" decoding="async" />
        ) : (
          <span className="wall-tile-placeholder">无缩略图</span>
        )}
        <Badges card={card} />
        {actions}
        <p className="wall-tile-title">{title}</p>
      </div>
    );
  }

  return (
    <div className="wall-card" {...shared}>
      <div className="relative aspect-square overflow-hidden rounded-thumb bg-surface-2">
        {src ? (
          <img
            src={src}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
            decoding="async"
          />
        ) : (
          <span className="absolute inset-0 flex items-center justify-center text-micro text-text-3">
            无缩略图
          </span>
        )}
        <Badges card={card} />
        {actions}
      </div>

      <div className="mt-2.5">
        <p className="truncate-1 text-label text-text">{title}</p>
        <p className="truncate-1 text-micro text-text-2">
          {card.author_name ?? "未知作者"}
        </p>
      </div>
    </div>
  );
}