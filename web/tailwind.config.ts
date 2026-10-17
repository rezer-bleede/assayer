import type { Config } from 'tailwindcss';

const config: Config = {
  darkMode: 'class',
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'rgb(var(--bg) / <alpha-value>)',
        fg: 'rgb(var(--fg) / <alpha-value>)',
        muted: 'rgb(var(--muted) / <alpha-value>)',
        border: 'rgb(var(--border) / <alpha-value>)',
        surface: 'rgb(var(--surface) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        pass: 'rgb(var(--pass) / <alpha-value>)',
        fail: 'rgb(var(--fail) / <alpha-value>)',
        warn: 'rgb(var(--warn) / <alpha-value>)',
        // `ink` tracks the text color, so a low-alpha ink fill reads as a
        // subtle tint in both light and dark mode.
        ink: 'rgb(var(--fg) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', 'Inter', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
      },
      animation: {
        'fade-up': 'fade-up .28s cubic-bezier(.16,1,.3,1) both',
      },
    },
  },
  plugins: [],
};

export default config;