import { useUi, type ScanResult } from "../../stores/ui";
import { Dialog } from "../dialog/Dialog";

const ROWS: ReadonlyArray<{ key: keyof ScanResult; label: string }> = [
  { key: "scanned", label: "扫描文件" },
  { key: "imported", label: "新入库" },
  { key: "duplicates", label: "已存在，跳过" },
  { key: "unsupported", label: "未识别，跳过" },
  { key: "thumbnails", label: "生成缩略图" },
  { key: "errors", label: "失败" },
];

/**
 * 扫描结束后的汇总。
 *
 * `unsupported` 仍保留一行是为了兼容旧记录：现在文件名里认不出作品 ID 的图片
 * 也会以「本地文件」身份入库，所以这一项恒为 0，不会再显示。
 */
export function ScanResultDialog() {
  const result = useUi((state) => state.scanResult);
  const setScanResult = useUi((state) => state.setScanResult);

  const close = () => setScanResult(null);

  return (
    <Dialog
      open={result !== null}
      title={result?.cancelled ? "扫描已取消" : "扫描完成"}
      confirmLabel="知道了"
      onCancel={close}
      onConfirm={close}
    >
      {result && (
        <>
          {result.cancelled && (
            <p className="mb-3 text-micro text-text-2">
              任务在完成前被取消，已入库的数据会保留。
            </p>
          )}

          <div className="flex flex-col">
            {ROWS.map(({ key, label }) => (
              <div
                key={key}
                className="flex items-baseline justify-between gap-3 border-b border-line py-1.5 last:border-b-0"
              >
                <span className="text-micro text-text-2">{label}</span>
                <span className="text-body text-text">{String(result[key])}</span>
              </div>
            ))}
          </div>

          {result.imported === 0 && result.duplicates > 0 && !result.cancelled && (
            <p className="mt-3 text-micro text-text-2">
              没有新作品——这些图之前已经入库过了。
            </p>
          )}
          {result.unsupported > 0 && (
            <p className="mt-3 text-micro text-text-3">
              未识别的原因是文件名里找不到 7–10 位的 Pixiv 作品 ID。
              这类文件不会入库，可以手动改名后再扫。
            </p>
          )}
        </>
      )}
    </Dialog>
  );
}