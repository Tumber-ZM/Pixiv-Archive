import { ChevronLeft, ChevronRight } from "lucide-react";

interface PaginationProps {
  page: number;
  totalPages: number;
  total: number;
  onChange: (page: number) => void;
}

/** 首尾各 1 页 + 当前页左右各 1 页，其余用省略号折叠。 */
function pageItems(page: number, totalPages: number): (number | "ellipsis")[] {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  const wanted = [1, totalPages, page - 1, page, page + 1].filter(
    (p) => p >= 1 && p <= totalPages,
  );
  const unique = [...new Set(wanted)].sort((a, b) => a - b);

  const out: (number | "ellipsis")[] = [];
  let previous = 0;
  for (const current of unique) {
    if (previous && current - previous > 1) out.push("ellipsis");
    out.push(current);
    previous = current;
  }
  return out;
}

export function Pagination({ page, totalPages, total, onChange }: PaginationProps) {
  if (totalPages <= 1) {
    return (
      <div className="flex h-14 items-center justify-center text-micro text-text-2">
        共 {total} 件
      </div>
    );
  }

  return (
    <div className="flex h-14 items-center justify-center gap-1">
      <button
        type="button"
        className="page-btn"
        disabled={page <= 1}
        onClick={() => onChange(page - 1)}
        aria-label="上一页"
      >
        <ChevronLeft size={14} />
      </button>

      {pageItems(page, totalPages).map((item, index) =>
        item === "ellipsis" ? (
          <span key={`gap-${index}`} className="px-1 text-micro text-text-3">
            …
          </span>
        ) : (
          <button
            key={item}
            type="button"
            className="page-btn"
            data-active={item === page}
            onClick={() => onChange(item)}
          >
            {item}
          </button>
        ),
      )}

      <button
        type="button"
        className="page-btn"
        disabled={page >= totalPages}
        onClick={() => onChange(page + 1)}
        aria-label="下一页"
      >
        <ChevronRight size={14} />
      </button>

      <span className="ml-3 text-micro text-text-2">共 {total} 件</span>
    </div>
  );
}