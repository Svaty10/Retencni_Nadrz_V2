# Accessibility Statement

**Retenční nádrž V2** is committed to providing a web interface (`server/static/`) that is accessible and usable across desktop, tablet, and mobile devices, regardless of assistive technology or physical capability.

## Conformance & Design Measures

- **Semantic HTML & Landmarks:** The single-page application (`index.html`, `login.html`, `setup.html`) uses semantic landmarks (`<header>`, `<nav>`, `<main>`, `<section>`, `<dialog>`), explicit `<label>` associations, and `aria-label` / `aria-live` attributes on dynamic controls and status alerts.
- **Keyboard Navigation & Focus Management:**
  - All interactive controls, tabs, and actions are reachable via keyboard (`Tab` / `Shift+Tab`, `Enter`, `Escape`).
  - Modal dialogs (`openDialog` in `server/static/js/app.js`) implement focus trapping, automatic initial focus, and `Escape` dismissal.
  - Visible `:focus-visible` outlines are preserved across all interactive elements.
- **Color Contrast & Themes:**
  - High-contrast light and dark themes (`server/static/css/style.css` and `server/static/js/theme.js`) respect system `prefers-color-scheme` preferences and can be overridden per user account.
  - Status indicators pair color with text labels and icons so information is never conveyed by color alone.
- **Responsive & Touch-Friendly Layout:**
  - Layouts adapt cleanly down to compact mobile viewports without horizontal scrolling of primary content.
  - Interactive buttons and navigation controls maintain touch-friendly target dimensions.

## Known Limitations

- Historical charts rendered on `<canvas>` via Chart.js have summary metrics displayed as accessible DOM text in the surrounding cards (`Stav nádrže`, `Přehled`, and `Spojení`), though individual canvas data points are primarily visual.

## Reporting Accessibility Issues

If you encounter an accessibility barrier in the web interface or documentation, please [open an issue](https://github.com/Svaty10/Retencni_Nadrz_V2/issues/new/choose) describing the screen, browser/assistive technology used, and the barrier encountered.
