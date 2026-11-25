import { Heart, Images, RefreshCw, Trash2 } from "lucide-react";

import { useUi } from "../../stores/ui";

interface BatchBarProps {
  count: number;
  onSelectAll: () => void;
  onClear: () => void;
  onFavorite: () => void;
  onDelete: () => void;
  onRegenerateThumbnails: () => void;
  onSyncMetadata: () => void;
}

/** 批量操作条：选中 ≥1 件时从底部滑入（`06-界面规格.md` §10）。 */
export function BatchBar({
  count,
  onSelectAll,
  onClear,
  onFavorite,
  onDelete,
  onRegenerateThumbnails,
  onSyncMetadata,
}: BatchBarProps) {
  const sidebarCollapsed = useUi((state) => state.sidebarCollapsed);

  if (count <= 0) return null;

  return (
    <div
      className="fixed bottom-0 right-0 z-30 flex h-16 items-center gap-2 border-t border-line bg-surface px-6 transition-[left] duration-overlay ease-pa"
      // 底栏从侧栏右侧开始，避免盖住侧栏（侧栏宽度随折叠状态变化）
      style={{
        left: sidebarCollapsed ? "var(--sidebar-w-collapsed)" : "var(--sidebar-w)",
      }}
    >
      <span className="text-body text-text">已选 {count} 件</span>

      <div className="ml-6 flex items-center gap-2">
        <button type="button" className="btn" onClick={onSelectAll}>
          全选
        </button>
        <button type="button" className="btn inline-flex items-center gap-2" onClick={onFavorite}>
          <Heart size={14} />
          收藏
        </button>
        <button
          type="button"
          className="btn inline-flex items-center gap-2"
          onClick={onRegenerateThumbnails}
        >
          <Images size={14} />
          重生成缩略图
        </button>
        <button
          type="button"
          className="btn inline-flex items-center gap-2"
          onClick={onSyncMetadata}
        >
          <RefreshCw size={14} />
          重新同步元数据
        </button>
        <button
          type="button"
          className="btn btn-danger-solid inline-flex items-center gap-2"
          onClick={onDelete}
        >
          <Trash2 size={14} />
          删除
        </button>
        <button type="button" className="btn" onClick={onClear}>
          取消选择
        </button>
      </div>
    </div>
  );
}