/** 骨架屏：6 个 1:1 灰块，静态 opacity 0.6，不做闪烁动画（`06-界面规格.md` §3）。 */
export function SkeletonGrid({ count = 6 }: { count?: number }) {
  return (
    <div className="grid-wall">
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="aspect-square rounded-thumb bg-surface-2 opacity-60" />
      ))}
    </div>
  );
}