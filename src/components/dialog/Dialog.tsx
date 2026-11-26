import { useCallback, useEffect, useRef, type ReactNode } from "react";

interface DialogProps {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 可选的第三个按钮，例如「不保存」 */
  secondaryLabel?: string;
  onSecondary?: () => void;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 通用确认框。带 `role=dialog` / `aria-modal`、打开时聚焦首个可操作元素、
 * `Esc` 关闭、Tab 焦点锁在面板内（`05-设计系统.md` §7.6）。
 */
export function Dialog({
  open,
  title,
  children,
  confirmLabel = "确认",
  cancelLabel = "取消",
  secondaryLabel,
  onSecondary,
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  const focusables = useCallback((): HTMLElement[] => {
    const panel = panelRef.current;
    if (!panel) return [];
    return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
  }, []);

  useEffect(() => {
    if (!open) return;

    focusables()[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusables();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onCancel, focusables]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "var(--color-overlay)" }}
      onClick={onCancel}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-[440px] max-w-[92vw] rounded-card border border-line bg-surface p-6"
        onClick={(event) => event.stopPropagation()}
      >
        <p className="text-subheading text-text">{title}</p>
        {children && <div className="mt-3 text-body text-text-2">{children}</div>}

        <div className="mt-6 flex justify-end gap-2">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          {secondaryLabel && onSecondary && (
            <button type="button" className="btn" onClick={onSecondary} disabled={busy}>
              {secondaryLabel}
            </button>
          )}
          <button
            type="button"
            className={danger ? "btn btn-danger" : "btn"}
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}