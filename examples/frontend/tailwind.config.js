/** @type {import('tailwindcss').Config} */
// Every color here resolves to a CSS custom property defined in
// src/styles/tokens.css — that file is the only place color VALUES
// live. This config just names them as Tailwind utilities
// (bg-surface-1, text-text-secondary, border-attention, etc.).
function withOpacity(variable) {
  return `rgb(var(${variable}) / <alpha-value>)`;
}

export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: withOpacity("--color-bg"),
        surface: {
          1: withOpacity("--color-surface-1"),
          2: withOpacity("--color-surface-2"),
          3: withOpacity("--color-surface-3"),
        },
        border: {
          DEFAULT: withOpacity("--color-border"),
          strong: withOpacity("--color-border-strong"),
        },
        text: {
          primary: withOpacity("--color-text-primary"),
          secondary: withOpacity("--color-text-secondary"),
          tertiary: withOpacity("--color-text-tertiary"),
          onaccent: withOpacity("--color-text-on-accent"),
        },
        accent: {
          DEFAULT: withOpacity("--color-accent"),
          hover: withOpacity("--color-accent-hover"),
          soft: withOpacity("--color-accent-soft"),
        },
        attention: {
          DEFAULT: withOpacity("--color-attention"),
          soft: withOpacity("--color-attention-soft"),
          fg: withOpacity("--color-attention-fg"),
        },
        success: withOpacity("--color-success"),
        error: withOpacity("--color-error"),
        warning: withOpacity("--color-warning"),
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
      },
    },
  },
  plugins: [],
};
