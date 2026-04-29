/** @type {import('tailwindcss').Config} */
export default {
  // Scan both the extension and the shared frontend components so reused
  // classes from ReportView, FindingCard, etc. are emitted into our build.
  content: [
    "./*.html",
    "./src/**/*.{ts,tsx}",
    "../frontend/src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        paper: "#f4f3ee",
        panel: "#ebe9e0",
        sunken: "#f1efe8",
        ink: "#2c2c2a",
        muted: "#5f5e5a",
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          "Roboto",
          "system-ui",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          '"SF Mono"',
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
