import { useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface TooltipProps {
  /** 悬浮说明文本 */
  text: string;
  children: ReactNode;
}

const MAX_WIDTH = 300;
const GAP = 8;
/** 气泡高度按常见文案估算，用来决定向下还是向上展开 */
const ESTIMATED_HEIGHT = 84;

/**
 * 标题悬浮提示：鼠标悬停 / 键盘聚焦时在下方展开说明。
 *
 * 气泡挂到 body 上（fixed 定位），否则会被设置面板的 overflow-y-auto 裁掉；
 * 左右与上下都做边界收敛，贴边时不会出屏。
 */
export function Tooltip({ text, children }: TooltipProps) {
  const anchor = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  const show = () => {
    const rect = anchor.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.max(GAP, Math.min(rect.left, window.innerWidth - MAX_WIDTH - GAP));
    const below = rect.bottom + GAP;
    const top =
      below + ESTIMATED_HEIGHT > window.innerHeight
        ? Math.max(GAP, rect.top - GAP - ESTIMATED_HEIGHT)
        : below;
    setPos({ left, top });
  };

  return (
    <>
      <span
        ref={anchor}
        tabIndex={0}
        className="inline-flex cursor-help items-center self-start underline decoration-dotted decoration-line-hover underline-offset-4"
        style={{ textDecorationColor: "var(--color-text-3)" }}
        onMouseEnter={show}
        onMouseLeave={() => setPos(null)}
        onFocus={show}
        onBlur={() => setPos(null)}
      >
        {children}
      </span>
      {pos &&
        createPortal(
          <div
            role="tooltip"
            className="pointer-events-none fixed z-[60] rounded-card border border-line bg-surface-2 px-3 py-2 text-micro text-text-2"
            style={{ left: pos.left, top: pos.top, maxWidth: MAX_WIDTH }}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}