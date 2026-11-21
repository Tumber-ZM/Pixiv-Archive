import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

export interface MenuItem {
  label: string;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
  /** 该项之前画一条分隔线 */
  separatorBefore?: boolean;
}

interface ContextMenuProps {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
}

const MENU_WIDTH = 168;
const ITEM_HEIGHT = 30;
const SEPARATOR_HEIGHT = 9;

/**
 * 右键菜单。挂在 body 上，避免被容器的 overflow 裁掉；
 * 贴边时向内收，保证整块可见。无阴影，靠 1px 线与底色分层。
 */
export function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (ref.current?.contains(event.target as Node)) return;
      onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // 阻止冒泡，否则会连带关掉下面的查看器
      event.stopPropagation();
      onClose();
    };

    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("blur", onClose);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("blur", onClose);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  const height = items.reduce(
    (total, item) => total + ITEM_HEIGHT + (item.separatorBefore ? SEPARATOR_HEIGHT : 0),
    8,
  );
  const left = Math.max(4, Math.min(x, window.innerWidth - MENU_WIDTH - 4));
  const top = Math.max(4, Math.min(y, window.innerHeight - height - 4));

  return createPortal(
    <div
      ref={ref}
      className="ctx-menu"
      style={{ left, top }}
      role="menu"
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((item, index) => (
        <div key={`${item.label}-${index}`}>
          {item.separatorBefore && <div className="ctx-sep" />}
          <button
            type="button"
            role="menuitem"
            className="ctx-item"
            data-danger={item.danger}
            disabled={item.disabled}
            onClick={() => {
              item.onSelect();
              onClose();
            }}
          >
            {item.label}
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}