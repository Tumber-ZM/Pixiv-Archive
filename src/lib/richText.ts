/**
 * 清洗 Pixiv 的描述 HTML。
 *
 * 描述来自 Pixiv，是可以含 `<br>` / `<a>` 的富文本。直接当纯文本显示会露出标签，
 * 直接 `dangerouslySetInnerHTML` 又等于把第三方 HTML 塞进 WebView。
 * 这里用 DOMParser 解析后按白名单重建：文本一律转义，只保留换行、段落、
 * 链接和少量强调标签，链接只接受 http(s)。
 */
export function sanitizeRichText(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");

  const escape = (text: string) =>
    text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  const walk = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return escape(node.textContent ?? "");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";

    const element = node as HTMLElement;
    const inner = Array.from(element.childNodes).map(walk).join("");

    switch (element.tagName) {
      case "BR":
        return "<br />";
      case "P":
        return `<p>${inner}</p>`;
      case "A": {
        const href = element.getAttribute("href") ?? "";
        // 只放行 http(s)，挡住 javascript: 之类的伪协议
        if (!/^https?:\/\//i.test(href)) return inner;
        return `<a href="${escape(href)}" rel="noreferrer noopener">${inner}</a>`;
      }
      case "STRONG":
      case "B":
        return `<strong>${inner}</strong>`;
      case "EM":
      case "I":
        return `<em>${inner}</em>`;
      default:
        // 其余标签连同属性一起丢掉，只留内容
        return inner;
    }
  };

  return Array.from(doc.body.childNodes).map(walk).join("");
}