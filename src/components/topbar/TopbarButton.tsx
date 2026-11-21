import type { ReactNode } from "react";

interface TopbarButtonProps {
  children: ReactNode;
  onClick?: () => void;
  active?: boolean;
  disabled?: boolean;
  title?: string;
  className?: string;
  ariaExpanded?: boolean;
}

/** 顶栏统一的小按钮：随机、排序、R18/AI/GIF、已隐藏、作者列表的两个动作都用它。 */
export function TopbarButton({
  children,
  onClick,
  active,
  disabled,
  title,
  className,
  ariaExpanded,
}: TopbarButtonProps) {
  return (
    <button
      type="button"
      className={`tri-btn ${className ?? ""}`.trim()}
      data-active={active}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-expanded={ariaExpanded}
    >
      {children}
    </button>
  );
}
