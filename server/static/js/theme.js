// Motiv ještě před vykreslením stránky (bez probliknutí): poslední volba z tohoto
// prohlížeče; skutečná volba uživatele přijde z /api/me a app.js ji uloží sem.
(function () {
    try {
        var t = localStorage.getItem('theme');
        if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    } catch (e) { /* bez úložiště: podle zařízení */ }
})();
