import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

/** 供查看器把键盘 `+` / `-` 转成缩放操作 */
export interface ViewerImageHandle {
  zoomIn: () => void;
  zoomOut: () => void;
}

interface ViewerImageProps {
  src: string | null;
  pageIndex: number;
  pageCount: number;
  onPageChange: (page: number) => void;
  onPrevArtwork: () => void;
  onNextArtwork: () => void;
  canPrevArtwork: boolean;
  canNextArtwork: boolean;
  ref?: Ref<ViewerImageHandle>;
}

const MIN_SCALE = 0.2;
const MAX_SCALE = 5;
/** 滚轮与加减号的单步缩放倍率 */
const ZOOM_STEP = 1.25;
/** 拖出边界时的阻尼系数：实际位移 = 边界 + 超出量 × 该系数 */
const OVERSCROLL_DAMP = 0.35;
const SETTLE_MS = 260;

interface Metrics {
  /** 图片当前（已应用 transform）的渲染尺寸 */
  scaledW: number;
  scaledH: number;
  boxW: number;
  boxH: number;
}

/**
 * 把偏移限制在「图片还能看见」的范围内。
 * `damp` 为 0 时是硬边界（回弹用），大于 0 时允许橡皮筋式越界。
 */
function clampOffset(
  x: number,
  y: number,
  metrics: Metrics,
  damp: number,
): { x: number; y: number } {
  // 图片比容器小：余量是居中留白；比容器大：余量是可平移距离
  const limitX = Math.abs(metrics.boxW - metrics.scaledW) / 2;
  const limitY = Math.abs(metrics.boxH - metrics.scaledH) / 2;

  const apply = (value: number, limit: number) => {
    const overflow = Math.abs(value) - limit;
    if (overflow <= 0) return value;
    return Math.sign(value) * (limit + overflow * damp);
  };

  return { x: apply(x, limitX), y: apply(y, limitY) };
}

/**
 * 图片区：滚轮 / 加减号缩放 0.2×–5×、拖拽平移（带弹性边界）、双击复位（`06-界面规格.md` §8）。
 */
export function ViewerImage({
  src,
  pageIndex,
  pageCount,
  onPageChange,
  onPrevArtwork,
  onNextArtwork,
  canPrevArtwork,
  canNextArtwork,
  ref,
}: ViewerImageProps) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [settling, setSettling] = useState(false);

  const boxRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const dragOrigin = useRef<{ x: number; y: number } | null>(null);
  const metricsRef = useRef<Metrics | null>(null);
  // 松手回弹时需要用「当前真实偏移」，而 state 在闭包里是渲染快照
  const offsetRef = useRef(offset);
  offsetRef.current = offset;

  const measure = () => {
    const img = imgRef.current;
    const box = boxRef.current;
    if (!img || !box) return null;
    const imgRect = img.getBoundingClientRect();
    const boxRect = box.getBoundingClientRect();
    return {
      scaledW: imgRect.width,
      scaledH: imgRect.height,
      boxW: boxRect.width,
      boxH: boxRect.height,
    };
  };

  const reset = () => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  };

  /** 按比例缩放，夹在 MIN_SCALE–MAX_SCALE 之间 */
  const zoomBy = (factor: number) => {
    setScale((current) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, current * factor)));
  };

  useImperativeHandle(ref, () => ({
    zoomIn: () => {
      if (src) zoomBy(ZOOM_STEP);
    },
    zoomOut: () => {
      if (src) zoomBy(1 / ZOOM_STEP);
    },
  }));

  // 换图必须复位，否则上一张的缩放会带到下一张
  useEffect(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
    metricsRef.current = null;
  }, [src]);

  // 缩放后可能越界，压回边界内。
  // 必须等一帧：此刻 React 还没把新 transform 写进 DOM，直接量会拿到旧尺寸。
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const metrics = measure();
      if (!metrics) return;
      metricsRef.current = metrics;
      setOffset((current) => clampOffset(current.x, current.y, metrics, 0));
    });
    return () => cancelAnimationFrame(frame);
  }, [scale]);

  // 窗口尺寸变化后重新测量
  useEffect(() => {
    const onResize = () => {
      const metrics = measure();
      if (!metrics) return;
      metricsRef.current = metrics;
      setOffset((current) => clampOffset(current.x, current.y, metrics, 0));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // 回弹动画结束后关掉过渡，避免影响正常拖动的手感
  useEffect(() => {
    if (!settling) return;
    const timer = setTimeout(() => setSettling(false), SETTLE_MS);
    return () => clearTimeout(timer);
  }, [settling]);

  const onWheel = (event: React.WheelEvent<HTMLDivElement>) => {
    if (!src) return;
    zoomBy(event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
  };

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    // 1:1 原尺寸时图片正好贴合，没有可拖动的余量；缩放比不等于 1 就能拖
    if (scale === 1) return;
    metricsRef.current = measure();
    dragOrigin.current = { x: event.clientX - offset.x, y: event.clientY - offset.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const origin = dragOrigin.current;
    const metrics = metricsRef.current;
    if (!origin || !metrics) return;

    const raw = { x: event.clientX - origin.x, y: event.clientY - origin.y };
    setOffset(clampOffset(raw.x, raw.y, metrics, OVERSCROLL_DAMP));
  };

  const onPointerUp = () => {
    const origin = dragOrigin.current;
    dragOrigin.current = null;
    if (!origin) return;

    const metrics = metricsRef.current;
    if (!metrics) return;

    // 松手时把橡皮筋拉回硬边界，并启用过渡做回弹
    const settled = clampOffset(offsetRef.current.x, offsetRef.current.y, metrics, 0);
    if (settled.x !== offsetRef.current.x || settled.y !== offsetRef.current.y) {
      setSettling(true);
      setOffset(settled);
    }
  };

  return (
    <div
      ref={boxRef}
      className="relative flex h-full min-w-0 flex-1 items-center justify-center overflow-hidden"
      style={{ background: "var(--viewer-bg)" }}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={reset}
    >
      {src ? (
        <img
          ref={imgRef}
          src={src}
          alt=""
          draggable={false}
          onLoad={() => {
            metricsRef.current = measure();
          }}
          className="select-none"
          style={{
            maxWidth: "calc(100% - 128px)",
            maxHeight: "calc(100% - 128px)",
            objectFit: "contain",
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
            transition: settling ? `transform ${SETTLE_MS}ms var(--ease)` : "none",
            cursor: scale === 1 ? "default" : "grab",
          }}
        />
      ) : (
        <p className="text-body" style={{ color: "var(--viewer-text-2)" }}>
          无图片
        </p>
      )}

      <button
        type="button"
        className="viewer-nav absolute left-3 top-1/2 -translate-y-1/2 disabled:pointer-events-none"
        style={{ color: "var(--viewer-text)" }}
        onClick={onPrevArtwork}
        disabled={!canPrevArtwork}
        aria-label="上一个作品"
        title="上一个作品（←）"
      >
        <ChevronLeft size={20} />
      </button>
      <button
        type="button"
        className="viewer-nav absolute right-3 top-1/2 -translate-y-1/2 disabled:pointer-events-none"
        style={{ color: "var(--viewer-text)" }}
        onClick={onNextArtwork}
        disabled={!canNextArtwork}
        aria-label="下一个作品"
        title="下一个作品（→）"
      >
        <ChevronRight size={20} />
      </button>

      {pageCount > 1 && (
        <div className="absolute bottom-5 left-1/2 flex -translate-x-1/2 items-center gap-2">
          <button
            type="button"
            className="icon-btn"
            style={{ color: "var(--viewer-text-2)" }}
            onClick={() => onPageChange(pageIndex - 1)}
            disabled={pageIndex <= 0}
            aria-label="上一页"
          >
            <ChevronLeft size={14} />
          </button>
          <span
            className="text-label"
            style={{ color: "var(--viewer-text-2)", letterSpacing: "0.1em" }}
          >
            {pageIndex + 1} / {pageCount}
          </span>
          <button
            type="button"
            className="icon-btn"
            style={{ color: "var(--viewer-text-2)" }}
            onClick={() => onPageChange(pageIndex + 1)}
            disabled={pageIndex >= pageCount - 1}
            aria-label="下一页"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  );
}