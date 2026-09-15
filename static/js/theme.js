/* Shared by application pages and Django admin. No dependency on authentication or language. */
(() => {
    'use strict';
    const storageKey = 'speeders-theme';
    const root = document.documentElement;
    const system = window.matchMedia('(prefers-color-scheme: dark)');
    const valid = value => value === 'light' || value === 'dark';
    let preference = null;
    try {
        const saved = localStorage.getItem(storageKey);
        if (valid(saved)) preference = saved;
    } catch (_) { /* Private browsing may disable storage; switching still works. */ }

    function updateControls() {
        document.querySelectorAll('[data-theme-toggle]').forEach(button => {
            const label = root.dataset.theme === 'dark' ? button.dataset.labelLight : button.dataset.labelDark;
            button.setAttribute('aria-label', label);
            button.title = label;
            button.hidden = false;
        });
    }

    function applyTheme(theme) {
        root.dataset.theme = theme;
        updateControls();
        window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    }

    function setTheme(theme) {
        if (!valid(theme)) return;
        preference = theme;
        try { localStorage.setItem(storageKey, theme); } catch (_) { /* Keep the in-page preference. */ }
        applyTheme(theme);
    }

    const getCurrentTheme = () => root.dataset.theme;
    const toggleTheme = () => setTheme(getCurrentTheme() === 'dark' ? 'light' : 'dark');
    window.SpeedersTheme = Object.freeze({ setTheme, toggleTheme, getCurrentTheme });
    applyTheme(preference || (system.matches ? 'dark' : 'light'));

    system.addEventListener('change', event => {
        if (!preference) applyTheme(event.matches ? 'dark' : 'light');
    });
    window.addEventListener('storage', event => {
        if (event.key !== storageKey && event.key !== null) return;
        preference = valid(event.newValue) ? event.newValue : null;
        applyTheme(preference || (system.matches ? 'dark' : 'light'));
    });
    document.addEventListener('click', event => {
        if (event.target.closest('[data-theme-toggle]')) toggleTheme();
    });
    document.addEventListener('DOMContentLoaded', () => {
        // The base renders one control. Each page identifies its existing toolbar.
        const control = document.querySelector('[data-theme-control]');
        const toolbar = document.querySelector('[data-theme-toolbar]');
        if (control && toolbar) toolbar.append(control);
        updateControls();
    });
})();
