/** Tailwind v3 config for the panel UI. Regenerate static/css/tailwind.css with packaging/build-css.sh. */
module.exports = {
  // The dashboard toggles the "dark" class on <html> (Settings > Theme)
  darkMode: 'class',
  content: {
    relative: true,
    // app.js builds markup in template strings, so it is scanned too
    files: ['../../static/**/*.html', '../../static/js/**/*.js'],
  },
  theme: { extend: {} },
  plugins: [],
};
