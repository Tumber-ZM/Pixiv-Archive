import type { Config } from "tailwindcss";

/**
 * 所有颜色映射到 CSS 变量（tokens.css）。
 * 组件里只允许写语义色名（bg-surface / text-text-2 / border-line），
 * 不允许写 text-[#4a4a46] 这类字面量。
 */
const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        paper: "var(--color-paper)",
        surface: {
          DEFAULT: "var(--color-surface)",
          2: "var(--color-surface-2)",
          3: "var(--color-surface-3)",
        },
        text: {
          DEFAULT: "var(--color-text)",
          2: "var(--color-text-2)",
          3: "var(--color-text-3)",
        },
        line: {
          DEFAULT: "var(--color-line)",
          hover: "var(--color-line-hover)",
        },
        accent: {
          DEFAULT: "var(--color-accent)",
          soft: "var(--color-accent-soft)",
        },
        mint: { DEFAULT: "var(--color-mint)", soft: "var(--color-mint-soft)" },
        sakura: "var(--color-sakura)",
        mist: "var(--color-mist)",
        danger: { DEFAULT: "var(--color-danger)", soft: "var(--color-danger-soft)" },
        warn: "var(--color-warn)",
        ai: { DEFAULT: "var(--color-ai)", soft: "var(--color-ai-soft)" },
        overlay: "var(--color-overlay)",
      },

      fontFamily: {
        sans: ["var(--font-sans)"],
      },

      fontSize: {
        display: ["var(--text-display)", { lineHeight: "var(--lh-display)", fontWeight: "var(--fw-display)", letterSpacing: "var(--ls-display)" }],
        heading: ["var(--text-heading)", { lineHeight: "var(--lh-heading)", fontWeight: "var(--fw-heading)" }],
        subheading: ["var(--text-subheading)", { lineHeight: "var(--lh-subheading)", fontWeight: "var(--fw-subheading)" }],
        "body-lg": ["var(--text-body-lg)", { lineHeight: "var(--lh-body-lg)", fontWeight: "var(--fw-body-lg)" }],
        body: ["var(--text-body)", { lineHeight: "var(--lh-body)", fontWeight: "var(--fw-body)" }],
        label: ["var(--text-label)", { lineHeight: "var(--lh-label)", fontWeight: "var(--fw-label)", letterSpacing: "var(--ls-label)" }],
        micro: ["var(--text-micro)", { lineHeight: "var(--lh-micro)", fontWeight: "var(--fw-micro)", letterSpacing: "var(--ls-micro)" }],
        badge: ["var(--text-badge)", { lineHeight: "var(--lh-badge)", fontWeight: "var(--fw-badge)", letterSpacing: "var(--ls-badge)" }],
      },

      fontWeight: {
        display: "var(--fw-display)",
        heading: "var(--fw-heading)",
        body: "var(--fw-body)",
        badge: "var(--fw-badge)",
      },

      // 间距：不覆盖 Tailwind 默认数字刻度。
      // 默认已是 4px 基准（p-1=4, p-2=8, p-3=12, p-4=16, p-6=24, p-8=32, p-12=48, p-16=64, p-24=96），
      // 与 tokens.css 的 --s-1..--s-9 一一对应。覆盖数字会让 p-10 之类的语义静默改变，不要做。
      // 这里只补布局专用的具名尺寸。
      spacing: {
        sidebar: "var(--sidebar-w)",
        "sidebar-min": "var(--sidebar-w-collapsed)",
        topbar: "var(--topbar-h)",
        content: "var(--content-max)",
        "grid-gap": "var(--grid-gap)",
      },

      borderRadius: {
        card: "var(--radius-card)",
        btn: "var(--radius-btn)",
        input: "var(--radius-input)",
        thumb: "var(--radius-thumb)",
        badge: "var(--radius-badge)",
        full: "var(--radius-full)",
      },

      transitionDuration: {
        hover: "var(--dur-hover)",
        overlay: "var(--dur-overlay)",
        progress: "var(--dur-progress)",
      },

      transitionTimingFunction: {
        pa: "var(--ease)",
      },

      boxShadow: {
        none: "none",
      },

      gridTemplateColumns: {
        wall: "repeat(auto-fill, minmax(var(--grid-min), 1fr))",
      },

      screens: {
        sm: "640px",
        md: "900px",
        lg: "1280px",
        xl: "1600px",
      },
    },
  },
  plugins: [],
};

export default config;