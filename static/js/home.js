(() => {
    const header = document.querySelector('.site-header');
    const toggle = document.querySelector('.menu-toggle');
    const nav = document.querySelector('#main-nav');
    const mobile = window.matchMedia('(max-width: 900px)');

    function closeMenu() {
        toggle.setAttribute('aria-expanded', 'false');
        nav.classList.remove('is-open');
    }

    toggle.hidden = false;
    header.classList.add('has-menu');
    toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', String(open));
        nav.classList.toggle('is-open', open);
    });
    nav.addEventListener('click', (event) => {
        if (event.target.closest('a') && mobile.matches) closeMenu();
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
            closeMenu();
            toggle.focus();
        }
    });
    document.addEventListener('click', (event) => {
        if (!header.contains(event.target)) closeMenu();
    });
    header.addEventListener('focusout', (event) => {
        if (!header.contains(event.relatedTarget)) closeMenu();
    });
    mobile.addEventListener('change', closeMenu);
    const updateHeader = () => header.classList.toggle('is-scrolled', window.scrollY > 16);
    window.addEventListener('scroll', updateHeader, { passive: true });
    updateHeader();
})();
