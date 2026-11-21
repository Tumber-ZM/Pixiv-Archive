import { useLayoutEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";

import type { Layout } from "../../stores/ui";
import type { ArtworkCard as Card } from "../../types/ipc";
import { ArtworkCard } from "./ArtworkCard";

const MIN_COLUMN_WIDTH = 210;
const GAP = 16;
/** 缩略图下方标题行的预估高度 */
const LABEL_HEIGHT = 26;

interface WallGridProps {
  items: Card[];
  layout?: Layout;
  selected?: Set<number>;
  onOpen?: (card: Card) => void;
  onToggle?: (card: Card) => void;
  onFavorite?: (card: Card) => void;
  onDelete?: (card: Card) => void;
  onContextMenu?: (card: Card, event: MouseEvent<HTMLElement>) => void;
}

/**
 * 行优先分列：按顺序遍历，每次把卡片放进当前累计高度最矮的那一列。
 *
 * CSS 多列（`column-width`）是列优先——先填满第一列再填第二列，视觉上就是
 * 「从上往下再往右」，没法改成用户想要的「先从左往右铺满第一行」。
 * 所以这里自己算，布局靠 N 个 flex 列实现。
 */
function distribute(items: Card[], columns: number, columnWidth: number): Card[][] {
  const buckets: Card[][] = Array.from({ length: columns }, () => []);
  const heights = new Array<number>(columns).fill(0);

  for (const card of items) {
    const ratio =
      card.cover_width && card.cover_height ? card.cover_width / card.cover_height : 3 / 4;
    const estimated = columnWidth / ratio + LABEL_HEIGHT;

    let target = 0;
    for (let index = 1; index < columns; index += 1) {
      if (heights[index] < heights[target]) target = index;
    }

    buckets[target].push(card);
    heights[target] += estimated + GAP;
  }

  return buckets;
}

export function WallGrid({
  items,
  layout = "grid",
  selected,
  onOpen,
  onToggle,
  onFavorite,
  onDelete,
  onContextMenu,
}: WallGridProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useLayoutEffect(() => {
    if (layout !== "masonry") return;
    const element = containerRef.current;
    if (!element) return;

    const measure = () => setWidth(element.clientWidth);
    measure();

    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [layout]);

  const render = (card: Card) => (
    <ArtworkCard
      key={card.id}
      card={card}
      layout={layout}
      selected={selected?.has(card.id) ?? false}
      onOpen={onOpen}
      onToggle={onToggle}
      onFavorite={onFavorite}
      onDelete={onDelete}
      onContextMenu={onContextMenu}
    />
  );

  if (layout !== "masonry") {
    return <div className="grid-wall">{items.map(render)}</div>;
  }

  const columns =
    width > 0 ? Math.max(1, Math.floor((width + GAP) / (MIN_COLUMN_WIDTH + GAP))) : 0;
  const columnWidth = columns > 0 ? (width - GAP * (columns - 1)) / columns : 0;
  const buckets = columns > 0 ? distribute(items, columns, columnWidth) : [];

  return (
    <div ref={containerRef} className="flex items-start" style={{ gap: GAP }}>
      {buckets.map((bucket, index) => (
        <div key={index} className="flex min-w-0 flex-1 flex-col" style={{ gap: GAP }}>
          {bucket.map(render)}
        </div>
      ))}
    </div>
  );
}