import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  title: string;
  hint?: string;
  icon?: LucideIcon;
  children?: ReactNode;
}

/** 空态：垂直居中，最大宽 400px（`05-设计系统.md` §7.9）。 */
export function EmptyState({ title, hint, icon: Icon, children }: EmptyStateProps) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-8 py-16">
      {Icon && <Icon size={24} className="text-text-3 opacity-60" />}
      <p className="max-w-[400px] text-center text-subheading text-text">{title}</p>
      {hint && <p className="max-w-[400px] text-center text-body text-text-2">{hint}</p>}
      {children}
    </div>
  );
}