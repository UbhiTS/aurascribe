/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  "#f0f4ff",
          100: "#e0e9ff",
          200: "#c7d7fd",
          300: "#a4bbfc",
          400: "#7c96f8",
          500: "#5a72f2",
          600: "#4355e6",
          700: "#3744cc",
          800: "#2e39a5",
          900: "#2a3582",
          950: "#1a204f",
        },
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
    },
  },
  plugins: [require("@tailwindcss/typography")],
}

