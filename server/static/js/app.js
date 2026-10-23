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
        if (id === 'nastaveni') { setSettingsScope(SET_ALL); loadSettingsTab(); }
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
            bandRefill: cssVar('--band-refill'), bandRain: cssVar('--band-rain'),
            // datové řady – dost kontrastní proti --surface (>= 4.5:1), navzájem odlišné odstíny
            s1: cssVar('--series-1'), s2: cssVar('--series-2'), s3: cssVar('--series-3'),
            s4: cssVar('--series-4'), s5: cssVar('--series-5'), s6: cssVar('--series-6')
        });
        const d = Chart.defaults;
        d.color = T.muted;
        d.borderColor = T.grid;
        d.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
        d.font.size = 12;
        d.scale.grid.color = T.grid;
        d.scale.grid.tickLength = 0;
        d.scale.border.display = false;
        d.scale.ticks.color = T.ink2;
        d.scale.ticks.font = { size: 12 };
        d.scale.ticks.padding = 6;
        d.elements.line.borderWidth = 2.25;
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
    // Zvýraznění aktuální (poslední platné) hodnoty tečkou na konci čáry
    function lastDot(values, color) {
        let last = -1;
        for (let i = (values || []).length - 1; i >= 0; i--) { if (values[i] != null) { last = i; break; } }
        return {
            pointRadius: (c) => (c.dataIndex === last ? 4 : 0),
            pointBorderWidth: (c) => (c.dataIndex === last ? 2 : 1),
            pointBorderColor: (c) => (c.dataIndex === last ? T.surface : color),
            pointBackgroundColor: color
        };
    }
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
            data: { datasets: [line(T.s1, Object.assign({ label: 'Hladina (%)', data: series(d.t, d.level_pct), backgroundColor: alpha(T.s1, 0.05), fill: 'origin', borderWidth: 2.5, spanGaps: false }, lastDot(d.level_pct, T.s1)))] },
            plugins: [bandsPlugin],
            options: Object.assign(baseOpts(), {
                plugins: {
                    legend: { display: false },
                    bands: { bands: (lastState && lastState.refill && lastState.refill.configured) ? d.bands : (d.bands || []).filter((b) => b.kind !== 'refill') },
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
    const PALETTE = () => [T.s1, T.s2, T.s3, T.s4, T.s5, T.s6];
    async function loadAllNodesChart() {
        const nodes = (lastState && lastState.nodes) || [];
        if (!nodes.length) return;
        const r = rangeOf('all');
        const res = await Promise.all(nodes.map((n) =>
            api('GET', `/api/history?node=${n.id}&` + rangeQuery(r)).then((d) => ({ n, d })).catch(() => null)));
        const pal = PALETTE();
        const datasets = res.filter(Boolean).map(({ n, d }, i) => line(pal[i % pal.length], {
            label: n.name, data: series(d.t, d.level_pct), spanGaps: false, ...lastDot(d.level_pct, pal[i % pal.length])
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
                    line(T.s2, { label: 'Teplota (°C)', data: series(d.t, d.temp), yAxisID: 'y' }),
                    line(T.s1, { label: 'Vlhkost (%)', data: series(d.t, d.hum), yAxisID: 'y1' })
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
                    line(T.s3, { label: 'Napětí baterie (V)', data: series(d.t, d.vbat), spanGaps: true, yAxisID: 'y' }),
                    line(T.s4, { label: 'Signál (dBm)', data: series(d.t, d.rssi || []), borderWidth: 1.5, yAxisID: 'y1' })
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
        y: { beginAtZero: true, suggestedMax: 100, ticks: { precision: 0, callback: (v) => num(v, 0) + ' L', maxTicksLimit: 6 } }
    });
    const barOpts = () => ({ responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: barScales() });

    async function loadConsumptionChart() {
        const rows = await getFlows(rangeOf('consumption'));
        upsert('consumption', 'consumptionChart', {
            type: 'bar',
            data: {
                labels: dayLabels(rows),
                datasets: [
                    { label: 'Denní spotřeba (L)', data: rows.map((x) => x.use), backgroundColor: alpha(T.s2, 0.9) },
                    { label: 'Přítok ze srážek (L)', data: rows.map((x) => x.rain), backgroundColor: alpha(T.s1, 0.9) }
                ]
            },
            options: barOpts()
        });
    }

    async function loadRefillChart() {
        const rows = await getFlows(rangeOf('refill'));
        upsert('refill', 'refillChart', {
            type: 'bar',
            data: { labels: dayLabels(rows), datasets: [{ label: 'Denní dopouštění (L)', data: rows.map((x) => x.refill), backgroundColor: alpha(T.s3, 0.9) }] },
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
                    { label: 'Srážky (mm)', data: d.precipitation_sum || [], backgroundColor: alpha(T.s1, 0.9), yAxisID: 'y', order: 2 },
                    line(T.s2, { label: 'Max teplota (°C)', data: d.temperature_2m_max || [], type: 'line', yAxisID: 'y1', order: 1, pointRadius: 2.5 })
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
            if (k === 'refill' && document.body.classList.contains('no-refill')) return;   // karta je skrytá
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
        // Bez ventilu na dashboardu nic o dopouštění; ventil jiné nádrže → jen poznámka.
        // (Běžící dopouštění se ukáže i po změně nastavení, aby šlo zastavit.)
        const hasValve = !!rf.configured || !!rf.active;
        const wasHidden = document.body.classList.contains('no-refill');
        document.body.classList.toggle('no-refill', !hasValve);
        if (wasHidden && hasValve && lastTs !== null && $('dashboard').classList.contains('active')) reloadChart('refill');
        $('refillBox').classList.toggle('hidden', !hasValve && !zc);
        $('refillTitle').classList.toggle('hidden', !hasValve);
        $('refillActions').classList.toggle('hidden', !(ME.role === 'admin' && hasValve));
        $('refillResumeBtn').classList.toggle('hidden', !rf.auto_paused_until);
        $('manualPctInput').max = rf.max_pct || 100;
        runRefillTimer();

        // výstrahy
        const warn = [];
        if (s.node && s.status === 'offline') warn.push('Čidlo neposílá data – zkontrolujte baterii nebo dosah signálu.');
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
                ${ico('battery')} <span class="${b.low ? 'bad' : ''}">${num(b.percent, 0, ' %')}</span>${n.refill_configured && n.refilling ? ` · <span class="ok">${ico('drop')} dopouští</span>` : ''}</div></div>`;
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
        if ($('nastaveni').classList.contains('active')) { renderSettingsBar(nodes); return; }
        const tabWithNodes = $('dashboard').classList.contains('active');
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

    // Nastavení: „Všechny nádrže“ (společné) + záložka každé nádrže (jen ta nádrž)
    let SET_ALL = true;
    try { SET_ALL = sessionStorage.getItem('setAll') !== '0'; } catch (e) { /* ignore */ }

    function setSettingsScope(all) {
        SET_ALL = !!all || !CURRENT_NODE;
        try { sessionStorage.setItem('setAll', SET_ALL ? '1' : '0'); } catch (e) { /* ignore */ }
        document.body.classList.toggle('set-all', SET_ALL);
        document.body.classList.toggle('set-tank', !SET_ALL);
        if (lastState) renderSettingsBar(lastState.nodes || []);
        if (!SET_ALL) applyNode(currentNodeCfg());
        layoutSettingsColumns();
    }

    function renderSettingsBar(nodes) {
        const bar = $('nodeBar');
        bar.classList.remove('hidden');
        bar.textContent = '';
        const tab = (label, active, fn, extra) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'node-pill' + (extra ? ' ' + extra : '') + (active ? ' active' : '');
            b.setAttribute('role', 'tab');
            b.setAttribute('aria-selected', active ? 'true' : 'false');
            b.innerHTML = label;
            b.addEventListener('click', fn);
            bar.appendChild(b);
        };
        tab('Všechny nádrže', SET_ALL, () => setSettingsScope(true), 'all');
        nodes.forEach((n) => {
            tab(`<span class="dot ${esc(n.status)}"></span>${esc(n.name)}`, !SET_ALL && n.id === CURRENT_NODE, () => {
                if (n.id !== CURRENT_NODE) selectNode(n.id);
                setSettingsScope(false);
            });
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

    // Štítky u zděditelných polí nádrže: „výchozí“ / „vlastní“ + „↺ výchozí“ (zruší vlastní hodnotu)
    function fieldBox(el) {
        const t = el.closest('.toggle');
        if (t) return t.querySelector('.toggle-text');
        const g = el.closest('.form-group');
        return g ? g.querySelector('label') : null;
    }
    function renderInheritTags(form, n) {
        form.querySelectorAll('.inh').forEach((t) => t.remove());
        const inherited = new Set(n.inherited || []), own = new Set(n.overrides || []);
        Array.from(form.elements).forEach((el) => {
            if (!el.name || !(inherited.has(el.name) || own.has(el.name))) return;
            const box = fieldBox(el);
            if (!box) return;
            const wrap = document.createElement('span');
            wrap.className = 'inh';
            const tag = document.createElement('span');
            tag.className = 'inh-tag' + (own.has(el.name) ? ' own' : '');
            tag.textContent = own.has(el.name) ? 'vlastní' : 'výchozí';
            tag.title = own.has(el.name) ? 'Vlastní hodnota této nádrže' : 'Zděděno z výchozího nastavení nádrží';
            wrap.appendChild(tag);
            if (own.has(el.name)) {
                const r = document.createElement('button');
                r.type = 'button';
                r.className = 'inh-reset';
                r.innerHTML = ico('restart', 'ico-sm') + 'výchozí';
                r.title = 'Zrušit vlastní hodnotu a použít výchozí';
                r.addEventListener('click', (e) => {
                    e.preventDefault();
                    action(() => api('POST', '/api/node?' + nq(), { reset: [el.name] }), 'Použita výchozí hodnota.')
                        .then((res) => { if (res) { loadSettingsTab(); pollState(); } });
                });
                wrap.appendChild(r);
            } else {
                const mark = () => { tag.textContent = 'vlastní'; tag.classList.add('own', 'pending'); tag.title = 'Po uložení bude vlastní hodnotou této nádrže'; };
                el.addEventListener('input', mark, { once: true });
                el.addEventListener('change', mark, { once: true });
            }
            box.appendChild(wrap);
        });
    }

    function applyNode(n) {
        if (!n) return;
        $('calNodeName').textContent = n.name;
        $('calNodeId').value = n.id;
        fillForm($('calForm'), n);
        renderInheritTags($('calForm'), n);
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
        layoutSettingsColumns();
    }

    // Karty „Všechny nádrže“ ve sloupcích: každá karta do nejkratšího sloupce (v pořadí),
    // poslední karta sloupce se roztáhne → spodky všech sloupců na jedné lince.
    // 1 sloupec pod 1280 px, 2 do 1700 px, jinak 3. Spouští se při změně šířky/režimu/obsahu.
    let colsKey = '';
    function layoutSettingsColumns() {
        const host = $('setCols');
        if (!host) return;
        const cards = Array.from(document.querySelectorAll('#nastaveni .col-card'))
            .sort((a, b) => (+a.dataset.order) - (+b.dataset.order));
        const n = window.innerWidth >= 1700 ? 3 : window.innerWidth >= 1280 ? 2 : 1;
        let cols = Array.from(host.children);
        if (cols.length !== n) {
            host.textContent = '';
            cols = [];
            for (let i = 0; i < n; i++) { const c = document.createElement('div'); c.className = 'set-col'; host.appendChild(c); cols.push(c); }
            colsKey = '';
        }
        host.dataset.cols = String(n);
        if (!$('nastaveni').classList.contains('active') || !SET_ALL) return;    // skryté: změřit nejde
        // změřit výšky v jednom sloupci (stejná šířka jako ve výsledku)
        const heights = cards.map((c) => {
            cols[0].appendChild(c);                     // v původním pořadí
            c.classList.remove('fill');
            return c.offsetHeight;
        });
        const sum = new Array(n).fill(0), plan = [];
        cards.forEach((c, i) => {
            let k = 0;
            for (let j = 1; j < n; j++) if (sum[j] < sum[k] - 1) k = j;
            plan.push(k);
            sum[k] += heights[i] + 16;
        });
        cards.forEach((c, i) => { cols[plan[i]].appendChild(c); });
        cols.forEach((col) => {
            const vis = Array.from(col.children).filter((c) => c.offsetParent !== null);
            if (vis.length) vis[vis.length - 1].classList.add('fill');
        });
        colsKey = plan.join(',');
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
        const pill = $('tgAdminStatus');
        pill.textContent = 'Bot: ' + tgStatusText(t);
        pill.className = 'status-pill ' + (!t || !t.configured ? '' : t.connected ? 'online' : 'stale');
        const configured = !!(t && t.configured);
        $('tgChangeBtn').classList.toggle('hidden', !configured);
        if (!configured) $('tgSetup').classList.remove('hidden');
        else if (!TG_EDIT) $('tgSetup').classList.add('hidden');
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

    // NetBird: instalace z webu (root helper retencka-control, průběh v s.install)
    let NB_POLL = null;
    function renderNetbirdInstall(s) {
        const ins = s.install || {};
        const running = ins.state === 'queued' || ins.state === 'installing';
        $('nbInstallBox').classList.toggle('hidden', s.installed || running);
        $('servisBox').classList.toggle('hidden', !s.installed);
        $('nbProgress').classList.toggle('hidden', !running);
        $('nbProgressText').textContent = ins.message || 'Instaluji…';
        const res = $('nbResult');
        const fresh = ins.updated_at && Date.now() / 1000 - ins.updated_at < 3600;
        res.className = !running && fresh && ins.state === 'done' ? 'alert-info'
            : !running && fresh && ins.state === 'error' ? 'alert-danger' : 'hidden';
        res.textContent = ins.message || '';
        clearTimeout(NB_POLL);
        if (running) NB_POLL = setTimeout(() => api('GET', '/api/servis').then(renderServis).catch(() => {}), 3000);
    }

    function renderServis(s) {
        if (!s) return;
        renderNetbirdInstall(s);
        let txt;
        if (!s.installed) txt = 'NetBird není nainstalován';
        else if (s.mode === 'on') txt = 'zapnuto trvale';
        else if (s.mode === '24h' && s.until) txt = 'zapnuto do ' + fmtDateTime(s.until * 1000);
        else txt = 'vypnuto';
        if (s.installed && s.connected) txt += ` · připojeno (${s.ip || '?'})`;
        $('servisStatus').textContent = txt;
        setSelect($('servisMode'), s.mode || 'off');
    }

    let TG_EDIT = false;

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
        const [st, nodes, ha, sys, bk, tg, sv, defs] = await Promise.all([
            api('GET', '/api/settings').catch(() => null),
            api('GET', '/api/nodes').catch(() => null),
            api('GET', '/api/ha').catch(() => null),
            api('GET', '/api/system').catch(() => null),
            api('GET', '/api/backup').catch(() => null),
            api('GET', '/api/telegram').catch(() => null),
            api('GET', '/api/servis').catch(() => null),
            api('GET', '/api/node-defaults').catch(() => null)
        ]);
        if (defs) fillForm($('defForm'), defs.defaults);
        renderBackups(bk);
        if (tg) renderTelegramAdmin(tg);
        renderServis(sv);
        if (nodes) { nodesCache = nodes; renderNodes(nodes); applyNode(currentNodeCfg()); }
        if (st) applySettings(st.settings);
        if (ha) renderHa(ha);
        if (sys) renderSystem(sys);
        if (!CURRENT_NODE && !SET_ALL) setSettingsScope(true);
        layoutSettingsColumns();
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
        document.querySelectorAll('#nastaveni .col-card').forEach((c, i) => { c.dataset.order = String(i); });
        let resizeT = null;
        window.addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(layoutSettingsColumns, 150); });
        $('defForm').addEventListener('submit', (e) => {
            e.preventDefault();
            action(() => api('POST', '/api/node-defaults', formData(e.target))).then((r) => {
                if (!r) return;
                fillForm($('defForm'), r.defaults);
                toast(r.changed.length ? 'Výchozí nastavení uloženo – platí pro nádrže bez vlastní hodnoty.' : 'Beze změn.');
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
        $('tgChangeBtn').addEventListener('click', () => {
            TG_EDIT = !TG_EDIT;
            $('tgSetup').classList.toggle('hidden', !TG_EDIT);
            if (TG_EDIT) $('tgToken').focus();
            layoutSettingsColumns();
        });
        $('tgSave').addEventListener('click', async () => {
            const token = $('tgToken').value.trim();
            const msg = $('tgSetupMsg');
            if (!/^\d{5,}:[A-Za-z0-9_-]{20,}$/.test(token)) { msg.textContent = 'Token má tvar 123456789:AA… – zkopírujte ho celý ze zprávy od @BotFather.'; return; }
            $('tgSave').disabled = true;
            msg.textContent = 'Ověřuji token…';
            const r = await action(() => api('POST', '/api/settings', { telegram_token: token }));
            if (!r) { $('tgSave').disabled = false; msg.textContent = ''; return; }
            $('tgToken').value = '';
            let t = null;
            for (let i = 0; i < 12; i++) {                 // bot se připojuje na pozadí
                await new Promise((res) => setTimeout(res, 1500));
                t = await api('GET', '/api/telegram').catch(() => null);
                if (t && (t.connected || (t.error && i > 2))) break;
            }
            $('tgSave').disabled = false;
            if (t && t.connected) {
                msg.textContent = '';
                TG_EDIT = false;
                toast(`Telegram bot @${t.bot} je připojený.`);
            } else msg.textContent = (t && t.error) ? `Nepodařilo se připojit: ${t.error}.` : 'Bot se zatím nepřipojil – zkontrolujte token a připojení k internetu.';
            if (t) renderTelegramAdmin(t);
            layoutSettingsColumns();
        });
        $('nbInstallBtn').addEventListener('click', () => {
            const key = $('nbKey').value.trim(), url = $('nbUrl').value.trim();
            if (!/^[A-Za-z0-9-]{20,80}$/.test(key)) { toast('Setup key má 20–80 znaků (písmena, číslice, pomlčky).', true); return; }
            if (url && !/^https:\/\//i.test(url)) { toast('Adresa serveru musí začínat https://', true); return; }
            if (!confirm('Nainstalovat vzdálený servis (NetBird)? Servisní přístup bude povolen na 24 hodin.')) return;
            action(() => api('POST', '/api/servis/install', { setup_key: key, management_url: url }), 'Instalace spuštěna.')
                .then((r) => { if (r) { $('nbKey').value = ''; api('GET', '/api/servis').then(renderServis).catch(() => {}); } });
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
    }

    async function startPairing(id, name, msg) {
        const r = await action(() => api('POST', '/api/pairing/start', { node_id: id, name: name || '' }),
            msg || 'Čekám na čidlo…');
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
                    if (!confirm(`Odebrat čidlo ${n.id} bez potvrzení?\nJen pro ztracené nebo zničené čidlo. Hub si jeho klíč uschová – když se čidlo najde, stačí u něj v „Čidla v dosahu“ kliknout „Spárovat znovu“ a vyjmout a vložit baterii.`)) return;
                    action(() => api('POST', '/api/nodes/remove', { node_id: n.id, force: true }), 'Čidlo odebráno.').then(done);
                });
            } else {
                btn('Nastavit', 'btn-outline', () => { selectNode(n.id); setSettingsScope(false); applyNode(currentNodeCfg()); window.scrollTo({ top: 0, behavior: 'smooth' }); });
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
            // paired, but not to this hub: another hub's sensor (cannot be taken over), or one
            // removed here whose key the hub still keeps (archive) – that one can be re-paired
            const foreign = q.node_paired && !q.known && !q.archived;
            const state = q.answered ? 'čekám na data'
                : q.target ? 'páruji…'
                : foreign ? 'patří jinému hubu'
                : (q.node_paired && !q.known) ? 'odebrané čidlo (klíč v archivu)'
                : q.known ? (q.node_paired ? 'výměna baterie' : 'čidlo ztratilo klíč')
                : 'nové';
            tr.innerHTML = `<td><code>${esc(q.id)}</code></td><td>${num(q.rssi, 0, ' dBm')}</td><td>${fmtAgo(q.age_s)}</td><td>${esc(state)}</td><td class="td-actions"></td>`;
            if (!q.target && !foreign) {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm';
                b.textContent = q.known ? 'Znovu spárovat' : (q.node_paired ? 'Spárovat znovu' : 'Spárovat');
                b.addEventListener('click', () => {
                    let name = '';
                    if (!q.known && q.node_paired) {
                        if (!confirm(`Spárovat znovu čidlo ${q.id}? Hub ho uvolní uschovaným klíčem a dá mu nový.`)) return;
                    } else if (!q.known) {
                        name = prompt(`Název pro čidlo ${q.id} (např. Jímka). Ověřte, že ID souhlasí se štítkem čidla:`, '');
                        if (name === null) return;
                    } else if (!confirm(`Znovu spárovat čidlo ${q.id}? Starý klíč se zruší a vygeneruje se nový.`)) return;
                    startPairing(q.id, name);
                });
                tr.lastElementChild.appendChild(b);
            }
            pb.appendChild(tr);
        });
        ((d.pairing && d.pairing.pending) || []).filter((p) => !pr.some((q) => q.id === p.id)).forEach((p) => {
            // PAIR_ACK sent, the first data did not arrive yet: accepted for up to 24 h
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><code>${esc(p.id)}</code> ${esc(p.name || '')}</td><td>–</td><td>–</td>
                <td>čekám na první data (${Math.max(1, Math.ceil(p.expires_in / 3600))} h)</td><td class="td-actions"></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-outline btn-sm'; b.textContent = 'Zrušit';
            b.addEventListener('click', () => action(() => api('POST', '/api/pairing/cancel', { node_id: p.id })).then(refreshNodes));
            tr.lastElementChild.appendChild(b);
            pb.appendChild(tr);
        });
        targets.filter((t) => !pr.some((q) => q.id === t.id)).forEach((t) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td><code>${esc(t.id)}</code> ${esc(t.name || '')}</td><td>–</td><td>–</td>
                <td>čekám (${Math.ceil(t.expires_in / 60)} min)</td><td class="td-actions"></td>`;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'btn btn-outline btn-sm'; b.textContent = 'Zrušit';
            b.addEventListener('click', () => action(() => api('POST', '/api/pairing/cancel', { node_id: t.id })).then(refreshNodes));
            tr.lastElementChild.appendChild(b);
            pb.appendChild(tr);
        });

        const hb = $('heardBody');
        hb.textContent = '';
        if (!d.heard.length) {
            hb.innerHTML = '<tr><td colspan="6" class="muted">Zatím nic nepřijato.</td></tr>';
        }
        const targetIds = new Set(targets.map((t) => t.id));
        d.heard.forEach((h) => {
            const tr = document.createElement('tr');
            const foreign = h.kind !== 'pair' && !h.added;
            tr.innerHTML = `<td><code>${esc(h.id)}</code>${h.added ? ico('check', 'ico-sm') : ''}</td><td>${num(h.rssi, 0, ' dBm')}</td>
                <td>${fmtAgo(h.age_s)}</td><td>${esc(h.count)}</td><td class="muted small">${h.kind === 'pair' ? 'nabízí párování' : (h.added ? 'data' : 'cizí čidlo')}</td><td class="td-actions"></td>`;
            const cell = tr.lastElementChild;
            if (foreign && targetIds.has(h.id)) {
                cell.innerHTML = '<span class="muted small">Vyjměte a vložte do čidla baterii.</span>';
            } else if (foreign && h.archived) {
                // removed here (or its first data were lost): the hub still holds its key
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm'; b.textContent = 'Spárovat znovu';
                b.title = h.key_match ? 'Čidlo vysílá s klíčem uschovaným v hubu' : 'Hub má uschovaný starší klíč tohoto ID';
                b.addEventListener('click', () => {
                    if (!confirm(`Spárovat znovu čidlo ${h.id}${h.archived_name ? ' (' + h.archived_name + ')' : ''}?\nPak do čidla vyjměte a vložte baterii – hub ho uvolní uschovaným klíčem a spáruje s novým.`)) return;
                    startPairing(h.id, '', 'Vyjměte a vložte do čidla baterii.');
                });
                cell.appendChild(b);
            } else if (foreign) {
                cell.innerHTML = '<span class="muted small">Čidlo patří jinému hubu nebo bylo spárované před přeinstalací – smažte párování v čidle (4-smazat-parovani.cmd).</span>';
            }
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
            // senzor dopouštění jen pro nádrž s vlastním ventilem
            const sum = nodesCache && (nodesCache.summaries || []).find((x) => x.id === n.id);
            if (sum && sum.refill_configured) {
                y += `      - name: "${pre} dopousti"\n        unique_id: ${uid}_dopousti\n        value_template: "{{ value_json.is_refilling }}"\n        device_class: running\n`;
            }
        });
        return y;
    }

    function renderHa(d) {
        $('haToken').value = d.token;
        $('haYaml').textContent = haYaml((d.hostnames || [])[0] || location.hostname, d.port, d.nodes || []);
    }

    function renderSystem(d) {
        $('sysInfo').innerHTML = `
            <dt>Verze</dt><dd>${esc(d.version)}</dd>
            <dt>Adresa v síti</dt><dd>${(d.hostnames || []).map((h) => 'http://' + esc(h)).join('<br>')}</dd>
            <dt>Běží</dt><dd>${fmtDuration(d.uptime_s)}</dd>
            <dt>Paměť procesu</dt><dd>${d.rss_kb ? num(d.rss_kb / 1024, 1, ' MB') : '–'}</dd>
            <dt>Databáze / volné místo</dt><dd>${num(d.db_mb, 1, ' MB')} / ${num(d.disk_free_mb, 0, ' MB')}</dd>`;
        setUpdateBadge({ status: d.update, running: d.version, prefs: d.update_prefs });
    }

    // ------------------------------------------------------------ updates
    // Dialog nad stránkou: kontrola → výběr → instalace s průběhem. Stav píše root updater
    // do update-status.json, web ho čte přes /api/update/status (viz server/retencka/updates.py).
    const UPD_STEPS = [['downloading', 'Stahuji'], ['verifying', 'Ověřuji podpis'], ['installing', 'Instaluji'], ['restarting', 'Restartuji']];
    const UPD = {
        open: false, mode: null, since: 0, target: null, timer: null, failSince: null,
        last: null, opener: null, pendingSince: null
    };

    function fmtIsoDate(iso) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
        return m ? `${+m[3]}. ${+m[2]}. ${m[1]}` : '';
    }

    // Bezpečné vykreslení podmnožiny Markdownu (nadpisy, odrážky, odstavce, **tučně**, `kód`,
    // [odkaz](https://…)) – jen createElement + textContent, nikdy innerHTML z cizího textu.
    function mdInline(parent, text) {
        const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;
        let last = 0, m;
        while ((m = re.exec(text))) {
            if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
            const t = m[0];
            if (t.startsWith('**')) {
                const b = document.createElement('strong'); b.textContent = t.slice(2, -2); parent.appendChild(b);
            } else if (t.startsWith('`')) {
                const c = document.createElement('code'); c.textContent = t.slice(1, -1); parent.appendChild(c);
            } else {
                const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t);
                if (mm && /^https:\/\//i.test(mm[2])) {
                    const a = document.createElement('a');
                    a.textContent = mm[1]; a.href = mm[2]; a.target = '_blank'; a.rel = 'noopener noreferrer';
                    parent.appendChild(a);
                } else parent.appendChild(document.createTextNode(mm ? mm[1] : t));
            }
            last = m.index + t.length;
        }
        if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }
    function renderMarkdown(md) {
        const root = document.createElement('div');
        root.className = 'md';
        let list = null, para = null;
        String(md || '').split(/\r?\n/).forEach((raw) => {
            const line = raw.trimEnd();
            const h = /^(#{1,6})\s+(.*)$/.exec(line);
            const li = /^\s*[-*+]\s+(.*)$/.exec(line);
            if (!line.trim()) { list = null; para = null; return; }
            if (h) {
                list = null; para = null;
                const el = document.createElement('h4');
                mdInline(el, h[2]);
                root.appendChild(el);
            } else if (li) {
                para = null;
                if (!list) { list = document.createElement('ul'); root.appendChild(list); }
                const el = document.createElement('li');
                mdInline(el, li[1]);
                list.appendChild(el);
            } else if (list && /^\s{2,}/.test(raw)) {
                list.lastChild.appendChild(document.createTextNode(' '));
                mdInline(list.lastChild, line.trim());
            } else {
                list = null;
                if (!para) { para = document.createElement('p'); root.appendChild(para); } else para.appendChild(document.createTextNode(' '));
                mdInline(para, line.trim());
            }
        });
        return root;
    }

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text != null) e.textContent = text;
        return e;
    }
    function updButton(label, cls, fn) {
        const b = el('button', 'btn ' + cls, label);
        b.type = 'button';
        b.addEventListener('click', fn);
        return b;
    }

    function updView(kind, sum) {
        const body = $('updBody'), foot = $('updFoot');
        body.textContent = '';
        foot.textContent = '';
        const st = (sum && sum.status) || {};
        const running = (sum && sum.running) || ME.version;
        const close = () => updButton('Zavřít', 'btn-outline', closeUpdateDialog);
        $('updModal').classList.toggle('busy', kind === 'install' || kind === 'restart');
        if (kind === 'checking') {
            const row = el('div', 'upd-wait');
            row.append(el('span', 'spinner'), el('span', '', 'Hledám aktualizace…'));
            body.appendChild(row);
            foot.appendChild(close());
        } else if (kind === 'up_to_date') {
            body.appendChild(el('p', 'upd-big', `Máte nejnovější verzi ${running}.`));
            if (st.updated_at) body.appendChild(el('p', 'muted small', 'Zkontrolováno ' + fmtDateTime(st.updated_at * 1000)));
            foot.appendChild(close());
        } else if (kind === 'available') {
            const head = el('div', 'upd-avail');
            head.appendChild(el('p', 'upd-big', `K dispozici je verze ${st.latest}`));
            const meta = [`nainstalovaná ${running}`];
            if (st.published_at) meta.push('vydáno ' + fmtIsoDate(st.published_at));
            head.appendChild(el('p', 'muted small', meta.join(' · ')));
            body.appendChild(head);
            if (st.failed_before) body.appendChild(el('div', 'alert-warning', 'Tuto verzi se dříve nepodařilo spustit (hub se vrátil k předchozí). Instalaci lze zkusit znovu.'));
            if (st.message && /už není nejnovější/.test(st.message)) body.appendChild(el('div', 'alert-info', st.message));
            const notes = Array.isArray(st.notes) ? st.notes : [];
            if (notes.length) {
                const box = el('div', 'upd-notes');
                notes.forEach((n) => {
                    if (!n || typeof n !== 'object') return;
                    box.appendChild(el('h3', '', `Verze ${n.version}` + (n.date ? ' · ' + fmtIsoDate(n.date) : '')));
                    box.appendChild(renderMarkdown(n.markdown));
                });
                body.appendChild(box);
            } else body.appendChild(el('p', 'muted small', 'Poznámky k vydání nejsou k dispozici.'));
            foot.append(updButton('Později', 'btn-outline', closeUpdateDialog),
                updButton(`Aktualizovat na ${st.latest}`, 'btn-primary', () => startInstall(st.latest)));
        } else if (kind === 'error') {
            body.appendChild(el('div', 'alert-danger', st.message || 'Kontrola aktualizací selhala.'));
            foot.append(close(), updButton('Zkusit znovu', 'btn-primary', () => openUpdateDialog('check')));
        } else if (kind === 'install' || kind === 'restart') {
            body.appendChild(el('p', 'upd-big', `Aktualizace na verzi ${UPD.target}`));
            const ol = el('ol', 'upd-steps');
            const idx = kind === 'restart' ? 3 : Math.max(0, UPD_STEPS.findIndex(([k]) => k === st.state));
            UPD_STEPS.forEach(([, label], i) => {
                const li = el('li', i < idx ? 'done' : i === idx ? 'active' : '');
                li.appendChild(el('span', 'step-dot'));
                li.appendChild(el('span', '', label));
                if (i === idx) li.setAttribute('aria-current', 'step');
                ol.appendChild(li);
            });
            body.appendChild(ol);
            const msg = el('div', 'upd-wait');
            msg.append(el('span', 'spinner'), el('span', '', kind === 'restart' ? 'Čekám na restart…'
                : (UPD_STEPS.some(([k]) => k === st.state) && st.message) || 'Připravuji instalaci…'));
            body.appendChild(msg);
            body.appendChild(el('p', 'muted small', 'Nezavírejte prosím stránku, hub se během aktualizace krátce restartuje.'));
        } else if (kind === 'done') {
            body.appendChild(el('p', 'upd-big ok', `Aktualizováno na ${running}`));
            body.appendChild(el('p', 'muted small', 'Načtěte stránku znovu, ať se použije nová verze webu.'));
            foot.appendChild(updButton('Načíst znovu', 'btn-primary', () => window.location.reload()));
        } else if (kind === 'failed') {
            body.appendChild(el('div', 'alert-danger', st.message || 'Aktualizace se nezdařila.'));
            body.appendChild(el('p', 'muted small', `Dál běží předchozí verze ${running}, data ani nastavení se nezměnila.`));
            foot.append(close(), updButton('Zkusit znovu', 'btn-primary', () => openUpdateDialog('check')));
        } else if (kind === 'timeout') {
            body.appendChild(el('div', 'alert-danger', UPD.timeoutMsg || 'Služba aktualizací neodpovídá.'));
            foot.append(close(), updButton('Zkusit znovu', 'btn-primary', () => openUpdateDialog('check')));
        }
        // po překreslení držet fokus v dialogu (po otevření na hlavní akci)
        if (UPD.open && (UPD.needFocus || !$('updModal').contains(document.activeElement))) {
            UPD.needFocus = false;
            focusFirst();
        }
    }

    function focusables() {
        return Array.from($('updModal').querySelectorAll('button, [href], input, select, [tabindex]:not([tabindex="-1"])'))
            .filter((e) => !e.disabled && e.offsetParent !== null);
    }
    function focusFirst() {
        const f = $('updFoot').querySelector('.btn-primary') || $('updFoot').querySelector('button') || $('updClose');
        (f || $('updModal').querySelector('.modal')).focus();
    }

    function viewFor(sum) {
        const st = (sum && sum.status) || {};
        const s = st.state;
        if (UPD.mode === 'install') {
            if (s === 'done' && sum.running === UPD.target) return 'done';
            if (s === 'rolled_back' || (s === 'error' && (st.updated_at || 0) >= UPD.since - 1)) return 'failed';
            if (s === 'restarting' || s === 'done') return 'restart';      // čeká se na novou verzi
            return 'install';
        }
        if (UPD.mode === 'check' && ((st.updated_at || 0) < UPD.since - 1 || s === 'checking' || sum.pending)) return 'checking';
        if (s === 'available') return 'available';
        if (s === 'up_to_date') return 'up_to_date';
        if (s === 'error' || s === 'rolled_back') return 'error';
        if (s === 'done') return sum.running === st.current ? 'up_to_date' : 'available';
        if (['downloading', 'verifying', 'installing', 'restarting'].includes(s) && sum.busy) {
            UPD.mode = 'install'; UPD.target = st.target || st.latest; UPD.since = st.started_at || 0;
            return 'install';
        }
        return s ? 'checking' : 'up_to_date';
    }

    async function updPoll() {
        clearTimeout(UPD.timer);
        if (!UPD.open) return;
        let sum = null;
        try {
            const r = await fetch('/api/update/status', { credentials: 'same-origin' });
            if (r.status === 401) { window.location.replace('/login'); return; }
            if (!r.ok) throw new Error('HTTP ' + r.status);
            sum = await r.json();
        } catch (e) {
            sum = null;
        }
        if (!UPD.open) return;
        if (!sum) {                                   // server se restartuje
            if (UPD.mode === 'install') {
                UPD.failSince = UPD.failSince || Date.now();
                if (Date.now() - UPD.failSince > 180000) {
                    UPD.timeoutMsg = 'Hub se po restartu neozval do 3 minut. Zkuste stránku načíst znovu za chvíli.';
                    updView('timeout');
                    return;
                }
                updView('restart', UPD.last);
            }
            UPD.timer = setTimeout(updPoll, 1500);
            return;
        }
        UPD.failSince = null;
        UPD.last = sum;
        setUpdateBadge(sum);
        const kind = viewFor(sum);
        if (kind === 'checking' && sum.pending) {
            UPD.pendingSince = UPD.pendingSince || Date.now();
            if (Date.now() - UPD.pendingSince > 45000) {        // žádost nikdo nevyzvedl
                UPD.timeoutMsg = 'Služba aktualizací na žádost nereaguje (retencka-update.path). Zkuste to později nebo zkontrolujte journalctl -u retencka-update.';
                updView('timeout', sum);
                return;
            }
        } else UPD.pendingSince = null;
        updView(kind, sum);
        if (['checking', 'install', 'restart'].includes(kind)) UPD.timer = setTimeout(updPoll, 1500);
    }

    async function openUpdateDialog(mode) {
        const m = $('updModal');
        if (!UPD.open) {
            UPD.opener = document.activeElement;
            m.classList.remove('hidden');
            document.body.classList.add('modal-open');
            UPD.open = true;
            UPD.needFocus = true;
        }
        UPD.mode = mode;
        UPD.failSince = null;
        UPD.pendingSince = null;
        if (mode === 'check') {
            updView('checking');
            UPD.since = Math.floor(Date.now() / 1000);
            const r = await action(() => api('POST', '/api/update/check', {}));
            if (!r) {                                  // 409 = právě probíhá → jen ukázat stav
                UPD.mode = 'show';
            }
        }
        updPoll();
    }

    async function startInstall(ver) {
        UPD.target = ver;
        UPD.mode = 'install';
        UPD.since = Math.floor(Date.now() / 1000);
        updView('install', { status: { state: 'downloading', message: 'Odesílám žádost…' } });
        const r = await action(() => api('POST', '/api/update/install', { version: ver }));
        if (!r) { UPD.mode = 'show'; }
        updPoll();
    }

    function closeUpdateDialog() {
        if (!UPD.open || $('updModal').classList.contains('busy')) return;
        UPD.open = false;
        clearTimeout(UPD.timer);
        $('updModal').classList.add('hidden');
        document.body.classList.remove('modal-open');
        // řádek v kartě Systém se po zavření překreslí → fokus na stálé tlačítko
        const back = UPD.opener && UPD.opener.isConnected && !$('sysUpdate').contains(UPD.opener) ? UPD.opener : $('updateBtn');
        if (back && back.offsetParent !== null) back.focus(); else if (document.activeElement) document.activeElement.blur();
        refreshUpdateStatus();
    }

    function setUpdateBadge(sum) {
        const st = (sum && sum.status) || {};
        const avail = st.state === 'available' && st.latest && st.latest !== (sum.running || ME.version);
        $('avatarDot').classList.toggle('hidden', !avail);
        $('menuUpdDot').classList.toggle('hidden', !avail);
        renderUpdateLine(sum);
        applyUpdatePrefs(sum && sum.prefs);
    }

    function renderUpdateLine(sum) {
        const box = $('sysUpdate');
        if (!box) return;
        box.textContent = '';
        const st = (sum && sum.status) || {};
        const when = st.updated_at || st.checked_at;
        if (st.state === 'available' && st.latest) {
            const row = el('div', 'upd-avail-line');
            row.appendChild(el('span', 'badge-dot'));
            row.appendChild(el('strong', '', `K dispozici verze ${st.latest}`));
            row.appendChild(updButton('Zobrazit', 'btn-primary btn-sm', () => openUpdateDialog('show')));
            box.appendChild(row);
        }
        const last = !when ? 'Zatím nekontrolováno.'
            : `Poslední kontrola ${fmtDateTime(when * 1000)}: ${st.message || st.state || ''}`;
        box.appendChild(el('p', 'muted small', last));
    }

    function applyUpdatePrefs(p) {
        if (!p) return;
        document.querySelectorAll('.upd-auto').forEach((c) => { c.checked = !!p.auto; });
        document.querySelectorAll('.upd-channel-sel').forEach((s) => { s.value = p.channel; });
    }

    async function refreshUpdateStatus() {
        if (ME.role !== 'admin') return;
        try { setUpdateBadge(await api('GET', '/api/update/status')); } catch (e) { /* ignore */ }
    }

    function bindUpdates() {
        $('updateBtn').addEventListener('click', () => openUpdateDialog('check'));
        $('updClose').addEventListener('click', closeUpdateDialog);
        $('updModal').addEventListener('mousedown', (e) => { if (e.target === $('updModal')) closeUpdateDialog(); });
        $('updModal').addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeUpdateDialog(); return; }
            if (e.key !== 'Tab') return;
            const f = focusables();
            if (!f.length) return;
            const i = f.indexOf(document.activeElement);
            if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
            else if (!e.shiftKey && (i === -1 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
        });
        const save = (src) => {
            const box = src.closest('.upd-prefs');
            const auto = box.querySelector('.upd-auto').checked;
            const channel = box.querySelector('.upd-channel-sel').value;
            action(() => api('POST', '/api/update/prefs', { auto, channel }),
                auto ? 'Aktualizace se budou instalovat automaticky v noci.' : 'Aktualizace jen ručně.')
                .then((r) => { if (r) applyUpdatePrefs(r.prefs); else refreshUpdateStatus(); });
        };
        document.querySelectorAll('.upd-auto, .upd-channel-sel').forEach((c) => c.addEventListener('change', () => save(c)));
        refreshUpdateStatus();
        setInterval(() => { if (!document.hidden && !UPD.open) refreshUpdateStatus(); }, 60000);
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
        DISK_LOW: ['alert', 'Málo místa na kartě', 'bad'],
        TELEGRAM_LINKED: ['antenna', 'Telegram propojen', 'ok'], TELEGRAM_UNLINKED: ['antenna', 'Telegram odpojen', ''],
        SERVICE_ACCESS: ['lock', 'Vzdálený servis', 'warn'], SERVICE_RESTART: ['restart', 'Restart aplikace', ''],
        SERVICE_REBOOT: ['restart', 'Restart zařízení', 'warn']
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
        if (ME.role === 'admin') { bindSettings(); bindUpdates(); }
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
