/* ==========================================================================
 * Retenční Nádrž – klientská aplikace
 * Vanilla JS, žádné inline handlery (CSP). Grafy kreslí prohlížeč (Chart.js),
 * server posílá jen předem zprůměrovaná data.
 * ========================================================================== */
(function () {
    'use strict';

    // ------------------------------------------------------------------ util
    const $ = (id) => document.getElementById(id);
    const ME = { username: '', role: 'user', csrf: '', hostnames: [] };
    const charts = {};
    let lastTs = null;
    let CURRENT_NODE = null;
    let VIEW_ALL = false;          // přehled všech nádrží
    let lastState = null;
    let refillEndsAt = null;
    let settingsCache = null;

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function num(v, digits, unit) {
        if (v === null || v === undefined || Number.isNaN(v)) return '–';
        return Number(v).toLocaleString('cs-CZ', { minimumFractionDigits: digits, maximumFractionDigits: digits }) + (unit || '');
    }

    function pad(n) { return String(n).padStart(2, '0'); }
    function fmtDateTime(ms) {
        const d = new Date(ms);
        return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    function fmtDuration(sec) {
        sec = Math.max(0, Math.round(sec));
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
        const p = [];
        if (h) p.push(h + 'h');
        if (m || h) p.push(m + 'm');
        p.push(s + 's');
        return p.join(' ');
    }
    function fmtAgo(sec) {
        if (sec == null) return '–';
        if (sec < 90) return `před ${sec} s`;
        if (sec < 5400) return `před ${Math.round(sec / 60)} min`;
        if (sec < 172800) return `před ${Math.round(sec / 3600)} h`;
        return `před ${Math.round(sec / 86400)} dny`;
    }

    function toast(msg, isError) {
        const t = $('toast');
        t.textContent = msg;
        t.className = 'toast show' + (isError ? ' error' : '');
        clearTimeout(toast._t);
        toast._t = setTimeout(() => { t.className = 'toast'; }, 3500);
    }

    async function api(method, url, body) {
        const opt = { method, credentials: 'same-origin', headers: {} };
        if (method !== 'GET') {
            opt.headers['X-CSRF-Token'] = ME.csrf;
            if (body !== undefined) {
                opt.headers['Content-Type'] = 'application/json';
                opt.body = JSON.stringify(body);
            }
        }
        const r = await fetch(url, opt);
        if (r.status === 401) {
            window.location.replace('/login');
            throw new Error('Nepřihlášen');
        }
        let data = null;
        try { data = await r.json(); } catch (e) { /* non-JSON */ }
        if (!r.ok || (data && data.ok === false)) {
            throw new Error((data && (data.error || data.message)) || ('Chyba ' + r.status));
        }
        return data;
    }

    async function action(fn, okMsg) {
        try {
            const res = await fn();
            if (okMsg) toast(typeof okMsg === 'function' ? okMsg(res) : okMsg);
            return res;
        } catch (e) {
            toast(e.message || 'Chyba spojení se serverem', true);
            return null;
        }
    }

    // Čárové ikony ze SVG sprite v index.html (<symbol id="i-…">)
    function ico(name, cls) {
        return `<svg class="ico${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
    }

    // Výběry jsou nativní <select> (styl v CSS) – jen nastavení hodnoty
    function setSelect(select, value) {
        select.value = String(value);
    }

    // ------------------------------------------------------- účet (menu)
    function bindAccountMenu() {
        const btn = $('accountBtn'), menu = $('accountMenu');
        const items = () => Array.from(menu.querySelectorAll('.menu-item')).filter((el) => !el.classList.contains('hidden'));
        const isOpen = () => !menu.classList.contains('hidden');
        function open() {
            menu.classList.remove('hidden');
            btn.setAttribute('aria-expanded', 'true');
            const first = items()[0];
            if (first) first.focus();
        }
        function close(refocus) {
            if (!isOpen()) return;
            menu.classList.add('hidden');
            btn.setAttribute('aria-expanded', 'false');
            if (refocus) btn.focus();
        }
        btn.addEventListener('click', (e) => { e.stopPropagation(); if (isOpen()) close(false); else open(); });
        btn.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown' && !isOpen()) { e.preventDefault(); open(); }
        });
        menu.addEventListener('keydown', (e) => {
            const list = items(), i = list.indexOf(document.activeElement);
            if (!list.length) return;
            if (e.key === 'ArrowDown') { e.preventDefault(); list[(i + 1) % list.length].focus(); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); list[(i - 1 + list.length) % list.length].focus(); }
            else if (e.key === 'Home') { e.preventDefault(); list[0].focus(); }
            else if (e.key === 'End') { e.preventDefault(); list[list.length - 1].focus(); }
            else if (e.key === 'Tab') close(false);
        });
        menu.querySelectorAll('.menu-item').forEach((el) => el.addEventListener('click', () => close(false)));
        document.addEventListener('click', (e) => { if (isOpen() && !menu.contains(e.target)) close(false); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && isOpen()) { e.preventDefault(); close(true); } });
    }

    // --------------------------------------------------------------- tabs
    function showTab(id, btn) {
        document.querySelectorAll('.tab-content').forEach((el) => el.classList.remove('active'));
        document.querySelectorAll('.tab-btn').forEach((el) => { el.classList.remove('active'); el.removeAttribute('aria-current'); });
        $(id).classList.add('active');
        btn.classList.add('active');
        btn.setAttribute('aria-current', 'page');
        try { sessionStorage.setItem('tab', id); } catch (e) { /* ignore */ }
        if (lastState) renderNodeBar(lastState.nodes || []);
        if (id === 'dashboard') loadAllCharts();
        if (id === 'nastaveni') loadSettingsTab();
        if (id === 'logovani') loadAuditLog(1);
        if (id === 'uzivatele') loadUsers();
        if (id === 'heslo') loadTelegramMine();
    }

    // ------------------------------------------------------- range pickers
    const PRESETS = {
        time: [['24h', '24 h', 'Posledních 24 h'], ['7d', '7 dní', 'Posledních 7 dní'], ['30d', '30 dní', 'Posledních 30 dní'], ['365d', 'rok', 'Poslední rok'], ['custom', 'vlastní', 'Vlastní rozsah']],
        days: [['7d', '7 dní', 'Posledních 7 dní'], ['30d', '30 dní', 'Posledních 30 dní'], ['365d', 'rok', 'Poslední rok'], ['custom', 'vlastní', 'Vlastní rozsah']]
    };
    const SPAN = { '24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400, '365d': 365 * 86400 };
    const pickers = {};

    function isoDate(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }

    // České datum: <input type="date"> kreslí prohlížeč podle SVÉHO jazyka (v anglickém
    // Chromu mm/dd/yyyy). Nativní políčko proto zůstává skryté jako úložiště hodnoty
    // (ISO, id i posluchače beze změny) a kalendář; uživatel vidí a píše „13. 10. 2026“.
    function fmtCzDate(iso) {
        if (!iso) return '';
        const [y, m, d] = iso.split('-');
        return `${+d}. ${+m}. ${y}`;
    }
    function parseCzDate(txt) {
        const m = txt.trim().match(/^(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})$/);
        if (!m) return null;
        const d = new Date(+m[3], +m[2] - 1, +m[1]);
        return d.getDate() === +m[1] && d.getMonth() === +m[2] - 1 ? isoDate(d) : null;
    }
    function czDateInput(native) {
        if (native.dataset.cz) return;
        native.dataset.cz = '1';
        const wrap = document.createElement('span');
        wrap.className = 'date-cz';
        native.parentNode.insertBefore(wrap, native);
        const shown = document.createElement('input');
        shown.type = 'text';
        shown.className = native.className;
        shown.placeholder = 'd. m. rrrr';
        shown.inputMode = 'numeric';
        shown.autocomplete = 'off';
        if (native.title) shown.title = native.title;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'date-cz-btn';
        btn.title = 'Vybrat v kalendáři';
        btn.setAttribute('aria-label', 'Vybrat v kalendáři');
        btn.innerHTML = ico('calendar');
        native.classList.add('date-native');
        native.tabIndex = -1;
        wrap.append(shown, btn, native);
        const sync = () => { shown.value = fmtCzDate(native.value); };
        native.addEventListener('change', sync);
        native.addEventListener('input', sync);
        shown.addEventListener('change', () => {
            const txt = shown.value.trim();
            const iso = txt ? parseCzDate(txt) : '';
            if (iso === null) { toast('Datum zadejte jako 13. 10. 2026.', true); sync(); return; }
            native.value = (iso && native.max && iso > native.max) ? native.max : iso;
            sync();
            native.dispatchEvent(new Event('change'));
        });
        btn.addEventListener('click', () => {
            try { native.showPicker(); } catch (e) { shown.focus(); }
        });
        sync();
    }

    function buildRangePicker(el) {
        const key = el.dataset.chart;
        const kind = el.dataset.kind || 'time';
        // segmentový přepínač místo rozbalovacího seznamu
        const seg = document.createElement('div');
        seg.className = 'seg';
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Rozsah');
        const segBtns = PRESETS[kind].map(([v, label, title]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.dataset.range = v;
            b.textContent = label;
            b.title = title;
            b.setAttribute('aria-pressed', v === el.dataset.default ? 'true' : 'false');
            seg.appendChild(b);
            return b;
        });
        const custom = document.createElement('div');
        custom.className = 'range-custom hidden';
        const today = new Date();
        const weekAgo = new Date(Date.now() - 7 * 86400e3);
        custom.innerHTML = `<span>od</span><input type="date" class="unified-input" value="${isoDate(weekAgo)}" max="${isoDate(today)}">
            <span>do</span><input type="date" class="unified-input" value="${isoDate(today)}" max="${isoDate(today)}">
            <button type="button" class="btn btn-outline btn-sm range-apply">Zobrazit</button>`;
        el.appendChild(seg);
        el.appendChild(custom);
        const [inFrom, inTo] = custom.querySelectorAll('input[type="date"]');
        inFrom.setAttribute('aria-label', 'Od');
        inTo.setAttribute('aria-label', 'Do');
        czDateInput(inFrom);
        czDateInput(inTo);
        const state = { key, preset: el.dataset.default, from: null, to: null };
        pickers[key] = state;

        segBtns.forEach((b) => b.addEventListener('click', () => {
            const v = b.dataset.range;
            segBtns.forEach((x) => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
            state.preset = v;
            custom.classList.toggle('hidden', v !== 'custom');
            if (v !== 'custom') reloadChart(key);
        }));
        custom.querySelector('.range-apply').addEventListener('click', () => {
            const f = new Date(inFrom.value + 'T00:00:00');
            const t = new Date(inTo.value + 'T00:00:00');
            if (isNaN(f) || isNaN(t) || t < f) { toast('Zadejte platný rozsah od–do.', true); return; }
            state.from = Math.floor(f.getTime() / 1000);
            state.to = Math.floor(t.getTime() / 1000) + 86400;    // včetně celého dne "do"
            reloadChart(key);
        });
    }

    function rangeOf(key) {
        const p = pickers[key];
        const now = Math.floor(Date.now() / 1000);
        if (p.preset === 'custom' && p.from) return { from: p.from, to: p.to, live: false };
        const span = SPAN[p.preset === 'custom' ? '7d' : p.preset] || 86400;
        return { from: now - span, to: now, live: true };
    }
    function rangeQuery(r) { return `from=${r.from}&to=${r.to}`; }

    // -------------------------------------------------------------- charts
    // Vzhled grafů z CSS proměnných (světlý / tmavý motiv) – čte se znovu při změně motivu
    const T = {};
    function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
    function alpha(color, a) {
        const m = /^#([0-9a-f]{6})$/i.exec(color);
        if (!m) return color;
        const n = parseInt(m[1], 16);
        return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
    }
    function readTheme() {
        Object.assign(T, {
            ink: cssVar('--ink'), ink2: cssVar('--ink-2'), muted: cssVar('--muted'),
            surface: cssVar('--surface'), line: cssVar('--line'), lineStrong: cssVar('--line-strong'),
            grid: cssVar('--grid'), accent: cssVar('--accent'), c2: cssVar('--chart-2'),
            green: cssVar('--green'), amber: cssVar('--amber'), red: cssVar('--red'),
            bandRefill: cssVar('--band-refill'), bandRain: cssVar('--band-rain')
        });
        const d = Chart.defaults;
        d.color = T.muted;
        d.borderColor = T.grid;
        d.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
        d.font.size = 12;
        d.scale.grid.color = T.grid;
        d.scale.grid.tickLength = 0;
        d.scale.border.display = false;
        d.scale.ticks.color = T.muted;
        d.scale.ticks.padding = 6;
        d.elements.line.borderWidth = 1.75;
        d.elements.line.tension = 0.25;
        d.elements.point.radius = 0;
        d.elements.point.hoverRadius = 3;
        d.elements.point.hitRadius = 6;
        d.elements.bar.borderRadius = 2;
        d.datasets.bar.maxBarThickness = 26;
        const lg = d.plugins.legend;
        lg.align = 'start';
        lg.labels.color = T.ink2;
        lg.labels.usePointStyle = true;
        lg.labels.pointStyle = 'rect';
        lg.labels.boxWidth = 8;
        lg.labels.boxHeight = 8;
        lg.labels.padding = 14;
        const tt = d.plugins.tooltip;
        tt.backgroundColor = T.surface;
        tt.titleColor = T.ink;
        tt.bodyColor = T.ink2;
        tt.footerColor = T.muted;
        tt.borderColor = T.lineStrong;
        tt.borderWidth = 1;
        tt.cornerRadius = 6;
        tt.padding = 10;
        tt.boxPadding = 4;
        tt.usePointStyle = true;
        tt.titleFont = { weight: '600' };
        tt.footerFont = { weight: 'normal' };
    }
    readTheme();
    // jen délka – nahrazení celého objektu by smazalo easing a animační smyčka Chart.js
    // by spadla ("this._fn is not a function") → další grafy by zůstaly prázdné
    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    Chart.defaults.animation.duration = reduceMotion ? 0 : 150;
    // čára + body stejné barvy (legenda a tooltip berou backgroundColor)
    const line = (color, extra) => Object.assign({ borderColor: color, backgroundColor: color, pointBackgroundColor: color, pointBorderColor: color }, extra || {});

    const histCache = new Map();
    async function getHistory(r) {
        const k = CURRENT_NODE + '|' + rangeQuery(r);
        const hit = histCache.get(k);
        if (hit && Date.now() - hit.at < 60000) return hit.data;
        const data = await api('GET', `/api/history?node=${CURRENT_NODE}&` + rangeQuery(r));
        histCache.set(k, { at: Date.now(), data });
        if (histCache.size > 12) histCache.delete(histCache.keys().next().value);
        return data;
    }

    // Dílky časové osy zarovnané na celé hodiny / půlnoc / začátek měsíce (místní čas).
    // Chart.js by čas dělil jako obyčejná čísla – dílky na náhodných hodinách a popisky
    // dnů s mezerami (7.10., 9.10. …).
    const MONTHS = ['led', 'úno', 'bře', 'dub', 'kvě', 'čvn', 'čvc', 'srp', 'zář', 'říj', 'lis', 'pro'];
    function timeTicks(fromMs, toMs, maxN) {
        const H = 3600e3, D = 86400e3, span = toMs - fromMs, out = [];
        const hours = [1, 2, 3, 6, 12].find((h) => span / (h * H) <= maxN);
        const days = [1, 2, 3, 7, 14].find((n) => span / (n * D) <= maxN);
        if (hours) {
            const d = new Date(fromMs);
            d.setMinutes(0, 0, 0);
            while (d.getTime() < fromMs || d.getHours() % hours) d.setHours(d.getHours() + 1);
            for (; d.getTime() <= toMs; d.setHours(d.getHours() + hours)) out.push({ value: d.getTime(), unit: 'h' });
        } else if (days) {
            const d = new Date(fromMs);
            d.setHours(0, 0, 0, 0);
            if (d.getTime() < fromMs) d.setDate(d.getDate() + 1);
            for (; d.getTime() <= toMs; d.setDate(d.getDate() + days)) out.push({ value: d.getTime(), unit: 'd' });
        } else {
            const months = [1, 2, 3, 6, 12].find((m) => span / (m * 30.4 * D) <= maxN) || 12;
            const d = new Date(fromMs);
            d.setHours(0, 0, 0, 0);
            d.setDate(1);
            if (d.getTime() < fromMs) d.setMonth(d.getMonth() + 1);
            while (d.getMonth() % months) d.setMonth(d.getMonth() + 1);
            for (; d.getTime() <= toMs; d.setMonth(d.getMonth() + months)) out.push({ value: d.getTime(), unit: 'm' });
        }
        return out;
    }
    function tickLabel(v, unit) {
        const d = new Date(v);
        if (unit === 'm') return d.getMonth() === 0 ? `${MONTHS[0]} ${d.getFullYear()}` : MONTHS[d.getMonth()];
        if (unit === 'h' && (d.getHours() || d.getMinutes())) return `${d.getHours()}:${pad(d.getMinutes())}`;
        return `${d.getDate()}. ${d.getMonth() + 1}.`;
    }

    function timeScale(r) {
        const ticks = timeTicks(r.from * 1000, r.to * 1000, window.innerWidth < 640 ? 4 : 8);   // na telefonu méně popisků
        const unitOf = new Map(ticks.map((t) => [t.value, t.unit]));
        return {
            type: 'linear',
            min: r.from * 1000,
            max: r.to * 1000,
            afterBuildTicks: (axis) => { axis.ticks = ticks.map((t) => ({ value: t.value })); },
            ticks: { autoSkip: true, maxRotation: 0, callback: (v) => tickLabel(v, unitOf.get(v)) }
        };
    }
    const timeTooltip = { callbacks: { title: (ctx) => ctx.length ? fmtDateTime(ctx[0].parsed.x) : '' } };

    function series(t, values) {
        const out = new Array(t.length);
        for (let i = 0; i < t.length; i++) out[i] = { x: t[i], y: values[i] };
        return out;
    }

    function upsert(key, canvasId, config) {
        if (charts[key]) charts[key].destroy();
        charts[key] = new Chart($(canvasId).getContext('2d'), config);
    }

    // Úseky dopouštění (zeleně) a přítoku z deště (modře): jen jemné podbarvení pod křivkou,
    // bez hran (popis dává tooltip)
    const bandsPlugin = {
        id: 'bands',
        beforeDatasetsDraw(chart, args, opts) {
            const bands = opts.bands || [];
            if (!bands.length) return;
            const x = chart.scales.x, { top, bottom, left, right } = chart.chartArea;
            const ctx = chart.ctx;
            // blízké úseky téhož druhu se slijí do jedné plochy (místo hřebenu tenkých čar)
            const spans = { refill: [], rain: [] };
            bands.slice().sort((a, b) => a.start - b.start).forEach((b) => {
                let l = x.getPixelForValue(b.start), r = x.getPixelForValue(b.end);
                if (r < left || l > right) return;
                if (r - l < 3) { l -= 1.5; r += 1.5; }
                const list = spans[b.kind === 'refill' ? 'refill' : 'rain'];
                const last = list[list.length - 1];
                if (last && l - last[1] < 8) last[1] = Math.max(last[1], r);
                else list.push([l, r]);
            });
            ctx.save();
            Object.keys(spans).forEach((k) => {
                ctx.fillStyle = k === 'refill' ? T.bandRefill : T.bandRain;
                spans[k].forEach(([l, r]) => {
                    const cl = Math.max(l, left), cr = Math.min(r, right);
                    ctx.fillRect(cl, top, cr - cl, bottom - top);
                });
            });
            ctx.restore();
        }
    };
    function bandsAt(bands, xVal) {
        const out = [];
        (bands || []).forEach((b) => {
            if (xVal < b.start - 150e3 || xVal > b.end + 150e3) return;
            const t = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
            const what = b.kind === 'refill' ? 'Dopouštění' : 'Přítok z deště';
            out.push(`${what} ${t(b.start)}–${t(b.end)}${b.litres != null ? ` · +${num(b.litres, 0, ' L')}` : ''}`);
        });
        return out;
    }

    // Zapnutí čidla / výměna baterie – tenká plná čára a značka nahoře v grafu baterie
    // (plný kroužek = vložení baterie, prázdný = restart)
    const bootMarks = {
        id: 'bootMarks',
        afterDatasetsDraw(chart, args, opts) {
            const ev = opts.boots || [];
            if (!ev.length) return;
            const x = chart.scales.x, { top, bottom, left, right } = chart.chartArea;
            const ctx = chart.ctx;
            ctx.save();
            ctx.lineWidth = 1;
            ev.forEach((e) => {
                const px = Math.round(x.getPixelForValue(e.t)) + 0.5;
                if (px < left || px > right) return;
                ctx.strokeStyle = alpha(T.amber, 0.35);
                ctx.beginPath(); ctx.moveTo(px, top + 8); ctx.lineTo(px, bottom); ctx.stroke();
                ctx.beginPath(); ctx.arc(px, top + 4, 3.5, 0, Math.PI * 2);
                ctx.strokeStyle = T.amber;
                ctx.fillStyle = e.wdt ? T.surface : T.amber;
                ctx.fill(); ctx.stroke();
            });
            ctx.restore();
        }
    };

    const pctAxis = () => ({ min: 0, max: 100, ticks: { callback: (v) => v + ' %', maxTicksLimit: 6 } });
    const baseOpts = () => ({ responsive: true, maintainAspectRatio: false, interaction: { mode: 'nearest', axis: 'x', intersect: false } });

    async function loadLevelChart() {
        const r = rangeOf('level');
        const d = await getHistory(r);
        upsert('level', 'historyChart', {
            type: 'line',
            data: { datasets: [line(T.accent, { label: 'Hladina (%)', data: series(d.t, d.level_pct), backgroundColor: alpha(T.accent, 0.08), fill: 'origin', borderWidth: 2, spanGaps: false })] },
            plugins: [bandsPlugin],
            options: Object.assign(baseOpts(), {
                plugins: {
                    legend: { display: false },
                    bands: { bands: d.bands },
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: timeTooltip.callbacks.title,
                            label: (ctx) => {
                                const i = ctx.dataIndex;
                                return `${num(d.level_pct[i], 1, ' %')} · ${num(d.level_cm[i], 1, ' cm')} · ${num(d.volume_l[i], 0, ' L')}`;
                            },
                            footer: (items) => items.length ? bandsAt(d.bands, items[0].parsed.x) : []
                        }
                    }
                },
                scales: { x: timeScale(r), y: pctAxis() }
            })
        });
    }

    // Přehled: hladina všech nádrží v jednom grafu (kategorické, tlumené barvy)
    const PALETTE = () => [T.accent, T.c2, '#b07a2e', '#3c8c87', '#8a6bb8', '#b65f7f', '#6f8a3c', '#5b7fa6'];
    async function loadAllNodesChart() {
        const nodes = (lastState && lastState.nodes) || [];
        if (!nodes.length) return;
        const r = rangeOf('all');
        const res = await Promise.all(nodes.map((n) =>
            api('GET', `/api/history?node=${n.id}&` + rangeQuery(r)).then((d) => ({ n, d })).catch(() => null)));
        const pal = PALETTE();
        const datasets = res.filter(Boolean).map(({ n, d }, i) => line(pal[i % pal.length], {
            label: n.name, data: series(d.t, d.level_pct), spanGaps: false
        }));
        upsert('all', 'allChart', {
            type: 'line',
            data: { datasets },
            options: Object.assign(baseOpts(), {
                plugins: { tooltip: { callbacks: { title: timeTooltip.callbacks.title, label: (c) => ` ${c.dataset.label}: ${num(c.parsed.y, 1, ' %')}` } } },
                scales: { x: timeScale(r), y: pctAxis() }
            })
        });
    }

    async function loadKlimaChart() {
        const r = rangeOf('klima');
        const d = await getHistory(r);
        upsert('klima', 'klimaChart', {
            type: 'line',
            data: {
                datasets: [
                    line(T.c2, { label: 'Teplota (°C)', data: series(d.t, d.temp), yAxisID: 'y' }),
                    line(T.accent, { label: 'Vlhkost (%)', data: series(d.t, d.hum), yAxisID: 'y1' })
                ]
            },
            options: Object.assign(baseOpts(), {
                plugins: { tooltip: timeTooltip },
                scales: {
                    x: timeScale(r),
                    y: { position: 'left', ticks: { callback: (v) => v + ' °C', maxTicksLimit: 6 } },
                    y1: { position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + ' %', maxTicksLimit: 6 } }
                }
            })
        });
    }

    async function loadBatteryChart() {
        const r = rangeOf('battery');
        const d = await getHistory(r);
        upsert('battery', 'batteryChart', {
            type: 'line',
            plugins: [bootMarks],
            data: {
                datasets: [
                    line(T.accent, { label: 'Napětí baterie (V)', data: series(d.t, d.vbat), spanGaps: true, yAxisID: 'y' }),
                    line(T.c2, { label: 'Signál (dBm)', data: series(d.t, d.rssi || []), borderWidth: 1.25, yAxisID: 'y1' })
                ]
            },
            options: Object.assign(baseOpts(), {
                plugins: {
                    bootMarks: { boots: d.boots },
                    tooltip: {
                        callbacks: {
                            title: timeTooltip.callbacks.title,
                            footer: (items) => {
                                if (!items.length) return [];
                                const xv = items[0].parsed.x, half = Math.max(d.bucket_s || 300, 900) * 1000;
                                return (d.boots || []).filter((b) => Math.abs(b.t - xv) <= half)
                                    .map((b) => b.wdt ? 'Restart čidla' : 'Vložení baterie');
                            }
                        }
                    }
                },
                scales: {
                    x: timeScale(r),
                    y: { suggestedMin: 3.0, suggestedMax: 5.5, ticks: { callback: (v) => v.toFixed(1) + ' V', maxTicksLimit: 6 } },
                    y1: { position: 'right', suggestedMin: -110, suggestedMax: -50, grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + ' dBm', maxTicksLimit: 6 } }
                }
            })
        });
    }

    function dayLabels(rows) {
        return rows.map((x) => { const p = x.day.split('-'); return `${+p[2]}. ${+p[1]}.`; });
    }

    const flowsCache = new Map();
    async function getFlows(r) {
        const k = CURRENT_NODE + '|' + rangeQuery(r);
        const hit = flowsCache.get(k);
        if (hit && Date.now() - hit.at < 60000) return hit.data;
        const data = await api('GET', `/api/flows?node=${CURRENT_NODE}&` + rangeQuery(r));
        flowsCache.set(k, { at: Date.now(), data });
        if (flowsCache.size > 8) flowsCache.delete(flowsCache.keys().next().value);
        return data;
    }

    const barScales = () => ({
        x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 8 } },
        y: { beginAtZero: true, ticks: { callback: (v) => num(v, 0) + ' L', maxTicksLimit: 6 } }
    });
    const barOpts = () => ({ responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: barScales() });

    async function loadConsumptionChart() {
        const rows = await getFlows(rangeOf('consumption'));
        upsert('consumption', 'consumptionChart', {
            type: 'bar',
            data: {
                labels: dayLabels(rows),
                datasets: [
                    { label: 'Denní spotřeba (L)', data: rows.map((x) => x.use), backgroundColor: T.c2 },
                    { label: 'Přítok ze srážek (L)', data: rows.map((x) => x.rain), backgroundColor: alpha(T.accent, 0.6) }
                ]
            },
            options: barOpts()
        });
    }

    async function loadRefillChart() {
        const rows = await getFlows(rangeOf('refill'));
        upsert('refill', 'refillChart', {
            type: 'bar',
            data: { labels: dayLabels(rows), datasets: [{ label: 'Denní dopouštění (L)', data: rows.map((x) => x.refill), backgroundColor: alpha(T.green, 0.75) }] },
            options: barOpts()
        });
    }

    async function loadWeatherChart() {
        const d = await api('GET', '/api/weather');
        upsert('weather', 'weatherChart', {
            type: 'bar',
            data: {
                labels: (d.time || []).map((x) => { const p = x.split('-'); return `${+p[2]}. ${+p[1]}.`; }),
                datasets: [
                    { label: 'Srážky (mm)', data: d.precipitation_sum || [], backgroundColor: alpha(T.accent, 0.6), yAxisID: 'y', order: 2 },
                    line(T.c2, { label: 'Max teplota (°C)', data: d.temperature_2m_max || [], type: 'line', yAxisID: 'y1', order: 1, pointRadius: 2 })
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { grid: { display: false }, ticks: { maxRotation: 0 } },
                    y: { position: 'left', beginAtZero: true, ticks: { callback: (v) => v + ' mm', maxTicksLimit: 6 } },
                    y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + ' °C', maxTicksLimit: 6 } }
                }
            }
        });
    }

    const LOADERS = {
        level: loadLevelChart, klima: loadKlimaChart, battery: loadBatteryChart,
        consumption: loadConsumptionChart, refill: loadRefillChart, weather: loadWeatherChart,
        all: loadAllNodesChart
    };
    const NODE_CHARTS = ['level', 'klima', 'battery', 'consumption', 'refill', 'weather'];
    function reloadChart(key) {
        if (LOADERS[key]) LOADERS[key]().catch((e) => console.error(key, e));
    }
    function loadAllCharts(onlyLive) {
        if (onlyLive) { histCache.clear(); flowsCache.clear(); }
        (VIEW_ALL ? ['all'] : NODE_CHARTS).forEach((k) => {
            if (!CURRENT_NODE && k !== 'weather') return;
            if (onlyLive && pickers[k] && !rangeOf(k).live) return;   // vlastní rozsah se nemění
            reloadChart(k);
        });
    }

    // ---------------------------------------------------------- live state
    function renderState(s) {
        const pill = $('statusPill');
        pill.className = 'status-pill ' + s.status;
        pill.textContent = { online: 'Online', stale: 'Zpožděná data', offline: 'Offline' }[s.status] || s.status;

        const pct = s.water_level_percent;
        $('waterLevel').style.height = (pct == null ? 0 : Math.max(0, Math.min(100, pct))) + '%';
        $('txtPercent').textContent = num(pct, 1, ' %');
        $('txtLiters').textContent = num(s.volume_liters, 0, ' L');
        $('txtLevelCm').textContent = s.water_level_cm == null ? '–' : `${num(s.water_level_cm, 1, ' cm')} z ${num(s.max_depth_cm, 0, ' cm')}`;
        $('txtUpdated').textContent = s.timestamp ? `${fmtDateTime(s.timestamp * 1000)} (${fmtAgo(s.last_seen_seconds_ago)})` : 'Čekám na data z čidla…';
        $('txtTemp').textContent = num(s.temperature, 1, ' °C');
        $('txtHum').textContent = num(s.humidity, 1, ' %');

        const b = s.battery || {};
        $('txtBat').textContent = num(b.percent, 0, ' %');
        $('txtBatV').textContent = num(b.voltage, 2, ' V') + (b.sag_mv ? ` · při vysílání −${num(b.sag_mv, 0)} mV` : '');
        $('batBox').classList.toggle('battery-low', !!b.low);
        const days = b.days_left;
        $('txtBatEst').textContent = b.off ? 'Čidlo vypnuto – vyměňte baterii'
            : days == null ? 'Nedostatek dat' : (days > 3650 ? '> 10 let' : days > 730 ? `${num(days / 365, 1)} roku` : `${num(days, 0)} dní`);

        $('txtConsToday').textContent = num(s.consumption_today, 0, ' L');
        $('txtCons7d').textContent = num(s.consumption_7d, 0, ' L');
        $('txtRain').textContent = num(s.expected_rain_mm, 1, ' mm');
        const o = s.outlook || {};
        $('txtOutlook').textContent = o.days_left == null ? '–' : (o.days_left > 365 ? 'vystačí > rok' : `vystačí ~${num(o.days_left, 0)} dní`);
        $('txtOutlookSub').textContent = [
            o.use_per_day_l != null ? `spotřeba ~${num(o.use_per_day_l, 0)} L/den` : 'málo dat o spotřebě',
            o.inflow_7d_l ? `déšť 7 dní: +${num(o.inflow_7d_l, 0)} L → ~${num(o.pct_after_rain, 0)} %` : ''
        ].filter(Boolean).join(' · ');

        const n = s.node || {};
        $('txtRssi').textContent = n.rssi == null ? '–' : `${num(n.rssi, 0)} dBm`;
        $('txtLoss').textContent = n.loss_pct == null ? '–' : `ztráty 24 h: ${num(n.loss_pct, 1)} %`
            + (n.busy_pct ? ` · rušení ${num(n.busy_pct, 0)} %` : '');
        $('linkBox').classList.toggle('warn', n.rssi != null && (n.rssi < -100 || (n.loss_pct || 0) > 10 || (n.busy_pct || 0) > 20));

        const rf = s.refill || {};
        refillEndsAt = rf.active ? rf.ends_at : null;
        $('txtRefillStatus').textContent = rf.active ? 'Dopouští' : rf.waiting_for_data ? 'Čeká na měření' : rf.waiting_for_rain ? 'Čeká na déšť' : 'Nedopouští';
        const zc = rf.zone_controlled_by;
        $('txtRefillTime').textContent = zc ? `Ventil ovládá nádrž „${zc.name}“` : (rf.status || '–');
        $('zoneNote').classList.toggle('hidden', !zc);
        $('zoneNote').textContent = zc ? `Tento ventil ovládá nádrž „${zc.name}“.` : '';
        $('refillBox').classList.toggle('ok', !!rf.active);
        $('refillActions').classList.toggle('hidden', !(ME.role === 'admin' && rf.configured));
        $('refillResumeBtn').classList.toggle('hidden', !rf.auto_paused_until);
        $('manualPctInput').max = rf.max_pct || 100;
        runRefillTimer();

        // výstrahy
        const warn = [];
        if (s.status === 'offline') warn.push('Čidlo neposílá data – zkontrolujte baterii nebo dosah signálu.');
        if (s.radio && !s.radio.ok) warn.push('Rádiový přijímač CC1101 nepracuje – probíhá automatická obnova.');
        if (b.low) warn.push('Baterie čidla je téměř vybitá.');
        if (!s.time_ok) warn.push('Čas zařízení zatím není synchronizován (chybí internet) – časové údaje mohou být posunuté.');
        (n.warnings || []).forEach((w) => warn.push('Čidlo: ' + w));
        const ban = $('banner');
        ban.classList.toggle('hidden', warn.length === 0);
        ban.classList.toggle('danger', s.status === 'offline' || (s.radio && !s.radio.ok));
        ban.innerHTML = ico('alert') + '<ul>' + warn.map((w) => `<li>${esc(w)}</li>`).join('') + '</ul>';

        // živá data v nastavení
        $('liveProbeMv').textContent = num(s.probe_mv, 1, ' mV');
        $('liveColumn').textContent = num(s.column_cm, 1, ' cm');
        $('liveLevel').textContent = num(s.water_level_cm, 1, ' cm');
        $('liveInterval').textContent = n.fast ? '60 s (rychlý)' : '5 min';
    }

    function runRefillTimer() {
        if (!refillEndsAt) return;
        const left = refillEndsAt - Date.now() / 1000;
        $('txtRefillTime').textContent = left > 0 ? `Zbývá nejvýše ${fmtDuration(left)}` : 'Dokončuji…';
    }

    function setViewAll(on) {
        VIEW_ALL = !!on;
        try { sessionStorage.setItem('viewAll', VIEW_ALL ? '1' : ''); } catch (e) { /* ignore */ }
        $('overview').classList.toggle('hidden', !VIEW_ALL);
        $('nodeDash').classList.toggle('hidden', VIEW_ALL);
    }

    function renderOverview(nodes) {
        const g = $('overviewGrid');
        g.textContent = '';
        nodes.forEach((n) => {
            const c = document.createElement('div');
            c.className = 'card ov-card';
            const pct = n.level_pct == null ? 0 : Math.max(0, Math.min(100, n.level_pct));
            const b = n.battery || {};
            const st = { online: 'online', stale: 'zpožděná data', offline: 'offline' }[n.status] || n.status;
            c.innerHTML = `<div class="ov-tank"><div class="water-level"></div></div>
                <div class="ov-body"><h4><span class="dot ${esc(n.status)}"></span>${esc(n.name)}</h4>
                <div class="big">${num(n.level_pct, 0, ' %')}</div>
                <div class="meta">${num(n.volume_l, 0, ' L')}${n.max_l ? ' z ' + num(n.max_l, 0, ' L') : ''}<br>
                <span class="${n.status === 'online' ? '' : n.status === 'stale' ? 'warn' : 'bad'}">${esc(st)}</span>${n.age_s != null ? ' · ' + fmtAgo(n.age_s) : ''}<br>
                ${ico('battery')} <span class="${b.low ? 'bad' : ''}">${num(b.percent, 0, ' %')}</span>${n.refilling ? ` · <span class="ok">${ico('drop')} dopouští</span>` : ''}</div></div>`;
            c.querySelector('.water-level').style.height = pct + '%';     // CSSOM (CSP bez inline stylů)
            c.addEventListener('click', () => { setViewAll(false); selectNode(n.id, true); renderNodeBar(nodes); });
            g.appendChild(c);
        });
    }

    function selectNode(id, force) {
        if (!id || (id === CURRENT_NODE && !force)) return;
        CURRENT_NODE = id;
        try { sessionStorage.setItem('node', id); } catch (e) { /* ignore */ }
        lastTs = null;
        pollState().then(() => { if ($('dashboard').classList.contains('active')) loadAllCharts(); });
    }

    function renderNodeBar(nodes) {
        const bar = $('nodeBar');
        const tabWithNodes = $('dashboard').classList.contains('active') || $('nastaveni').classList.contains('active');
        bar.classList.toggle('hidden', nodes.length < 2 || !tabWithNodes);
        bar.textContent = '';
        const onDash = $('dashboard').classList.contains('active');
        const allActive = VIEW_ALL && onDash;
        if (nodes.length >= 2 && onDash) {
            const all = document.createElement('button');
            all.type = 'button';
            all.className = 'node-pill all' + (allActive ? ' active' : '');
            all.setAttribute('role', 'tab');
            all.setAttribute('aria-selected', allActive ? 'true' : 'false');
            const online = nodes.filter((n) => n.status === 'online').length;
            all.innerHTML = `Všechny nádrže <span class="pct">${online}/${nodes.length} online</span>`;
            all.addEventListener('click', () => {
                if (!$('dashboard').classList.contains('active')) showTab('dashboard', document.querySelector('.tab-btn[data-tab="dashboard"]'));
                setViewAll(true); renderNodeBar(nodes); renderOverview(nodes); loadAllCharts();
            });
            bar.appendChild(all);
        } else if (VIEW_ALL && nodes.length < 2) {
            setViewAll(false);
        }
        nodes.forEach((n) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'node-pill' + (n.id === CURRENT_NODE && !allActive ? ' active' : '');
            b.setAttribute('role', 'tab');
            b.setAttribute('aria-selected', n.id === CURRENT_NODE && !allActive ? 'true' : 'false');
            b.innerHTML = `<span class="dot ${esc(n.status)}"></span>${esc(n.name)} <span class="pct">${num(n.level_pct, 0, ' %')}</span>`;
            b.addEventListener('click', () => {
                const wasAll = VIEW_ALL;
                setViewAll(false);
                selectNode(n.id, wasAll);
                renderNodeBar(nodes);
                if ($('nastaveni').classList.contains('active')) applyNode(currentNodeCfg());
            });
            bar.appendChild(b);
        });
    }

    async function pollState() {
        try {
            let s = await api('GET', '/api/state' + (CURRENT_NODE ? '?node=' + CURRENT_NODE : ''));
            const ids = (s.nodes || []).map((n) => n.id);
            if (ids.length && (!CURRENT_NODE || !ids.includes(CURRENT_NODE) || !s.node)) {
                CURRENT_NODE = ids.includes(CURRENT_NODE) ? CURRENT_NODE : ids[0];
                s = await api('GET', '/api/state?node=' + CURRENT_NODE);
            }
            lastState = s;
            renderNodeBar(s.nodes || []);
            if (VIEW_ALL) renderOverview(s.nodes || []);
            if (!s.node) {
                $('statusPill').className = 'status-pill offline';
                $('statusPill').textContent = 'Čeká se na první čidlo';
                return;
            }
            renderState(s);
            if (VIEW_ALL) {
                const ns = s.nodes || [], on = ns.filter((n) => n.status === 'online').length;
                const pill = $('statusPill');
                pill.className = 'status-pill ' + (on === ns.length ? 'online' : on ? 'stale' : 'offline');
                pill.textContent = `Online ${on}/${ns.length}`;
            }
            if (s.timestamp !== lastTs) {
                if (lastTs !== null && $('dashboard').classList.contains('active')) loadAllCharts(true);
                lastTs = s.timestamp;
            }
        } catch (e) {
            const pill = $('statusPill');
            pill.className = 'status-pill offline';
            pill.textContent = 'Server nedostupný';
        }
    }

    // --------------------------------------------------------------- refill
    function bindRefill() {
        $('refillStartBtn').addEventListener('click', () => {
            const v = parseFloat($('manualPctInput').value);
            if (!v || v <= 0) { toast('Zadejte platné procento cílové hladiny.', true); return; }
            action(() => api('POST', '/api/refill/start?node=' + CURRENT_NODE, { target_pct: v }), (r) => r.message).then(pollState);
        });
        $('refillResumeBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/refill/resume?node=' + CURRENT_NODE, {}), (r) => r.message).then(pollState);
        });
        $('refillStopBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/refill/stop?node=' + CURRENT_NODE, {}), (r) => r.message).then(pollState);
        });
    }

    // ------------------------------------------------------------- settings
    let nodesCache = null;     // poslední odpověď /api/nodes

    function fillForm(form, s) {
        Array.from(form.elements).forEach((el) => {
            if (!el.name || !(el.name in s)) return;
            if (el.type === 'checkbox') el.checked = s[el.name] === true || s[el.name] === 'true';   // přepínač
            else if (el.tagName === 'SELECT') setSelect(el, s[el.name] == null ? '' : String(s[el.name]));
            else if (el.type === 'password') { el.value = ''; el.placeholder = s[el.name] || 'nenastaveno'; }
            else el.value = s[el.name] == null ? '' : s[el.name];
        });
    }
    function formData(form) {
        const out = {};
        Array.from(form.elements).forEach((el) => {
            if (!el.name || el.disabled) return;
            if (el.type === 'password' && el.value === '') return;   // klíč beze změny
            if (el.type === 'checkbox') { out[el.name] = el.checked; return; }        // true / false jako dřív
            out[el.name] = el.tagName === 'SELECT' ? (el.value === 'true' ? true : el.value === 'false' ? false : el.value) : el.value;
        });
        return out;
    }

    function renderCalPoints(points) {
        const body = $('calPoints');
        body.textContent = '';
        const pts = (points || []).slice();
        while (pts.length < 2) pts.push(['', '']);
        pts.forEach((p, i) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${i + 1}${i === 0 ? ' (nižší hladina)' : i === 1 && pts.length === 2 ? ' (vyšší hladina)' : ''}</td>
                <td><input type="number" step="0.1" class="unified-input cal-mv" value="${esc(p[0])}"></td>
                <td><input type="number" step="0.1" class="unified-input cal-cm" value="${esc(p[1])}"></td>`;
            body.appendChild(tr);
        });
    }

    function currentNodeCfg() {
        return nodesCache ? nodesCache.nodes.find((n) => n.id === CURRENT_NODE) : null;
    }

    function applyNode(n) {
        if (!n) return;
        $('calNodeName').textContent = n.name;
        $('calNodeId').value = n.id;
        fillForm($('calForm'), n);
        renderCalPoints(n.cal_points);
        $('batTrim').textContent = num(n.battery_trim, 4);
        $('deadbandInfo').textContent = `±${num(n.deadband_cm, 1)} cm (≈ ${num(n.deadband_l, 0)} L)`;
        $('realFlowHint').textContent = n.real_flow_rate_lpm ? `(naměřeno ${num(n.real_flow_rate_lpm, 1)})` : '';
    }

    function applySettings(s) {
        fillForm($('sysForm'), s);
    }

    function setAdvanced(on) {
        document.body.classList.toggle('show-adv', !!on);
        $('advToggle').checked = !!on;
        $('advLabel').textContent = on ? 'základní i pokročilé nastavení' : 'základní nastavení';
        try { localStorage.setItem('advSettings', on ? '1' : ''); } catch (e) { /* ignore */ }
    }

    function renderBackups(d) {
        const body = $('backupBody');
        body.textContent = '';
        const list = (d && d.backups) || [];
        if (!list.length) body.innerHTML = '<tr><td colspan="2" class="muted">Zatím žádná – první vznikne do 24 h od spuštění.</td></tr>';
        list.forEach((b) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${fmtDateTime(b.ts * 1000)}</td><td>${num(b.size_mb, 1, ' MB')}</td>`;
            body.appendChild(tr);
        });
    }

    function tgStatusText(t) {
        if (!t || !t.configured) return 'nenastaveno';
        return t.connected ? `připojeno · @${t.bot}` : (t.error || 'připojuji…');
    }

    function renderTelegramAdmin(t) {
        $('tgAdminStatus').textContent = tgStatusText(t);
        const body = $('tgChatsBody');
        body.textContent = '';
        const list = (t && t.chats) || [];
        if (!list.length) body.innerHTML = '<tr><td colspan="5" class="muted">Žádný propojený chat – uživatelé se propojí v „Můj účet“.</td></tr>';
        list.forEach((c) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${esc(c.title || c.chat_id)}</td><td>${esc(c.username)}</td>
                <td>${c.notify ? 'zapnuta' : 'vypnuta'}</td><td>${fmtDateTime(c.created * 1000)}</td><td class="td-actions"></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-danger btn-sm'; b.textContent = 'Odpojit';
            b.addEventListener('click', () => {
                if (!confirm('Odpojit tento Telegram chat?')) return;
                action(() => api('POST', '/api/telegram/unlink', { chat_id: c.chat_id }), 'Odpojeno.').then(loadSettingsTab);
            });
            tr.querySelector('.td-actions').appendChild(b);
            body.appendChild(tr);
        });
    }

    function renderServis(s) {
        if (!s) return;
        let txt;
        if (!s.installed) txt = 'NetBird není nainstalován';
        else if (s.mode === 'on') txt = 'zapnuto trvale';
        else if (s.mode === '24h' && s.until) txt = 'zapnuto do ' + fmtDateTime(s.until * 1000);
        else txt = 'vypnuto';
        if (s.installed && s.connected) txt += ` · připojeno (${s.ip || '?'})`;
        $('servisStatus').textContent = txt;
        setSelect($('servisMode'), s.mode || 'off');
    }

    async function loadTelegramMine() {
        const t = await api('GET', '/api/telegram/mine').catch(() => null);
        if (!t) return;
        $('tgMineInfo').textContent = t.connected ? `Upozornění a ovládání nádrží z telefonu (bot @${t.bot}).`
            : t.configured ? `Telegram bot není připojený${t.error ? ' – ' + t.error : ''}.`
                : 'Telegram zatím nenastavil administrátor (Nastavení → Obecné → Telegram).';
        $('tgLinkBtn').disabled = !t.connected;
        const body = $('tgMineBody');
        body.textContent = '';
        if (!t.chats.length) body.innerHTML = '<tr><td colspan="3" class="muted">Zatím nepropojeno.</td></tr>';
        t.chats.forEach((c) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${esc(c.title || c.chat_id)}</td><td>${fmtDateTime(c.created * 1000)}</td><td class="td-actions"></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-danger btn-sm'; b.textContent = 'Odpojit';
            b.addEventListener('click', () => {
                action(() => api('POST', '/api/telegram/unlink', { chat_id: c.chat_id }), 'Odpojeno.').then(loadTelegramMine);
            });
            tr.querySelector('.td-actions').appendChild(b);
            body.appendChild(tr);
        });
    }

    async function loadSettingsTab() {
        const [st, nodes, ha, sys, bk, tg, sv] = await Promise.all([
            api('GET', '/api/settings').catch(() => null),
            api('GET', '/api/nodes').catch(() => null),
            api('GET', '/api/ha').catch(() => null),
            api('GET', '/api/system').catch(() => null),
            api('GET', '/api/backup').catch(() => null),
            api('GET', '/api/telegram').catch(() => null),
            api('GET', '/api/servis').catch(() => null)
        ]);
        renderBackups(bk);
        if (tg) renderTelegramAdmin(tg);
        renderServis(sv);
        if (nodes) { nodesCache = nodes; renderNodes(nodes); applyNode(currentNodeCfg()); }
        if (st) applySettings(st.settings);
        if (ha) renderHa(ha);
        if (sys) renderSystem(sys);
    }

    const nq = () => 'node=' + encodeURIComponent(CURRENT_NODE || '');

    function bindSettings() {
        let adv = false;
        try { adv = localStorage.getItem('advSettings') === '1'; } catch (e) { /* ignore */ }
        setAdvanced(adv);
        $('advToggle').addEventListener('change', (e) => setAdvanced(e.target.checked));
        $('backupBtn').addEventListener('click', () => toast('Připravuji zálohu…'));
        $('importFile').addEventListener('change', async (e) => {
            const f = e.target.files[0];
            e.target.value = '';
            if (!f) return;
            const full = $('importFull').checked;
            if (!confirm(`Nahrát zálohu „${f.name}“?` + (full ? '\nPřepíše se i nastavení a doplní uživatelé.' : ''))) return;
            toast('Nahrávám zálohu…');
            try {
                const r = await fetch('/api/backup/import' + (full ? '?full=1' : ''), {
                    method: 'POST', credentials: 'same-origin', body: f,
                    headers: { 'X-CSRF-Token': ME.csrf, 'Content-Type': 'application/octet-stream' }
                });
                const d = await r.json().catch(() => ({}));
                if (!r.ok || d.ok === false) throw new Error(d.error || ('Chyba ' + r.status));
                toast(`Hotovo: ${num(d.measurements, 0)} nových záznamů, ${num(d.skipped, 0)} duplicit přeskočeno` +
                      (d.nodes ? `, ${d.nodes} čidel` : ''));
                histCache.clear(); flowsCache.clear(); loadSettingsTab(); pollState();
            } catch (err) { toast(err.message, true); }
        });
        $('calForm').addEventListener('submit', (e) => {
            e.preventDefault();
            const data = formData(e.target);
            const pts = [];
            let bad = false;
            $('calPoints').querySelectorAll('tr').forEach((tr) => {
                const mv = tr.querySelector('.cal-mv').value, cm = tr.querySelector('.cal-cm').value;
                if (mv === '' || cm === '') { bad = true; return; }
                pts.push([parseFloat(mv), parseFloat(cm)]);
            });
            if (bad || pts.length < 2) { toast('Vyplňte oba kalibrační body (napětí i výšku).', true); return; }
            data.cal_points = pts;
            action(() => api('POST', '/api/node?' + nq(), data)).then((r) => {
                if (!r) return;
                toast(r.recalc_suggested ? 'Uloženo. Pro starší data použijte „Přepočítat historii“.' : 'Uloženo.');
                loadSettingsTab(); pollState(); histCache.clear(); flowsCache.clear();
            });
        });
        $('sysForm').addEventListener('submit', (e) => {
            e.preventDefault();
            action(() => api('POST', '/api/settings', formData(e.target))).then((r) => {
                if (!r) return;
                applySettings(r.settings);
                toast(r.changed.length ? 'Nastavení uloženo.' : 'Beze změn.');
                pollState();
                if (r.changed.includes('telegram_token')) setTimeout(loadSettingsTab, 4000);   // bot se připojuje
            });
        });
        $('servisSave').addEventListener('click', () => {
            const mode = $('servisMode').value;
            if (mode !== 'off' && !confirm('Povolit vzdálený servisní přístup k tomuto zařízení?')) return;
            action(() => api('POST', '/api/servis', { mode }), 'Uloženo.').then((r) => { if (r) renderServis(r); });
        });
        $('calAddBtn').addEventListener('click', () => {
            const v = $('calActualCm').value;
            if (v === '') { toast('Zadejte skutečnou hladinu v cm.', true); return; }
            action(() => api('POST', '/api/calibration/point?' + nq(), { actual_cm: v }), 'Kalibrační bod uložen.').then((r) => {
                if (r) { $('calActualCm').value = ''; loadSettingsTab(); pollState(); }
            });
        });
        $('recomputeBtn').addEventListener('click', () => {
            if (!confirm('Přepočítat celou historii tohoto čidla podle aktuální kalibrace a vyhlazení?')) return;
            action(() => api('POST', '/api/calibration/recompute?' + nq(), {}), (r) => `Přepočteno ${r.updated} záznamů.`)
                .then(() => { histCache.clear(); flowsCache.clear(); pollState(); });
        });
        $('batCalBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/calibration/battery?' + nq(), { actual_v: $('batActualV').value }), 'Baterie zkalibrována.')
                .then((r) => { if (r) { $('batTrim').textContent = num(r.battery_trim, 4); $('batActualV').value = ''; pollState(); } });
        });
        $('addNodeForm').addEventListener('submit', (e) => {
            e.preventDefault();
            startPairing($('newNodeId').value, $('newNodeName').value).then((ok) => { if (ok) e.target.reset(); });
        });
        $('haShow').addEventListener('click', () => {
            const t = $('haToken');
            t.type = t.type === 'password' ? 'text' : 'password';
            $('haShow').textContent = t.type === 'password' ? 'Zobrazit' : 'Skrýt';
        });
        $('haCopy').addEventListener('click', () => {
            const txt = $('haYaml').textContent.replace('<TOKEN>', $('haToken').value);
            navigator.clipboard.writeText(txt).then(() => toast('YAML zkopírován (včetně tokenu).'),
                () => toast('Kopírování selhalo – označte text ručně.', true));
        });
        $('haRotate').addEventListener('click', () => {
            if (!confirm('Vygenerovat nový token? Starý přestane v Home Assistantu fungovat.')) return;
            action(() => api('POST', '/api/ha/rotate', {}), 'Nový token vygenerován.').then(loadSettingsTab);
        });
        $('updateBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/system/update', {}), (r) => r.message);
        });
    }

    async function startPairing(id, name) {
        const r = await action(() => api('POST', '/api/pairing/start', { node_id: id, name: name || '' }),
            'Čekám na čidlo…');
        if (r) await refreshNodes();
        return !!r;
    }

    async function refreshNodes() {
        try {
            const nodes = await api('GET', '/api/nodes');
            nodesCache = nodes;
            renderNodes(nodes);
        } catch (e) { /* ignore */ }
    }

    function renderNodes(d) {
        const r = d.radio || {};
        $('radioInfo').innerHTML = `
            <dt>Přijímač</dt><dd>${r.ok ? 'v pořádku' : 'nedostupný'} (${esc(r.mode || '?')}${r.chip_version != null ? ', CC1101 v0x' + Number(r.chip_version).toString(16).toUpperCase() : ''})</dd>
            <dt>Resety rádia</dt><dd>${esc(r.resets)}</dd>
            <dt>Chybné CRC / cizí pakety</dt><dd>${esc(r.crc_errors)} / ${esc(r.foreign)}</dd>
            <dt>Neověřené podpisy / opakované pakety</dt><dd>${esc(r.auth_failures)} / ${esc(r.replays)}</dd>`;

        const body = $('nodesBody');
        body.textContent = '';
        if (!d.summaries.length) body.innerHTML = '<tr><td colspan="6" class="muted">Zatím žádné čidlo</td></tr>';
        d.summaries.forEach((n) => {
            const tr = document.createElement('tr');
            const st = n.unpairing ? 'odpárování – vyjměte a vložte baterii'
                : ({ online: 'online', stale: 'zpožděná data', offline: 'offline' }[n.status] || n.status);
            tr.innerHTML = `<td><strong>${esc(n.name)}</strong>${n.refill_configured ? `<span title="Ovládá dopouštění">${ico('drop', 'ico-sm')}</span>` : ''}${n.paired ? `<span title="Spárováno">${ico('lock', 'ico-sm')}</span>` : ' <span class="muted small">(nespárováno)</span>'}</td><td><code>${esc(n.id)}</code></td>
                <td>${esc(st)}${!n.unpairing && n.age_s != null ? ' · ' + fmtAgo(n.age_s) : ''}</td><td>${num(n.rssi, 0, ' dBm')}</td>
                <td>${num(n.battery && n.battery.percent, 0, ' %')}</td><td class="td-actions"></td>`;
            const btn = (label, cls, fn) => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-sm ' + cls; b.textContent = label;
                b.addEventListener('click', fn);
                tr.lastElementChild.appendChild(b);
            };
            const done = () => { loadSettingsTab(); pollState(); };
            if (n.unpairing) {
                btn('Zrušit', 'btn-outline', () => action(() => api('POST', '/api/nodes/unpair/cancel', { node_id: n.id }), 'Odpárování zrušeno.').then(done));
                btn('Odebrat bez čidla', 'btn-danger', () => {
                    if (!confirm(`Odebrat čidlo ${n.id} bez potvrzení?\nJen pro ztracené nebo zničené čidlo – pokud se najde, bude nutné ho přeprogramovat.`)) return;
                    action(() => api('POST', '/api/nodes/remove', { node_id: n.id, force: true }), 'Čidlo odebráno.').then(done);
                });
            } else {
                btn('Nastavit', 'btn-outline', () => { selectNode(n.id); applyNode(currentNodeCfg()); window.scrollTo({ top: 0, behavior: 'smooth' }); });
                btn('Odpárovat', 'btn-danger', () => {
                    if (!confirm(`Odpárovat čidlo ${n.name} (${n.id})?\nDokončí se po vyjmutí a vložení baterie do čidla. Naměřená data zůstanou.`)) return;
                    action(() => api('POST', '/api/nodes/remove', { node_id: n.id }), (r) => r.removed ? 'Čidlo odebráno.' : 'Vyjměte a vložte do čidla baterii.').then(done);
                });
            }
            body.appendChild(tr);
        });

        renderPairSteps(d);
        const pb = $('pairBody');
        pb.textContent = '';
        const pr = (d.pairing && d.pairing.requests) || [];
        const targets = (d.pairing && d.pairing.targets) || [];
        if (!pr.length && !targets.length) {
            pb.innerHTML = '<tr><td colspan="5" class="muted">Žádné nové čidlo</td></tr>';
        }
        pr.forEach((q) => {
            const tr = document.createElement('tr');
            const foreign = q.node_paired && !q.known;   // paired to another hub – cannot be taken over
            const state = q.answered ? 'čekám na data'
                : q.target ? 'páruji…'
                : foreign ? 'patří jinému hubu'
                : q.known ? (q.node_paired ? 'výměna baterie' : 'bez klíče')
                : 'nové';
            tr.innerHTML = `<td><code>${esc(q.id)}</code></td><td>${num(q.rssi, 0, ' dBm')}</td><td>${fmtAgo(q.age_s)}</td><td>${esc(state)}</td><td></td>`;
            if (!q.target && !foreign) {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm'; b.textContent = q.known ? 'Znovu spárovat' : 'Spárovat';
                b.addEventListener('click', () => {
                    let name = '';
                    if (!q.known) {
                        name = prompt(`Název pro čidlo ${q.id} (např. Jímka). Ověřte, že ID souhlasí se štítkem čidla:`, '');
                        if (name === null) return;
                    } else if (!confirm(`Znovu spárovat čidlo ${q.id}? Starý klíč se zruší a vygeneruje se nový.`)) return;
                    startPairing(q.id, name);
                });
                tr.lastElementChild.appendChild(b);
            }
            pb.appendChild(tr);
        });
        targets.filter((t) => !pr.some((q) => q.id === t.id)).forEach((t) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><code>${esc(t.id)}</code> ${esc(t.name || '')}</td><td>–</td><td>–</td>
                <td>čekám (${Math.ceil(t.expires_in / 60)} min)</td><td></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-outline btn-sm'; b.textContent = 'Zrušit';
            b.addEventListener('click', () => action(() => api('POST', '/api/pairing/cancel', { node_id: t.id })).then(refreshNodes));
            tr.lastElementChild.appendChild(b);
            pb.appendChild(tr);
        });

        const hb = $('heardBody');
        hb.textContent = '';
        if (!d.heard.length) {
            hb.innerHTML = '<tr><td colspan="5" class="muted">Zatím nic nepřijato.</td></tr>';
        }
        d.heard.forEach((h) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><code>${esc(h.id)}</code>${h.added ? ico('check', 'ico-sm') : ''}</td><td>${num(h.rssi, 0, ' dBm')}</td>
                <td>${fmtAgo(h.age_s)}</td><td>${esc(h.count)}</td><td class="muted small">${h.kind === 'pair' ? 'nabízí párování' : (h.added ? 'data' : 'cizí čidlo')}</td>`;
            hb.appendChild(tr);
        });
    }

    function renderPairSteps(d) {
        const pr = (d.pairing && d.pairing.requests) || [];
        const targets = (d.pairing && d.pairing.targets) || [];
        const offering = pr.some((q) => !q.node_paired && !q.target);
        const answered = targets.some((t) => t.answered) || pr.some((q) => q.answered);
        const waiting = targets.length > 0;
        let step = 1;
        if (offering || waiting) step = 2;
        if (answered) step = 3;
        if (!offering && !waiting && pairDone > Date.now()) step = 4;
        if (answered) pairDone = Date.now() + 60000;
        document.querySelectorAll('#pairSteps li').forEach((li) => {
            const n = +li.dataset.step;
            li.classList.toggle('done', n < step || (step === 4 && n === 4));
            li.classList.toggle('active', n === step && step !== 4);
        });
    }
    let pairDone = 0;

    function slug(s) {
        return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    }

    function haYaml(host, port, nodes) {
        const avail = `        availability: "{{ value_json is defined and value_json.status != 'offline' }}"\n`;
        const S = [
            ['hladina', 'water_level_percent', '        unit_of_measurement: "%"\n        state_class: measurement\n        icon: mdi:water-percent\n'],
            ['hladina cm', 'water_level_cm', '        unit_of_measurement: "cm"\n        device_class: distance\n        state_class: measurement\n'],
            ['objem', 'volume_liters', '        unit_of_measurement: "L"\n        device_class: volume_storage\n        state_class: measurement\n'],
            ['teplota', 'temperature', '        unit_of_measurement: "°C"\n        device_class: temperature\n        state_class: measurement\n'],
            ['vlhkost', 'humidity', '        unit_of_measurement: "%"\n        device_class: humidity\n        state_class: measurement\n'],
            ['baterie', 'battery_percent', '        unit_of_measurement: "%"\n        device_class: battery\n        state_class: measurement\n'],
            ['baterie napeti', 'battery_voltage', '        unit_of_measurement: "V"\n        device_class: voltage\n        state_class: measurement\n'],
            ['signal', 'rssi', '        unit_of_measurement: "dBm"\n        device_class: signal_strength\n        state_class: measurement\n        entity_category: diagnostic\n']
        ];
        let y = '# configuration.yaml – Retenční nádrž (platforma REST, 1 dotaz na čidlo → více entit)\nrest:\n';
        (nodes.length ? nodes : [{ id: '', name: '' }]).forEach((n, i) => {
            // první čidlo: sensor.retencka_hladina, …; další: sensor.retencka_<nazev>_hladina, …
            const pre = i === 0 ? 'Retencka' : `Retencka ${slug(n.name) || n.id.toLowerCase()}`;
            const uid = i === 0 ? 'retencka' : `retencka_${n.id.toLowerCase()}`;
            y += `  # ${n.name || 'čidlo'} (${n.id || '?'})\n  - resource: http://${host}:${port}/api/v1/water/state${n.id ? '?node=' + n.id : ''}\n`;
            y += '    headers:\n      Authorization: "Bearer <TOKEN>"\n    scan_interval: 60\n    timeout: 10\n    sensor:\n';
            S.forEach(([nm, key, extra]) => {
                y += `      - name: "${pre} ${nm}"\n        unique_id: ${uid}_${slug(nm)}\n        value_template: "{{ value_json.${key} }}"\n${extra}${avail}`;
            });
            y += `      - name: "${pre} stav"\n        unique_id: ${uid}_stav\n        value_template: "{{ value_json.status }}"\n        icon: mdi:access-point\n`;
            y += `    binary_sensor:\n      - name: "${pre} baterie slaba"\n        unique_id: ${uid}_baterie_slaba\n        value_template: "{{ value_json.battery_low }}"\n        device_class: battery\n`;
            y += `      - name: "${pre} dopousti"\n        unique_id: ${uid}_dopousti\n        value_template: "{{ value_json.is_refilling }}"\n        device_class: running\n`;
        });
        return y;
    }

    function renderHa(d) {
        $('haToken').value = d.token;
        $('haYaml').textContent = haYaml((d.hostnames || [])[0] || location.hostname, d.port, d.nodes || []);
    }

    function renderSystem(d) {
        const up = d.update || {};
        $('sysInfo').innerHTML = `
            <dt>Verze</dt><dd>${esc(d.version)}</dd>
            <dt>Adresa v síti</dt><dd>${(d.hostnames || []).map((h) => 'http://' + esc(h)).join('<br>')}</dd>
            <dt>Běží</dt><dd>${fmtDuration(d.uptime_s)}</dd>
            <dt>Paměť procesu</dt><dd>${d.rss_kb ? num(d.rss_kb / 1024, 1, ' MB') : '–'}</dd>
            <dt>Databáze / volné místo</dt><dd>${num(d.db_mb, 1, ' MB')} / ${num(d.disk_free_mb, 0, ' MB')}</dd>
            <dt>Aktualizace</dt><dd>${up.checked_at ? esc(up.message || '') + ' (' + fmtDateTime(up.checked_at * 1000) + ')' : 'zatím nekontrolováno'}</dd>`;
    }

    // ---------------------------------------------------------------- audit
    // [ikona, text, závažnost]  závažnost: '' | ok | warn | bad | info
    const ACTIONS = {
        // ikona = název symbolu ve sprite (index.html)
        LOGIN_SUCCESS: ['key', 'Přihlášení', 'ok'], LOGIN_FAILED: ['block', 'Neúspěšné přihlášení', 'bad'],
        LOGOUT: ['logout', 'Odhlášení', ''], SETUP_LOGIN: ['key', 'První přihlášení', 'warn'],
        SETUP_FAILED: ['block', 'Neplatné výchozí přihlášení', 'bad'], PASSWORD_CHANGED: ['key', 'Změna hesla', ''],
        USER_ADDED: ['user', 'Nový uživatel', 'ok'], USER_DELETED: ['user', 'Smazán uživatel', 'warn'],
        USER_ROLE_CHANGED: ['user', 'Změna role', 'warn'],
        SETTINGS_CHANGED: ['sliders', 'Změna nastavení', ''], CALIBRATION: ['ruler', 'Kalibrace', ''],
        MANUAL_REFILL: ['drop', 'Dopouštění (ručně)', 'info'], AUTO_REFILL: ['drop', 'Dopouštění (automaticky)', 'info'],
        RAIN_WAIT: ['rain', 'Čekání na déšť', 'info'], REFILL_ERROR: ['alert', 'Chyba dopouštění', 'bad'],
        REFILL_FAULT: ['alert', 'Porucha dopouštění', 'bad'],
        NODE_PAIRED: ['antenna', 'Čidlo spárováno', 'ok'], NODE_PAIRING: ['antenna', 'Čekání na párování', ''],
        NODE_REMOVED: ['antenna', 'Čidlo odebráno', 'warn'], NODE_UNPAIR_REQUESTED: ['antenna', 'Odpárování zahájeno', 'warn'],
        NODE_UNPAIRED: ['antenna', 'Čidlo odpárováno', 'warn'], NODE_UNPAIR_CANCELLED: ['antenna', 'Odpárování zrušeno', ''],
        NODE_BOOT: ['restart', 'Restart čidla', ''],
        TIME_FIXED: ['clock', 'Oprava času', ''], UPDATE_REQUESTED: ['arrow-up', 'Aktualizace', ''],
        BACKUP_DOWNLOADED: ['archive', 'Stažení zálohy', ''], BACKUP_IMPORTED: ['archive', 'Nahrání zálohy', 'warn'],
        DISK_LOW: ['alert', 'Málo místa na kartě', 'bad']
    };
    const LOG = { page: 1, cat: '', items: 0 };
    const DAYS = ['neděle', 'pondělí', 'úterý', 'středa', 'čtvrtek', 'pátek', 'sobota'];

    function dayTitle(ms) {
        const d = new Date(ms), today = new Date();
        const y = new Date(Date.now() - 86400e3);
        const same = (a, b) => a.toDateString() === b.toDateString();
        if (same(d, today)) return 'Dnes';
        if (same(d, y)) return 'Včera';
        return `${DAYS[d.getDay()]} ${d.getDate()}. ${d.getMonth() + 1}. ${d.getFullYear()}`;
    }

    function auditQuery(page) {
        const p = new URLSearchParams({ page: String(page) });
        if (LOG.cat) p.set('category', LOG.cat);
        if ($('logWho').value) p.set('who', $('logWho').value);
        const q = $('logSearch').value.trim();
        if (q) p.set('q', q);
        if ($('logFrom').value) p.set('from', String(Math.floor(new Date($('logFrom').value + 'T00:00:00').getTime() / 1000)));
        if ($('logTo').value) p.set('to', String(Math.floor(new Date($('logTo').value + 'T00:00:00').getTime() / 1000) + 86400));
        return '/api/audit?' + p.toString();
    }

    async function loadAuditLog(page) {
        const list = $('auditList');
        if (page === 1) { list.innerHTML = '<div class="muted">Načítám…</div>'; LOG.lastDay = null; }
        try {
            const d = await api('GET', auditQuery(page));
            LOG.page = page;
            if (page === 1) list.textContent = '';
            const who = $('logWho');
            const known = Array.from(who.options).map((o) => o.value);
            (d.users || []).forEach((u) => {
                if (!known.includes(u)) {
                    const o = document.createElement('option');
                    o.value = u; o.textContent = u;
                    who.appendChild(o);
                }
            });
            if (!d.logs.length && page === 1) list.innerHTML = '<div class="muted log-empty">Žádné záznamy</div>';
            d.logs.forEach((l) => {
                const ms = l.ts * 1000;
                const day = dayTitle(ms);
                if (day !== LOG.lastDay) {
                    const h = document.createElement('div');
                    h.className = 'log-day';
                    h.textContent = day;
                    list.appendChild(h);
                    LOG.lastDay = day;
                }
                const [icon, text, sev] = ACTIONS[l.action] || ['dot', l.action, ''];
                const sys = !l.username || l.username.startsWith('system');
                const row = document.createElement('div');
                row.className = 'log-row ' + (sev ? 'sev-' + sev : '');
                const t = new Date(ms);
                row.innerHTML = `<div class="log-icon">${ico(icon)}</div>
                    <div class="log-main"><div class="log-title">${esc(text)}</div>
                    <div class="log-detail">${esc(l.detail || '')}</div></div>
                    <div class="log-meta"><span class="log-time">${pad(t.getHours())}:${pad(t.getMinutes())}</span>
                    <span class="log-user ${sys ? 'sys' : ''}">${sys ? 'automatika' : esc(l.username)}</span>
                    ${l.ip && !sys ? `<span class="log-ip">${esc(l.ip)}</span>` : ''}</div>`;
                list.appendChild(row);
            });
            $('logSummary').textContent = `${d.total} záznamů`;
            const pag = $('auditPagination');
            pag.textContent = '';
            if (page < d.total_pages) {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-outline'; b.textContent = 'Načíst starší';
                b.addEventListener('click', () => loadAuditLog(page + 1));
                pag.appendChild(b);
            }
        } catch (e) {
            list.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
        }
    }

    // ---------------------------------------------------------------- users
    async function loadUsers() {
        const body = $('usersBody');
        let users;
        try { users = await api('GET', '/api/users'); } catch (e) { toast(e.message, true); return; }
        body.textContent = '';
        users.forEach((u) => {
            const self = u.username.toLowerCase() === ME.username.toLowerCase();
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><strong>${esc(u.username)}</strong>${self ? ' (vy)' : ''}</td>
                <td><select class="table-select" aria-label="Role"${self ? ' disabled' : ''}>
                    <option value="user">Uživatel</option><option value="admin">Administrátor</option></select></td>
                <td>${u.last_login ? fmtDateTime(u.last_login * 1000) : '–'}</td><td class="td-actions"></td>`;
            const sel = tr.querySelector('select');
            sel.value = u.role;
            sel.addEventListener('change', () => {
                action(() => api('POST', '/api/users/role', { username: u.username, role: sel.value }), 'Role změněna.').then((r) => { if (!r) loadUsers(); });
            });
            if (!self) {
                const cell = tr.querySelector('.td-actions');
                const pw = document.createElement('button');
                pw.type = 'button'; pw.className = 'btn btn-outline btn-sm'; pw.textContent = 'Nové heslo';
                pw.addEventListener('click', () => {
                    const p = prompt(`Nové heslo pro ${u.username} (min. 10 znaků):`);
                    if (p) action(() => api('POST', '/api/users/password', { username: u.username, password: p }), 'Heslo nastaveno, uživatel byl odhlášen.');
                });
                const del = document.createElement('button');
                del.type = 'button'; del.className = 'btn btn-danger btn-sm'; del.textContent = 'Smazat';
                del.addEventListener('click', () => {
                    if (!confirm('Opravdu smazat uživatele ' + u.username + '?')) return;
                    action(() => api('POST', '/api/users/delete', { username: u.username }), 'Uživatel smazán.').then(loadUsers);
                });
                cell.appendChild(pw); cell.appendChild(del);
            }
            body.appendChild(tr);
        });
    }

    function bindTelegram() {
        $('tgLinkBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/telegram/link', {})).then((r) => {
                if (!r) return;
                $('tgLinkBox').classList.remove('hidden');
                $('tgLinkUrl').href = r.url || '#';
                $('tgLinkBot').textContent = r.bot ? '@' + r.bot : '';
                $('tgLinkCode').textContent = '/start ' + r.code;
                setTimeout(loadTelegramMine, 15000);
            });
        });
    }

    function bindUsers() {
        $('addUserForm').addEventListener('submit', (e) => {
            e.preventDefault();
            const p = $('newPassword').value;
            if (p !== $('newPasswordConfirm').value) { toast('Hesla se neshodují!', true); return; }
            action(() => api('POST', '/api/users/add', { username: $('newUsername').value.trim(), password: p, role: $('newRole').value }), 'Uživatel přidán.')
                .then((r) => { if (r) { e.target.reset(); setSelect($('newRole'), 'user'); loadUsers(); } });
        });
        $('pwForm').addEventListener('submit', (e) => {
            e.preventDefault();
            if ($('pwNew').value !== $('pwNew2').value) { toast('Hesla se neshodují!', true); return; }
            action(() => api('POST', '/api/me/password', { old_password: $('pwOld').value, new_password: $('pwNew').value }), 'Heslo bylo změněno.')
                .then((r) => { if (r) e.target.reset(); });
        });
        document.querySelectorAll('#logChips .chip').forEach((c) => c.addEventListener('click', () => {
            document.querySelectorAll('#logChips .chip').forEach((x) => x.classList.toggle('active', x === c));
            LOG.cat = c.dataset.cat;
            loadAuditLog(1);
        }));
        ['logWho', 'logFrom', 'logTo'].forEach((id) => $(id).addEventListener('change', () => loadAuditLog(1)));
        let searchT = null;
        $('logSearch').addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(() => loadAuditLog(1), 350); });
        $('auditRefresh').addEventListener('click', () => loadAuditLog(1));
        $('exportBtn').addEventListener('click', () => {
            if (CURRENT_NODE === null || CURRENT_NODE === undefined) return;
            window.location.href = `/api/export.csv?node=${CURRENT_NODE}&` + rangeQuery(rangeOf('export'));
        });
    }

    // ------------------------------------------------------------------ init
    async function init() {
        try {
            Object.assign(ME, await api('GET', '/api/me'));
            try { CURRENT_NODE = sessionStorage.getItem('node'); VIEW_ALL = sessionStorage.getItem('viewAll') === '1'; } catch (e) { /* ignore */ }
        } catch (e) { return; }
        $('userBadge').textContent = ME.username;
        $('userRole').textContent = ME.role === 'admin' ? 'Administrátor' : 'Uživatel';
        const initial = (ME.username || '?').trim().charAt(0) || '?';
        $('avatarInitial').textContent = initial;
        $('menuInitial').textContent = initial;
        $('accountBtn').title = `${ME.username} (${ME.role === 'admin' ? 'admin' : 'uživatel'})`;
        $('menuVersion').textContent = `Verze ${ME.version}`;
        $('aboutLine').textContent = `Verze ${ME.version} · ${(ME.hostnames || []).join(', ')}`;
        if (ME.role === 'admin') document.querySelectorAll('.admin-only').forEach((el) => el.classList.remove('hidden'));

        document.querySelectorAll('.range-picker').forEach(buildRangePicker);
        document.querySelectorAll('input[type="date"]').forEach(czDateInput);
        bindAccountMenu();
        // přepnutí světlý/tmavý motiv → grafy znovu s novými barvami
        if (window.matchMedia) {
            const mq = window.matchMedia('(prefers-color-scheme: dark)');
            const onScheme = () => { readTheme(); if ($('dashboard').classList.contains('active')) loadAllCharts(); };
            if (mq.addEventListener) mq.addEventListener('change', onScheme); else if (mq.addListener) mq.addListener(onScheme);
        }

        document.querySelectorAll('.tab-btn').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab, b)));
        $('logoutBtn').addEventListener('click', () => {
            api('POST', '/api/auth/logout', {}).finally(() => window.location.replace('/login'));
        });
        bindRefill();
        if (ME.role === 'admin') { bindSettings(); }
        bindUsers();
        bindTelegram();

        setViewAll(VIEW_ALL);
        await pollState();
        let tab = 'dashboard';
        try { tab = sessionStorage.getItem('tab') || 'dashboard'; } catch (e) { /* ignore */ }
        const btn = document.querySelector(`.tab-btn[data-tab="${tab}"]:not(.hidden)`) || document.querySelector('.tab-btn[data-tab="dashboard"]');
        showTab(btn.dataset.tab, btn);

        setInterval(pollState, 10000);
        setInterval(() => { if (ME.role === 'admin' && !document.hidden && $('nastaveni').classList.contains('active')) refreshNodes(); }, 4000);
        setInterval(runRefillTimer, 1000);
        setInterval(() => { if (!document.hidden && $('dashboard').classList.contains('active')) loadAllCharts(true); }, 300000);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) pollState(); });
    }

    init();
})();
