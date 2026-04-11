/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/**/*.js",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        carbon: {
          50: '#f5f5f5',
          100: '#e0e0e0',
          200: '#a0a0a0',
          300: '#666666',
          400: '#333333',
          500: '#2a2a2a',
          600: '#1e1e1e',
          700: '#1a1a1a',
          800: '#161616',
          900: '#111111',
          950: '#0f0f0f',
        },
        amber: {
          DEFAULT: '#f59e0b',
          light: '#fbbf24',
          dim: '#b45309',
        },
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"SF Mono"', '"Fira Code"', '"Cascadia Code"', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
