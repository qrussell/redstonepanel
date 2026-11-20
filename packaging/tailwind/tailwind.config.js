/** Tailwind v3 config for the panel UI. Regenerate static/css/tailwind.css with packaging/build-css.sh. */

// Redstone branding. "gray" is replaced by warm stone greys, so every existing gray-* class
// (cards, borders, text, the dark theme's backgrounds) picks up the stone look. "redstone" is
// the brand red: 500 is the logo's signal colour, 400 its glow on dark backgrounds.
// The accent colours (buttons, tabs, focus rings) are CSS variables in input.css.
const stone = {
  50: '#f7f6f5',
  100: '#eeecea',
  200: '#dcd9d6',
  300: '#c0bcb8',
  400: '#99948f',
  500: '#736e69',
  600: '#57534f',
  700: '#3f3c39',
  800: '#2b2927',
  900: '#1b1a19',
  950: '#121110',
};
const redstone = {
  50: '#fff2f0',
  100: '#ffe0dc',
  200: '#ffc1b9',
  300: '#ff9184',
  400: '#ff5a4a',
  500: '#ff3b2f',
  600: '#d9261b',
  700: '#b31d14',
  800: '#8c1a13',
  900: '#651611',
  950: '#3a0906',
};

module.exports = {
  // The dashboard toggles the "dark" class on <html> (Settings > Theme)
  darkMode: 'class',
  content: {
    relative: true,
    // app.js builds markup in template strings, so it is scanned too
    files: ['../../static/**/*.html', '../../static/js/**/*.js'],
  },
  theme: {
    extend: {
      colors: { gray: stone, redstone },
    },
  },
  plugins: [],
};
