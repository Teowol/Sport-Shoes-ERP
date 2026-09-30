(() => {
    'use strict';
    const trigger = document.querySelector('.module-menu-toggle');
    const drawer = document.querySelector('#module-drawer');
    if (!trigger || !drawer) return;

    const close = () => drawer.close();
    trigger.hidden = false;
    trigger.addEventListener('click', () => {
        drawer.showModal();
        drawer.scrollTop = 0;
        trigger.setAttribute('aria-expanded', 'true');
        document.documentElement.classList.add('module-menu-open');
    });
    drawer.querySelector('.module-drawer-close').addEventListener('click', close);
    // Keep Tab cycling through the menu; Escape is handled by the modal dialog.
    drawer.addEventListener('keydown', event => {
        if (event.key !== 'Tab') return;
        const controls = [...drawer.querySelectorAll('button, a[href]')];
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
    drawer.addEventListener('close', () => {
        trigger.setAttribute('aria-expanded', 'false');
        document.documentElement.classList.remove('module-menu-open');
        trigger.focus({ preventScroll: true });
    });
    let backdropPress = false;
    const outside = event => {
        const bounds = drawer.getBoundingClientRect();
        return event.clientX < bounds.left || event.clientX > bounds.right ||
            event.clientY < bounds.top || event.clientY > bounds.bottom;
    };
    drawer.addEventListener('pointerdown', event => { backdropPress = outside(event); });
    drawer.addEventListener('click', event => {
        if (backdropPress && outside(event)) close();
        backdropPress = false;
    });
    drawer.querySelectorAll('a[href]').forEach(link => {
        if (new URL(link.href).pathname === window.location.pathname) {
            link.setAttribute('aria-current', 'page');
        }
        link.addEventListener('click', event => {
            if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) close();
        });
    });
})();
