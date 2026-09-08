(function () {
    'use strict';

    const STORAGE_KEY = 'saedam_sitemap_docked';
    const experience = document.getElementById('sitemapExperience');
    if (!experience) return;

    const groupsRoot = document.getElementById('sitemapGroups');
    const destination = document.getElementById('sitemapDestination');
    const menuToggle = experience.querySelector('[data-sitemap-menu-toggle]');
    const closeButton = experience.querySelector('[data-sitemap-close]');
    const dockButton = experience.querySelector('[data-sitemap-dock]');
    const clock = document.getElementById('sitemapClock');
    const title = document.getElementById('sitemapTitle');
    const brandKicker = experience.querySelector('.sitemap-brand small');
    const dockKicker = experience.querySelector('.sitemap-dock-copy small');
    const groupPalettes = {
        cosmic: ['#67e8f9', '#60a5fa', '#818cf8', '#a78bfa', '#f472b6', '#5eead4'],
        pastel: ['#72cbd4', '#80afea', '#969ee9', '#b19ae5', '#e495b3', '#76c7ae']
    };
    let lastFocused = null;
    let built = false;
    let closeTimer = 0;
    let currentVariant = 'cosmic';

    function textWithoutIcon(element) {
        const copy = element.cloneNode(true);
        copy.querySelectorAll('i').forEach(icon => icon.remove());
        return copy.textContent.trim().replace(/\s+/g, ' ');
    }

    function iconClass(element) {
        const icon = element.querySelector('i');
        return icon ? icon.className : 'fa-solid fa-circle-nodes';
    }

    function buildNavigation() {
        if (built) return;
        const sourceGroups = Array.from(document.querySelectorAll('.menu-list > .menu-item-wrapper'))
            .filter(item => {
                const link = item.querySelector(':scope > .menu-item');
                return link && textWithoutIcon(link) !== '메인메뉴';
            });

        sourceGroups.forEach((source, index) => {
            const mainLink = source.querySelector(':scope > .menu-item');
            if (!mainLink) return;

            const submenuLinks = Array.from(source.querySelectorAll(':scope > .dropdown-content > a'))
                .filter(link => !link.hasAttribute('data-sitemap-trigger'));
            const mainHref = mainLink.getAttribute('href') || '';
            if (!submenuLinks.length && mainHref && !mainHref.startsWith('javascript:')) submenuLinks.push(mainLink);
            if (!submenuLinks.length) return;

            const group = document.createElement('section');
            group.className = 'sitemap-group';
            group.style.setProperty('--group-color', groupPalettes.cosmic[index % groupPalettes.cosmic.length]);
            group.style.animationDelay = `${80 + index * 65}ms`;
            const label = textWithoutIcon(mainLink);
            const listId = `sitemap-submenus-${index}`;
            group.innerHTML = `
                <button class="sitemap-group-button" type="button" aria-expanded="false" aria-controls="${listId}">
                    <i class="${iconClass(mainLink)}"></i>
                    <span>${escapeHtml(label)}</span>
                    <small>SECTOR ${String(index + 1).padStart(2, '0')}</small>
                </button>
                <div class="sitemap-submenus" id="${listId}">
                    <div class="sitemap-submenus-inner"><div class="sitemap-submenu-list"></div></div>
                </div>`;

            const list = group.querySelector('.sitemap-submenu-list');
            const submenuPanel = group.querySelector('.sitemap-submenus');
            submenuPanel.inert = true;
            submenuPanel.setAttribute('aria-hidden', 'true');
            submenuLinks.forEach(link => {
                const item = document.createElement('button');
                item.type = 'button';
                item.className = 'sitemap-submenu';
                item.dataset.href = link.href;
                item.dataset.label = textWithoutIcon(link);
                item.innerHTML = `<i class="${iconClass(link)}"></i><span>${escapeHtml(item.dataset.label)}</span><i class="fa-solid fa-arrow-up-right-from-square"></i>`;
                item.addEventListener('click', () => launchDestination(item.dataset.href, item.dataset.label));
                list.appendChild(item);
            });

            group.querySelector('.sitemap-group-button').addEventListener('click', () => selectGroup(group, label));
            groupsRoot.appendChild(group);
        });
        built = true;
    }

    function setVariant(variant) {
        currentVariant = variant === 'pastel' ? 'pastel' : 'cosmic';
        const isPastel = currentVariant === 'pastel';
        experience.classList.toggle('is-pastel', isPastel);
        title.textContent = isPastel ? '사이트맵2' : '사이트맵';
        brandKicker.textContent = isPastel ? 'SAEDAM PASTEL NAVIGATION' : 'SAEDAM NAVIGATION SYSTEM';
        dockKicker.textContent = isPastel ? 'SITEMAP 2' : 'SITEMAP';
        const palette = groupPalettes[currentVariant];
        groupsRoot.querySelectorAll('.sitemap-group').forEach((group, index) => {
            group.style.setProperty('--group-color', palette[index % palette.length]);
        });
    }

    function escapeHtml(value) {
        const element = document.createElement('span');
        element.textContent = value;
        return element.innerHTML;
    }

    function selectGroup(group, label) {
        const willOpen = !group.classList.contains('is-active');
        groupsRoot.querySelectorAll('.sitemap-group').forEach(item => {
            item.classList.remove('is-active');
            item.querySelector('.sitemap-group-button').setAttribute('aria-expanded', 'false');
            const panel = item.querySelector('.sitemap-submenus');
            panel.inert = true;
            panel.setAttribute('aria-hidden', 'true');
        });
        if (willOpen) {
            group.classList.add('is-active');
            group.querySelector('.sitemap-group-button').setAttribute('aria-expanded', 'true');
            const panel = group.querySelector('.sitemap-submenus');
            panel.inert = false;
            panel.setAttribute('aria-hidden', 'false');
            destination.querySelector('strong').textContent = `${label} 업무 공간이 연결되었습니다. 이동할 화면을 선택하세요.`;
            window.setTimeout(() => group.scrollIntoView({ block: 'nearest', behavior: 'smooth' }), 180);
        } else {
            destination.querySelector('strong').textContent = '주메뉴를 선택하면 연결할 수 있는 화면이 나타납니다.';
        }
    }

    function openSitemap(openMenu, variant = currentVariant) {
        window.clearTimeout(closeTimer);
        buildNavigation();
        setVariant(variant);
        lastFocused = document.activeElement;
        experience.classList.remove('is-docked', 'is-closing', 'is-launching', 'is-arriving');
        experience.classList.add('is-visible');
        experience.setAttribute('aria-hidden', 'false');
        document.body.classList.add('sitemap-scroll-lock');
        if (openMenu) setMenuOpen(true);
        window.requestAnimationFrame(() => (openMenu ? groupsRoot.querySelector('.sitemap-group-button') : menuToggle)?.focus());
    }

    function closeSitemap() {
        experience.classList.add('is-closing');
        experience.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('sitemap-scroll-lock');
        sessionStorage.removeItem(STORAGE_KEY);
        closeTimer = window.setTimeout(() => {
            experience.className = 'sitemap-experience';
            setVariant('cosmic');
            setMenuOpen(false);
            if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
        }, 390);
    }

    function setMenuOpen(open) {
        experience.classList.toggle('is-menu-open', open);
        menuToggle.setAttribute('aria-expanded', String(open));
        menuToggle.querySelector('.sitemap-open-copy strong').textContent = open ? '메뉴 접기' : '메뉴 열기';
        experience.querySelector('.sitemap-navigation').setAttribute('aria-hidden', String(!open));
    }

    function launchDestination(href, label) {
        if (!href) return;
        destination.querySelector('strong').textContent = `${label} 화면으로 이동합니다.`;
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ href, label, variant: currentVariant, time: Date.now() }));
        experience.classList.add('is-launching');
        document.body.classList.remove('sitemap-scroll-lock');
        const launchDelay = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 640;
        window.setTimeout(() => { window.location.href = href; }, launchDelay);
    }

    function restoreDockedState() {
        let saved;
        try { saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null'); } catch (error) { saved = null; }
        if (!saved || Date.now() - Number(saved.time || 0) > 30 * 60 * 1000) {
            sessionStorage.removeItem(STORAGE_KEY);
            return;
        }
        buildNavigation();
        setVariant(saved.variant);
        experience.classList.add('is-arriving');
        experience.setAttribute('aria-hidden', 'false');
        window.setTimeout(() => {
            experience.classList.remove('is-arriving');
            experience.classList.add('is-docked');
        }, 860);
    }

    function updateClock() {
        if (!clock) return;
        clock.textContent = new Intl.DateTimeFormat('ko-KR', {
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        }).format(new Date());
    }

    document.querySelectorAll('[data-sitemap-trigger]').forEach(trigger => {
        trigger.addEventListener('click', event => {
            event.preventDefault();
            openSitemap(false, trigger.dataset.sitemapVariant);
        });
    });
    menuToggle.addEventListener('click', () => setMenuOpen(!experience.classList.contains('is-menu-open')));
    closeButton.addEventListener('click', closeSitemap);
    dockButton.addEventListener('click', () => openSitemap(true, currentVariant));

    experience.addEventListener('keydown', event => {
        if (event.key === 'Escape' && experience.classList.contains('is-visible')) closeSitemap();
        if (event.key !== 'Tab' || !experience.classList.contains('is-visible')) return;
        const focusable = Array.from(experience.querySelectorAll('button:not([disabled]), a[href]')).filter(el => el.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });

    updateClock();
    window.setInterval(updateClock, 1000);
    restoreDockedState();
})();
