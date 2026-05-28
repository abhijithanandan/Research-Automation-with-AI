// Tailwind v4 uses the dedicated PostCSS plugin (not the tailwindcss package
// directly). Per https://tailwindcss.com/docs/installation/using-postcss —
// the autoprefixer line is kept for Next 14 since Next's compiler doesn't
// vendor-prefix the @theme-emitted custom properties.
module.exports = {
  plugins: {
    "@tailwindcss/postcss": {},
    autoprefixer: {},
  },
};
