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

    const tr = (s) => (window.I18N ? window.I18N.tr(s) : s);

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function num(v, digits, unit) {
        if (v === null || v === undefined || Number.isNaN(v)) return '–';
        const loc = (window.I18N && window.I18N.getLang() === 'en') ? 'en-US' : 'cs-CZ';
        return Number(v).toLocaleString(loc, { minimumFractionDigits: digits, maximumFractionDigits: digits }) + (unit || '');
    }

    // síla signálu v % lineárně mezi −110 dBm (mez citlivosti CC1101) a −50 dBm (velmi silný)
    function sigPct(dbm) {
        if (dbm === null || dbm === undefined || Number.isNaN(dbm)) return null;
        return Math.round(Math.min(100, Math.max(0, (dbm + 110) / 60 * 100)));
    }
    function sigTxt(dbm) { return sigPct(dbm) == null ? '–' : `${sigPct(dbm)} % (${num(dbm, 0)} dBm)`; }

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
        const isEn = window.I18N && window.I18N.getLang() === 'en';
        if (sec < 90) return isEn ? `${sec} s ago` : `před ${sec} s`;
        if (sec < 5400) return isEn ? `${Math.round(sec / 60)} min ago` : `před ${Math.round(sec / 60)} min`;
        if (sec < 172800) return isEn ? `${Math.round(sec / 3600)} h ago` : `před ${Math.round(sec / 3600)} h`;
        const days = Math.round(sec / 86400);
        return isEn ? `${days} ${days === 1 ? 'day' : 'days'} ago` : `před ${days} dny`;
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

    // ------------------------------- dialogy: potvrzení i formuláře (#cfmModal)
    // openDialog({title, body: [text | Node], fields, validate, submit, confirm, cancel, danger})
    //   fields:   [{name, label, type, value, hint, unit, placeholder, autocomplete, min, max, step, maxLength, trim}]
    //             (type 'checkbox' = zaškrtávátko → true / false)
    //   validate: (hodnoty) → text chyby | {field, message} | null     (při chybě dialog zůstane otevřený)
    //   submit:   async (hodnoty) → text chyby (dialog zůstane otevřený) | nic (zavře se)
    //   stay:     (hodnoty) → po úspěšném submit dialog nezavírat (zavře ho až tlačítko cancel)
    //   after:    [Node] pod poli · wide: širší dialog · onOpen({inputs, submit}) po otevření
    // → Promise<false | true | {hodnoty polí}>. Enter potvrdí, Esc / klik mimo dialog zruší.
    function openDialog(o) {
        return new Promise((resolve) => {
            const m = $('cfmModal'), body = $('cfmBody'), foot = $('cfmFoot');
            const opener = document.activeElement;
            const fields = o.fields || [];
            const inputs = {};
            let busy = false, stayed = null;
            $('cfmTitle').textContent = o.title;
            m.querySelector('.modal').classList.toggle('modal-md', !!o.wide);
            body.textContent = '';
            const put = (x) => {
                if (x == null) return;
                if (typeof x === 'string') { const p = document.createElement('p'); p.textContent = x; body.appendChild(p); } else body.appendChild(x);
            };
            (Array.isArray(o.body) ? o.body : [o.body]).forEach(put);
            fields.forEach((f, i) => {
                const id = 'cfmF' + i;
                const g = document.createElement('div');
                g.className = 'form-group dlg-field';
                let input;
                if (f.type === 'checkbox') {
                    const lab = document.createElement('label');
                    lab.className = 'check';
                    input = document.createElement('input');
                    input.type = 'checkbox'; input.id = id; input.checked = !!f.value;
                    lab.append(input, document.createTextNode(' ' + f.label));
                    g.appendChild(lab);
                } else {
                    const lab = document.createElement('label');
                    lab.htmlFor = id; lab.textContent = f.label;
                    input = document.createElement('input');
                    input.type = f.type || 'text'; input.id = id;
                    input.value = f.value == null ? '' : f.value;
                    if (f.placeholder) input.placeholder = f.placeholder;
                    input.autocomplete = f.autocomplete || 'off';
                    ['min', 'max', 'step'].forEach((k) => { if (f[k] != null) input[k] = f[k]; });
                    if (f.maxLength) input.maxLength = f.maxLength;
                    if (f.type === 'number') input.inputMode = 'decimal';
                    g.appendChild(lab);
                    if (f.unit) {
                        const w = document.createElement('div');
                        w.className = 'input-unit';
                        const u = document.createElement('span');
                        u.className = 'unit'; u.textContent = f.unit;
                        w.append(input, u);
                        g.appendChild(w);
                    } else {
                        input.classList.add('unified-input');
                        g.appendChild(input);
                    }
                }
                if (f.hint) { const h = document.createElement('p'); h.className = 'field-hint'; h.textContent = f.hint; g.appendChild(h); }
                inputs[f.name] = input;
                body.appendChild(g);
            });
            const err = document.createElement('p');
            err.className = 'dlg-error hidden';
            err.id = 'cfmErr';
            err.setAttribute('role', 'alert');
            body.appendChild(err);
            (o.after || []).forEach(put);
            const hideErr = () => {
                err.classList.add('hidden');
                Object.values(inputs).forEach((i) => { i.removeAttribute('aria-invalid'); i.removeAttribute('aria-describedby'); });
            };
            const fail = (e) => {
                const msg = typeof e === 'string' ? e : e.message;
                const inp = (typeof e === 'object' && inputs[e.field]) || Object.values(inputs)[0];
                err.textContent = msg;
                err.classList.remove('hidden');
                if (inp) { inp.setAttribute('aria-invalid', 'true'); inp.setAttribute('aria-describedby', 'cfmErr'); inp.focus(); }
            };
            body.addEventListener('input', hideErr);
            body.addEventListener('change', hideErr);
            const collect = () => {
                const v = {};
                fields.forEach((f) => { const i = inputs[f.name]; v[f.name] = f.type === 'checkbox' ? i.checked : (f.trim ? i.value.trim() : i.value); });
                return v;
            };
            foot.textContent = '';
            const done = (v) => {
                m.classList.add('hidden');
                document.body.classList.remove('modal-open');
                m.removeEventListener('keydown', onKey, true);
                body.removeEventListener('input', hideErr);
                body.removeEventListener('change', hideErr);
                if (opener && opener.focus && opener.offsetParent !== null) opener.focus();
                resolve(v);
            };
            const cancel = document.createElement('button');
            cancel.type = 'button'; cancel.className = 'btn btn-outline'; cancel.textContent = o.cancel || 'Zrušit';
            cancel.addEventListener('click', () => { if (!busy) done(stayed || false); });
            const ok = document.createElement('button');
            ok.type = 'button'; ok.className = 'btn ' + (o.danger ? 'btn-danger-solid' : 'btn-primary'); ok.textContent = o.confirm || 'Potvrdit';
            const submit = async () => {
                if (busy) return;
                hideErr();
                const values = collect();
                if (o.validate) { const e = o.validate(values); if (e) { fail(e); return; } }
                if (o.submit) {
                    busy = true; ok.disabled = true; cancel.disabled = true;
                    let e = null;
                    try { e = await o.submit(values); } catch (x) { e = x.message || 'Chyba spojení se serverem'; }
                    busy = false; ok.disabled = false; cancel.disabled = false;
                    if (e) { fail(e); return; }
                }
                if (o.stay) { stayed = values; o.stay(values); return; }
                done(fields.length ? values : true);
            };
            ok.addEventListener('click', submit);
            foot.append(cancel, ok);
            const onKey = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (!busy) done(stayed || false); return; }
                if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT') { e.preventDefault(); submit(); return; }
                if (e.key !== 'Tab') return;
                const f = Array.from(m.querySelectorAll('button, [href], input, select')).filter((x) => !x.disabled && x.offsetParent !== null);
                if (!f.length) return;
                const i = f.indexOf(document.activeElement);
                if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); } else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
            };
            m.addEventListener('keydown', onKey, true);
            $('cfmClose').onclick = () => { if (!busy) done(stayed || false); };
            m.onmousedown = (e) => { if (e.target === m && !busy) done(stayed || false); };
            m.classList.remove('hidden');
            document.body.classList.add('modal-open');
            const first = fields.find((f) => !f.value) ? inputs[fields.find((f) => !f.value).name]
                : fields.length ? inputs[fields[0].name] : cancel;     // formulář: první prázdné pole; potvrzení: bezpečná volba
            first.focus();
            if (first.select && first.type !== 'checkbox') first.select();
            if (o.onOpen) o.onOpen({ inputs, submit, hideErr });
        });
    }
    const confirmDialog = (o) => openDialog(o).then((r) => !!r);
    const formDialog = (o) => openDialog(o).then((r) => (r && typeof r === 'object' ? r : null));

    // Po restartu: překryv „Čekám na zařízení…“ a samo se znovu připojí
    function waitForHub(kind) {
        const ov = $('waitOverlay'), text = $('waitText'), title = $('waitTitle');
        ov.classList.remove('hidden');
        document.body.classList.add('modal-open');
        title.textContent = kind === 'poweroff' ? 'Zařízení se vypíná…' : kind === 'factory' ? 'Obnovuji tovární nastavení…' : 'Čekám na zařízení…';
        text.textContent = kind === 'restart' ? 'Aplikace se restartuje, obvykle do 30 sekund.'
            : kind === 'factory' ? 'Data se mažou a aplikace se restartuje, obvykle do 30 sekund. Pak se zobrazí první přihlášení.'
            : kind === 'reboot' ? 'Zařízení se restartuje, obvykle do 2 minut. Stránka se pak načte sama.'
                : 'Až zhasne, můžete odpojit napájení. Znovu ho zapnete odpojením a připojením napájení.';
        const start = Date.now();
        let wentDown = false;
        const tick = async () => {
            let ok = false;
            try {
                const r = await fetch('/api/me', { credentials: 'same-origin', cache: 'no-store' });
                ok = r.ok || (kind === 'factory' && r.status === 401);      // po obnově už nikdo není přihlášený
            } catch (e) { ok = false; }
            const t = Date.now() - start;
            if (!ok) wentDown = true;
            if (kind === 'poweroff') {
                if (wentDown) { title.textContent = 'Zařízení je vypnuté'; return; }
                if (t < 120000) setTimeout(tick, 3000);
                return;
            }
            if (ok && (wentDown || t > (kind === 'reboot' ? 120000 : 25000))) {
                if (kind === 'factory') window.location.replace('/login'); else window.location.reload();
                return;
            }
            if (t > 300000) {
                title.textContent = 'Zařízení se zatím neozvalo';
                text.textContent = 'Zkontrolujte napájení a síť a zkuste stránku načíst znovu.';
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary'; b.textContent = 'Načíst znovu';
                b.addEventListener('click', () => window.location.reload());
                text.after(b);
                return;
            }
            setTimeout(tick, 2000);
        };
        setTimeout(tick, 2000);
    }

    function bindPowerMenu() {
        const run = async (kind, opts) => {
            if (!await confirmDialog(opts)) return;
            const r = await action(() => api('POST', '/api/system/' + kind, {}));
            if (r) waitForHub(kind);
        };
        $('menuRestart').addEventListener('click', () => run('restart', {
            title: 'Restartovat aplikaci?',
            body: ['Aplikace hubu se ukončí a systém ji hned spustí znovu (asi 10–30 s). Měření z čidel se mezitím uloží, nic se neztratí.',
                'Běžící dopouštění se po restartu obnoví podle nastavení.'],
            confirm: 'Restartovat aplikaci'
        }));
        $('menuReboot').addEventListener('click', () => run('reboot', {
            title: 'Restartovat zařízení?',
            body: ['Celé zařízení se restartuje (asi 1–2 minuty). Po dobu restartu hub neměří a nedopouští; stránka se pak připojí sama.'],
            confirm: 'Restartovat zařízení', danger: true
        }));
        $('menuPoweroff').addEventListener('click', () => run('poweroff', {
            title: 'Vypnout zařízení?',
            body: ['Zařízení se vypne: přestane měřit, dopouštět i posílat upozornění.',
                'Znovu ho zapnete odpojením a připojením napájení.'],
            confirm: 'Vypnout zařízení', danger: true
        }));
    }

    // ------------------------------------------------------ nápověda (?)
    // <button class="help-tip" data-help="klíč"> – text z HELP (nebo přímo text v atributu).
    // Jeden popover pro všechny: hover / focus (klávesnice) ho ukáže, klepnutí / klik připne,
    // druhé klepnutí, Esc nebo klik mimo ho zavře. Zůstává uvnitř okna (i na telefonu).
    const HELP = {
        shape: 'Tvar nádrže určuje výpočet objemu z výšky hladiny. U ležatého válce se objem i procenta počítají podle kruhového průřezu (ve spodní a horní části přibývá objem pomaleji než uprostřed).',
        notify: 'Když hladina klesne pod tuto hodnotu, pošle bot zprávu do propojených chatů v Telegramu. 0 = upozornění je vypnuté.',
        battery_type: 'Podle typu baterií se z napětí počítá zbývající kapacita a výdrž. Lithiové drží napětí téměř až do vybití, alkalické klesá postupně.',
        capacity: 'Skutečná kapacita sady baterií v mAh (z jejich datasheetu). Ovlivní jen odhad výdrže, ne měření hladiny.',
        runoff: 'Jaká část dešťové vody ze střechy doteče do nádrže (0,05–1). Zbytek se ztratí odpařením, vsakem nebo přetečením okapu. Obvykle 0,7–0,9.',
        accuracy: 'Chyba tlakové sondy v % jejího rozsahu (najdete v datasheetu). Z ní hub počítá, jak velké kolísání hladiny je jen šum, a ten vyhlazuje.',
        range: 'Největší výška vodního sloupce, kterou sonda změří (např. 200 cm). Spolu s přesností určuje vyhlazení šumu.',
        rainwait: 'Když hladina klesne pod spouštěcí hladinu, ale podle předpovědi brzy zaprší, hub s dopouštěním z řadu počká a nespotřebuje zbytečně vodu z vodovodu. Čekání omezují pole níže.',
        threshold: 'Spouštěcí hladina: když hladina klesne pod ni, automatika začne dopouštět (pokud se zrovna nečeká na déšť).',
        target: 'Cílová hladina automatického dopouštění; ventil se zavře, jakmile ji nádrž dosáhne. Nikdy ale nepřekročí „Nejvýš dopustit na“.',
        rainbelow: 'Je-li hladina pod spouštěcí hladinou o víc než tolik procentních bodů, hub už na déšť nečeká a dopouští hned.',
        raindrop: 'Při čekání na déšť hladina dál klesá. Jakmile klesne o tolik procentních bodů od začátku čekání, hub čekání ukončí a začne dopouštět.',
        rainhours: 'Na kolik hodin dopředu hub hledá v předpovědi déšť. Zároveň je to nejdelší možné čekání na déšť.',
        maxpct: 'Horní pojistka: automatické ani ruční dopouštění nezvedne hladinu nad tuto hodnotu. Zbytek nádrže zůstane volný pro déšť.',
        flow: 'Kolik litrů za minutu protéká ventilem. Slouží k odhadu času a množství; hub si průtok upřesňuje z naměřených dopouštění (hodnota pod polem).',
        maxminutes: 'Pojistka času: dopouštění se po této době vždy ukončí, i když hladina cíle nedosáhla (např. při poruše čidla nebo úniku vody).',
        offset: 'Svislá vzdálenost od dna nádrže ke snímači sondy. Hladina ode dna = voda nad sondou + tato výška, proto ji zadejte podle skutečné montáže.',
        deadband: 'Nejmenší změna hladiny, kterou hub bere jako skutečnou. Menší výkyvy (vlnění, šum sondy) ignoruje. Počítá se z přesnosti a rozsahu sondy.',
        provider: 'Zařízení, kterým se nádrž dopouští z vodovodu. Bez ventilu hub jen měří a ovládání dopouštění je skryté.',
        zone: 'Zóna ovladače Hydrawise, která otevírá ventil této nádrže. Názvy zón načtete tlačítkem vedle seznamu (API Hydrawise vrací názvy zkrácené na 15 znaků bez diakritiky). Jednu zónu může ovládat jen jedna nádrž.',
        relay: 'Interní číslo zóny v Hydrawise. Vyplňujte ručně jen tehdy, když se názvy zón nepodaří načíst.',
        autorefill: 'Hub sám otevře ventil, když hladina klesne pod spouštěcí hladinu. Vypnuté = dopouští se jen ručně tlačítkem „Dopustit“.',
        retention: 'Podrobná měření (každých pár minut) se po této době mažou, hodinové průměry zůstávají navždy. 0 = nemazat. Při nedostatku místa na kartě se mažou dříve.',
        calpoints: 'Dvojice „napětí sondy – výška vody nad sondou“. Z nich hub počítá přímku, která mění napětí na hladinu. Body se doplňují tlačítkem Uložit výše; ručně je upravujte jen výjimečně.',
        recompute: 'Přepočítá už uložená měření podle aktuální kalibrace a vyhlazení. Hodí se po opravě kalibrace nebo výšky sondy; nová měření se počítají vždy správně.',
        battrim: 'Změřte napětí baterií voltmetrem a zadejte ho. Hub z toho spočítá korekci (násobek), aby údaj souhlasil. Korekce 1,0000 = bez úpravy.',
        servis: 'Na 24 hodin se přístup sám vypne, „Trvale“ zůstane zapnutý, dokud ho nevypnete. Technik se připojí přes šifrovanou síť NetBird, jen když to povolíte.'
    };
    let closeHelp = () => {};
    function bindHelpTips() {
        const pop = document.createElement('div');
        pop.id = 'helpPop';
        pop.className = 'help-pop hidden';
        pop.setAttribute('role', 'tooltip');
        document.body.appendChild(pop);
        let cur = null, pinned = false;
        const labelOf = (b) => {
            const host = b.closest('label, h3, .toggle-text');
            if (!host) return '';
            const c = host.cloneNode(true);
            c.querySelectorAll('.help-tip, .inh, .muted, svg').forEach((x) => x.remove());
            return c.textContent.replace(/\s+/g, ' ').trim();
        };
        function place() {
            if (!cur) return;
            if (cur.offsetParent === null) { hide(false); return; }       // pole zmizelo (jiná záložka)
            const r = cur.getBoundingClientRect();
            const vw = document.documentElement.clientWidth, vh = window.innerHeight;
            const w = Math.min(300, vw - 24);
            pop.style.width = w + 'px';
            const h = pop.offsetHeight;
            const left = Math.max(12, Math.min(r.left + r.width / 2 - w / 2, vw - w - 12));
            let top = r.bottom + 8, above = false;
            if (top + h > vh - 8 && r.top - 8 - h > 8) { top = r.top - 8 - h; above = true; }
            pop.style.left = left + 'px';
            pop.style.top = top + 'px';
            pop.style.setProperty('--arrow-x', Math.round(r.left + r.width / 2 - left) + 'px');
            pop.classList.toggle('above', above);
        }
        function show(b, pin) {
            if (cur && cur !== b) { cur.setAttribute('aria-expanded', 'false'); cur.removeAttribute('aria-describedby'); }
            cur = b; pinned = pin;
            const key = b.dataset.help;
            pop.textContent = (window.I18N && window.I18N.getLang() === 'en' && window.I18N.HELP_EN[key]) || HELP[key] || key;
            pop.classList.remove('hidden');
            b.setAttribute('aria-expanded', 'true');
            b.setAttribute('aria-describedby', 'helpPop');
            place();
        }
        function hide(refocus) {
            if (!cur) return;
            const b = cur;
            pop.classList.add('hidden');
            b.setAttribute('aria-expanded', 'false');
            b.removeAttribute('aria-describedby');
            cur = null; pinned = false;
            if (refocus) b.focus();
        }
        closeHelp = () => hide(false);
        document.querySelectorAll('button.help-tip[data-help]').forEach((b) => {
            if (b.dataset.bound) return;
            b.dataset.bound = '1';
            const name = labelOf(b);
            b.textContent = '?';
            b.setAttribute('aria-label', name ? 'Nápověda: ' + name : 'Nápověda');
            b.setAttribute('aria-expanded', 'false');
            b.setAttribute('aria-controls', 'helpPop');
            b.addEventListener('click', (e) => {
                e.preventDefault(); e.stopPropagation();
                if (cur === b && pinned) hide(false); else show(b, true);
            });
            b.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse' && !pinned) show(b, false); });
            b.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && cur === b && !pinned) hide(false); });
            b.addEventListener('focus', () => { if (!pinned && b.matches(':focus-visible')) show(b, false); });
            b.addEventListener('blur', () => { if (cur === b && !pinned) hide(false); });
        });
        document.addEventListener('click', (e) => { if (cur && !pop.contains(e.target) && !cur.contains(e.target)) hide(false); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && cur) { e.preventDefault(); hide(true); } });
        window.addEventListener('scroll', place, true);
        window.addEventListener('resize', place);
    }

    // ------------------------------------------------------- účet (menu)
    // Motiv: 'auto' (podle zařízení) / 'light' / 'dark'; uložený u účtu na hubu,
    // kopie v localStorage jen kvůli prvnímu vykreslení (theme.js).
    function applyTheme(theme, redraw) {
        const t = theme === 'light' || theme === 'dark' ? theme : 'auto';
        if (t === 'auto') document.documentElement.removeAttribute('data-theme');
        else document.documentElement.setAttribute('data-theme', t);
        try { if (t === 'auto') localStorage.removeItem('theme'); else localStorage.setItem('theme', t); } catch (e) { /* ignore */ }
        document.querySelectorAll('#themeSeg [data-theme-pick]').forEach((b) =>
            b.setAttribute('aria-pressed', b.dataset.themePick === t ? 'true' : 'false'));
        ME.theme = t;
        if (redraw) { readTheme(); if ($('dashboard').classList.contains('active')) loadAllCharts(); }
    }
    function bindThemeSwitch() {
        document.querySelectorAll('#themeSeg [data-theme-pick]').forEach((b) => b.addEventListener('click', (ev) => {
            ev.stopPropagation();                     // menu zůstane otevřené
            const t = b.dataset.themePick, prev = ME.theme;
            applyTheme(t, true);
            api('POST', '/api/me/theme', { theme: t }).catch(() => { applyTheme(prev, true); toast('Motiv se nepodařilo uložit.'); });
        }));
    }

    function applyLang(lang, redraw) {
        const l = lang === 'en' ? 'en' : 'cs';
        document.querySelectorAll('#langSeg [data-lang-pick]').forEach((b) =>
            b.setAttribute('aria-pressed', b.dataset.langPick === l ? 'true' : 'false'));
        ME.lang = l;
        if (window.I18N) window.I18N.setLang(l);
        if (redraw) {
            const nav = $('setNav');
            if (nav) {
                const cards = Array.from(document.querySelectorAll('#nastaveni .card[data-nav]'))
                    .filter((c) => c.offsetParent !== null)
                    .sort((a, b) => (+a.dataset.order) - (+b.dataset.order));
                const btns = nav.querySelectorAll('.set-chip');
                cards.forEach((sec, i) => {
                    if (btns[i]) btns[i].textContent = sec.dataset.nav;
                });
            }
            if (lastState) renderState(lastState);
            if ($('dashboard').classList.contains('active')) loadAllCharts();
        }
    }
    function bindLangSwitch() {
        document.querySelectorAll('#langSeg [data-lang-pick]').forEach((b) => b.addEventListener('click', (ev) => {
            ev.stopPropagation();
            const l = b.dataset.langPick, prev = ME.lang;
            applyLang(l, true);
            api('POST', '/api/me/lang', { lang: l }).catch(() => {
                applyLang(prev, true);
                toast(tr('Jazyk se nepodařilo uložit.'), true);
            });
        }));
    }

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
        closeHelp();
        document.querySelectorAll('.tab-content').forEach((el) => el.classList.remove('active'));
        document.querySelectorAll('.tab-btn').forEach((el) => { el.classList.remove('active'); el.removeAttribute('aria-current'); });
        $(id).classList.add('active');
        btn.classList.add('active');
        btn.setAttribute('aria-current', 'page');
        try { sessionStorage.setItem('tab', id); } catch (e) { /* ignore */ }
        if (lastState) renderNodeBar(lastState.nodes || []);
        if (id === 'dashboard') loadAllCharts();
        if (id === 'nastaveni') { setSettingsScope(SET_SCOPE); loadSettingsTab(); }
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
        // zvolený rozsah si graf pamatuje (localStorage, jen v tomto prohlížeči)
        let saved = null;
        try { saved = JSON.parse(localStorage.getItem('range:' + key) || 'null'); } catch (e) { saved = null; }
        if (!saved || !PRESETS[kind].some(([v]) => v === saved.preset)) saved = null;
        if (saved && saved.preset === 'custom' && !(saved.fromIso && saved.toIso)) saved = null;
        const initial = saved ? saved.preset : el.dataset.default;
        const segBtns = PRESETS[kind].map(([v, label, title]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.dataset.range = v;
            b.textContent = label;
            b.title = title;
            b.setAttribute('aria-pressed', v === initial ? 'true' : 'false');
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
        const state = { key, preset: initial, from: null, to: null };
        if (saved && saved.preset === 'custom') {
            inFrom.value = saved.fromIso;
            inTo.value = saved.toIso;
            const f = new Date(saved.fromIso + 'T00:00:00'), t = new Date(saved.toIso + 'T00:00:00');
            if (!isNaN(f) && !isNaN(t) && t >= f) {
                state.from = Math.floor(f.getTime() / 1000);
                state.to = Math.floor(t.getTime() / 1000) + 86400;
                custom.classList.remove('hidden');
            }
        }
        czDateInput(inFrom);
        czDateInput(inTo);
        pickers[key] = state;
        const remember = () => {
            try {
                localStorage.setItem('range:' + key, JSON.stringify(state.preset === 'custom'
                    ? { preset: 'custom', fromIso: inFrom.value, toIso: inTo.value } : { preset: state.preset }));
            } catch (e) { /* ignore */ }
        };

        segBtns.forEach((b) => b.addEventListener('click', () => {
            const v = b.dataset.range;
            segBtns.forEach((x) => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
            state.preset = v;
            custom.classList.toggle('hidden', v !== 'custom');
            if (v !== 'custom') { remember(); reloadChart(key); }
        }));
        custom.querySelector('.range-apply').addEventListener('click', () => {
            const f = new Date(inFrom.value + 'T00:00:00');
            const t = new Date(inTo.value + 'T00:00:00');
            if (isNaN(f) || isNaN(t) || t < f) { toast('Zadejte platný rozsah od–do.', true); return; }
            state.from = Math.floor(f.getTime() / 1000);
            state.to = Math.floor(t.getTime() / 1000) + 86400;    // včetně celého dne "do"
            remember();
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
    const MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
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
        const mList = (window.I18N && window.I18N.getLang() === 'en') ? MONTHS_EN : MONTHS;
        if (unit === 'm') return d.getMonth() === 0 ? `${mList[0]} ${d.getFullYear()}` : mList[d.getMonth()];
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
            const what = b.kind === 'refill' ? tr('Dopouštění') : tr('Přítok z deště');
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
            data: { datasets: [line(T.s1, Object.assign({ label: tr('Hladina (%)'), data: series(d.t, d.level_pct), backgroundColor: alpha(T.s1, 0.05), fill: 'origin', borderWidth: 2.5, spanGaps: false }, lastDot(d.level_pct, T.s1)))] },
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
                    line(T.s2, { label: tr('Teplota (°C)'), data: series(d.t, d.temp), yAxisID: 'y' }),
                    line(T.s1, { label: tr('Vlhkost (%)'), data: series(d.t, d.hum), yAxisID: 'y1' })
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
                    line(T.s3, { label: tr('Baterie (%)'), data: series(d.t, d.bat_pct || []), spanGaps: true, yAxisID: 'y' }),
                    line(T.s4, { label: tr('Signál (%)'), data: series(d.t, (d.rssi || []).map(sigPct)), borderWidth: 1.5, yAxisID: 'y' })
                ]
            },
            options: Object.assign(baseOpts(), {
                plugins: {
                    bootMarks: { boots: d.boots },
                    tooltip: {
                        callbacks: {
                            title: timeTooltip.callbacks.title,
                            label: (ctx) => {
                                const i = ctx.dataIndex;
                                if (ctx.datasetIndex === 1) return tr('Signál') + ' ' + sigTxt(d.rssi[i]);
                                const sag = d.sag_mv && d.sag_mv[i] ? ` (${tr('při vysílání')} −${num(d.sag_mv[i], 0)} mV)` : '';
                                return `${tr('Baterie')} ${num(d.bat_pct[i], 0, ' %')} · ${num(d.vbat[i], 2, ' V')}${sag}`;
                            },
                            footer: (items) => {
                                if (!items.length) return [];
                                const xv = items[0].parsed.x, half = Math.max(d.bucket_s || 300, 900) * 1000;
                                return (d.boots || []).filter((b) => Math.abs(b.t - xv) <= half)
                                    .map((b) => b.wdt ? tr('Restart čidla') : tr('Vložení baterie'));
                            }
                        }
                    }
                },
                scales: {
                    x: timeScale(r),
                    y: pctAxis()
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
                    { label: tr('Denní spotřeba (L)'), data: rows.map((x) => x.use), backgroundColor: alpha(T.s2, 0.9) },
                    { label: tr('Přítok ze srážek (L)'), data: rows.map((x) => x.rain), backgroundColor: alpha(T.s1, 0.9) }
                ]
            },
            options: barOpts()
        });
    }

    async function loadRefillChart() {
        const rows = await getFlows(rangeOf('refill'));
        upsert('refill', 'refillChart', {
            type: 'bar',
            data: { labels: dayLabels(rows), datasets: [{ label: tr('Denní dopouštění (L)'), data: rows.map((x) => x.refill), backgroundColor: alpha(T.s3, 0.9) }] },
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
                    { label: tr('Srážky (mm)'), data: d.precipitation_sum || [], backgroundColor: alpha(T.s1, 0.9), yAxisID: 'y', order: 2 },
                    line(T.s2, { label: tr('Max teplota (°C)'), data: d.temperature_2m_max || [], type: 'line', yAxisID: 'y1', order: 1, pointRadius: 2.5 })
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
        $('gauge').classList.toggle('empty', pct == null || pct < 0.5);        // prázdná nádrž: bez vlnky
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
        $('txtRssi').textContent = sigTxt(n.rssi);
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

    // Málo místa na kartě (jen admin): odkaz vede na kartu Záloha a data v nastavení
    function renderDiskBanner(s) {
        const b = $('diskBanner');
        const low = ME.role === 'admin' && !!s.disk_low;
        b.classList.toggle('hidden', !low);
        if (!low) { b.dataset.mb = ''; return; }
        if (b.dataset.mb === String(s.disk_free_mb)) return;
        b.dataset.mb = String(s.disk_free_mb);
        b.innerHTML = ico('alert') + `<div><strong>Na kartě zbývá jen ${esc(num(s.disk_free_mb, 0, ' MB'))}.</strong>
            Starší podrobná měření se proto postupně mažou, hodinové průměry zůstávají.
            Stáhněte si zálohu nebo smažte stará data v <a href="#" class="disk-jump">Záloha a data</a>.</div>`;
    }
    function jumpToBackup() {
        showTab('nastaveni', document.querySelector('.tab-btn[data-tab="nastaveni"]'));
        setSettingsScope('general');
        const go = () => { const c = $('genData'); if (c && c.offsetParent !== null) c.scrollIntoView({ block: 'start' }); };
        setTimeout(go, 350);
        setTimeout(go, 1000);
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
        bar.dataset.mode = 'dash';
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
            all.innerHTML = `Všechna čidla <span class="pct">${online}/${nodes.length} online</span>`;
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

    // Nastavení: záložky „Obecné“ (celý systém) | „Všechna čidla“ (čidla + výchozí hodnoty)
    // | jedna záložka na nádrž. Poslední volba se pamatuje v rámci relace.
    let SET_SCOPE = 'general';
    try { SET_SCOPE = sessionStorage.getItem('setScope') || 'general'; } catch (e) { /* ignore */ }
    if (!['general', 'tank'].includes(SET_SCOPE)) SET_SCOPE = 'general';

    function setSettingsScope(scope) {
        SET_SCOPE = (scope === 'tank' && !CURRENT_NODE) ? 'general' : (scope === 'all' ? 'general' : scope);
        try { sessionStorage.setItem('setScope', SET_SCOPE); } catch (e) { /* ignore */ }
        ['general', 'tank'].forEach((k) => document.body.classList.toggle('set-' + k, SET_SCOPE === k));
        if (lastState) renderSettingsBar(lastState.nodes || []);
        if (SET_SCOPE === 'tank') applyNode(currentNodeCfg());
        layoutTries = 0;
        layoutSettingsColumns();
        scheduleSettingsLayout();
    }

    let settingsBarKey = '';
    function renderSettingsBar(nodes) {
        const bar = $('nodeBar');
        bar.classList.remove('hidden');
        // rebuilt only on a real change (the 10 s poll must not churn the DOM)
        const key = JSON.stringify([SET_SCOPE, CURRENT_NODE, nodes.map((n) => [n.id, n.name, n.status])]);
        if (key === settingsBarKey && bar.dataset.mode === 'settings') return;
        settingsBarKey = key;
        bar.dataset.mode = 'settings';
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
        tab('Obecné', SET_SCOPE === 'general', () => setSettingsScope('general'), 'all');

        nodes.forEach((n) => {
            tab(`<span class="dot ${esc(n.status)}"></span>${esc(n.name)}`, SET_SCOPE === 'tank' && n.id === CURRENT_NODE, () => {
                if (n.id !== CURRENT_NODE) selectNode(n.id);
                setSettingsScope('tank');
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
            renderDiskBanner(s);
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
        $('refillStartBtn').addEventListener('click', async () => {
            const rf = (lastState && lastState.refill) || {};
            const maxPct = rf.max_pct || 100;
            const cur = lastState ? lastState.water_level_percent : null;
            let msg = null;
            const r = await formDialog({
                title: 'Dopustit z řadu',
                body: [`Současná hladina: ${num(cur, 1, ' %')}${lastState && lastState.volume_liters != null ? ' (' + num(lastState.volume_liters, 0, ' L') + ')' : ''}.`,
                    'Ventil se otevře a po dosažení cílové hladiny se zase zavře.'],
                fields: [{ name: 'target', type: 'number', label: 'Cílová hladina', unit: '%', min: 1, max: maxPct, step: 1,
                    value: '', hint: `Nejvýš lze dopustit na ${num(maxPct, 0, ' %')}.` }],
                validate: (v) => {
                    const t = parseFloat(String(v.target).replace(',', '.'));
                    if (!(t > 0)) return { field: 'target', message: 'Zadejte platné procento cílové hladiny.' };
                    if (t > maxPct) return { field: 'target', message: `Nejvýš lze dopustit na ${num(maxPct, 0, ' %')}.` };
                    if (cur != null && t <= cur) return { field: 'target', message: `Cílová hladina musí být vyšší než současná (${num(cur, 1, ' %')}).` };
                    return null;
                },
                submit: async (v) => {
                    try { msg = (await api('POST', '/api/refill/start?node=' + CURRENT_NODE, { target_pct: parseFloat(String(v.target).replace(',', '.')) })).message; } catch (e) { return e.message; }
                    return null;
                },
                confirm: 'Dopustit'
            });
            if (!r) return;
            toast(msg || 'Dopouštění spuštěno.');
            pollState();
        });
        $('refillResumeBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/refill/resume?node=' + CURRENT_NODE, {}), (r) => r.message).then(pollState);
        });
        $('refillStopBtn').addEventListener('click', async () => {
            const ok = await confirmDialog({
                title: 'Zastavit dopouštění?',
                body: ['Ventil se zavře a probíhající dopouštění (nebo čekání) skončí.',
                    'Automatické dopouštění se může na čas pozastavit; vrátíte ho tlačítkem „Obnovit automatiku“.'],
                confirm: 'Zastavit dopouštění', danger: true
            });
            if (!ok) return;
            action(() => api('POST', '/api/refill/stop?node=' + CURRENT_NODE, {}), (r) => r.message).then(pollState);
        });
    }

    // ------------------------------------------------------------- settings
    let nodesCache = null;     // poslední odpověď /api/nodes
    let tgConfigured = false, hwConfigured = false;

    function fillForm(form, s) {
        const isCalForm = form && form.id === 'calForm';
        const BLANK_ZERO = new Set(['max_depth_cm', 'tank_area_m2', 'tank_diameter_cm', 'tank_length_cm', 'tank_width_cm', 'catchment_area_m2', 'flow_rate_lpm']);
        Array.from(form.elements).forEach((el) => {
            if (!el.name || !(el.name in s)) return;
            if (el.type === 'checkbox') el.checked = s[el.name] === true || s[el.name] === 'true';   // přepínač
            else if (el.tagName === 'SELECT') setSelect(el, s[el.name] == null ? '' : String(s[el.name]));
            else if (el.type === 'password') { el.value = ''; el.placeholder = s[el.name] || 'nenastaveno'; }
            else {
                let v = s[el.name];
                if (isCalForm && BLANK_ZERO.has(el.name) && (v === 0 || v === 0.0 || v === '0' || v === '0.0')) v = '';
                el.value = v == null ? '' : v;
            }
        });
    }
    function formData(form) {
        const isCalForm = form && form.id === 'calForm';
        const NUMERIC_CAL = new Set(['max_depth_cm', 'tank_area_m2', 'tank_diameter_cm', 'tank_length_cm', 'tank_width_cm', 'catchment_area_m2', 'flow_rate_lpm']);
        const OPTIONAL_CAL = new Set(['catchment_area_m2', 'flow_rate_lpm']);
        const out = {};
        Array.from(form.elements).forEach((el) => {
            if (!el.name || el.disabled) return;
            if (el.type === 'password' && el.value === '') return;   // klíč beze změny
            if (el.type === 'checkbox') { out[el.name] = el.checked; return; }        // true / false jako dřív
            if (el.tagName === 'SELECT') {
                out[el.name] = el.value === 'true' ? true : el.value === 'false' ? false : el.value;
                return;
            }
            if (isCalForm && NUMERIC_CAL.has(el.name) && el.value === '') {
                const group = el.closest('.form-group');
                const isHidden = group ? group.classList.contains('hidden') : false;
                if (isHidden || OPTIONAL_CAL.has(el.name)) {
                    out[el.name] = 0;
                    return;
                }
            }
            out[el.name] = el.value;
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
        document.querySelectorAll('#nastaveni .scope-tank .inh, #' + form.id + ' .inh').forEach((t) => t.remove());
        return;
    }

    function syncTankShape(scopeRoot) {
        if (!scopeRoot) return;
        const sel = scopeRoot.querySelector('select[name="tank_shape"]');
        if (!sel) return;
        const shape = sel.value || 'area';
        scopeRoot.querySelectorAll('.form-group[data-shapes]').forEach((group) => {
            const show = group.dataset.shapes.split(',').includes(shape);
            group.classList.toggle('hidden', !show);
            group.querySelectorAll('input').forEach((input) => {
                input.required = show && scopeRoot.id === 'tkTank';
            });
        });
        layoutSettingsColumns();
    }

    function applyNode(n) {
        if (!n) return;
        $('calNodeName').textContent = n.name;
        $('calNodeId').value = n.id;
        fillForm($('calForm'), n);
        renderInheritTags($('calForm'), n);
        syncTankShape($('tkTank'));
        if ($('tkNotify')) $('tkNotify').classList.toggle('hidden', !tgConfigured);
        if ($('tkRefill')) $('tkRefill').classList.toggle('hidden', !hwConfigured && !(n && (n.hydrawise_api_key || n.refill_provider === 'hydrawise')));
        renderZoneInfo(n);
        renderCalPoints(n.cal_points);
        $('batTrim').textContent = num(n.battery_trim, 4);
        $('deadbandInfo').textContent = `±${num(n.deadband_cm, 1)} cm (≈ ${num(n.deadband_l, 0)} L)`;
        $('realFlowHint').textContent = n.real_flow_rate_lpm ? `naměřeno ${num(n.real_flow_rate_lpm, 1)} L/min` : 'z řadu';
    }

    function applySettings(s) {
        document.querySelectorAll('#nastaveni form.settings-form').forEach((f) => fillForm(f, s));
        const name = (s.location_name || '').trim();
        $('locName').textContent = name || `${num(s.latitude, 4)}, ${num(s.longitude, 4)} (bez názvu)`;
    }

    function setAdvanced(on) {
        document.body.classList.toggle('show-adv', !!on);
        $('advToggle').checked = !!on;
        $('advLabel').textContent = on ? 'základní i pokročilé nastavení' : 'základní nastavení';
        try { localStorage.setItem('advSettings', on ? '1' : ''); } catch (e) { /* ignore */ }
        layoutSettingsColumns();
    }

    // Karty aktuální záložky ve sloupcích: každá karta do nejkratšího sloupce (v pořadí),
    // poslední karta sloupce se roztáhne → spodky všech sloupců na jedné lince. Karty jiných
    // záložek čekají skryté v #setPark. 1 sloupec pod 1280 px, 2 do 1700 px, jinak 3.
    // Moving a card in the DOM blurs a focused field and makes mobile browsers jump to
    // the top, so: never while the user edits a field (re-run on focusout), and move
    // only cards whose place actually changes.
    let layoutPending = false;
    function editingSettings() {
        const a = document.activeElement;
        return !!(a && a.closest && a.closest('#nastaveni') && a.matches('input, select, textarea'));
    }
    function placeCards(col, cards) {
        const cur = Array.from(col.children);
        if (cur.length === cards.length && cur.every((c, i) => c === cards[i])) return;
        cards.forEach((c) => { if (c.parentNode !== col || col.lastElementChild !== c) col.appendChild(c); });
    }
    function layoutSettingsColumns() {
        const host = $('setCols'), park = $('setPark');
        if (!host || !park) return;
        if (editingSettings()) { layoutPending = true; return; }
        layoutPending = false;
        const all = Array.from(document.querySelectorAll('#nastaveni .col-card'))
            .sort((a, b) => (+a.dataset.order) - (+b.dataset.order));
        const mine = all.filter((c) => c.classList.contains('scope-' + SET_SCOPE) && !c.classList.contains('hidden'));
        all.forEach((c) => { if (!mine.includes(c) && c.parentNode !== park) park.appendChild(c); });
        const n = window.innerWidth >= 1700 ? 3 : window.innerWidth >= 1280 ? 2 : 1;
        let cols = Array.from(host.children);
        if (cols.length !== n) {
            mine.forEach((c) => park.appendChild(c));
            host.textContent = '';
            cols = [];
            for (let i = 0; i < n; i++) { const c = document.createElement('div'); c.className = 'set-col'; host.appendChild(c); cols.push(c); }
        }
        host.dataset.cols = String(n);
        // Root cause of the "all cards in the left column" bug: after the Všechna čidla tab
        // (no column cards) the host stayed hidden, so the cards of the next tab were
        // measured inside a display:none box (all heights 0 → everything into column 0).
        host.classList.toggle('hidden', !mine.length);
        if (!$('nastaveni').classList.contains('active')) { renderSetNav(); return; }   // skryté: změřit nejde
        if (n === 1) {                                   // one column: just the order, no measuring
            placeCards(cols[0], mine);
            cols[0].querySelectorAll('.col-card.fill').forEach((c) => c.classList.remove('fill'));
            host.classList.toggle('hidden', !mine.length);
            renderSetNav();
            return;
        }
        // změřit výšky v jednom sloupci (stejná šířka jako ve výsledku), v původním pořadí
        const heights = mine.map((c) => {
            cols[0].appendChild(c);
            c.classList.remove('fill');
            return c.offsetHeight;
        });
        if (heights.every((h) => !h) && mine.length && layoutTries < 5) {   // not rendered yet: next frame
            layoutTries++;
            requestAnimationFrame(layoutSettingsColumns);
            return;
        }
        const sum = new Array(n).fill(0), plan = [];
        mine.forEach((c, i) => {
            let k = 0;
            for (let j = 1; j < n; j++) if (sum[j] < sum[k] - 1) k = j;
            plan.push(k);
            if (heights[i]) sum[k] += heights[i] + 16;
        });
        cols.forEach((col, k) => placeCards(col, mine.filter((c, i) => plan[i] === k)));
        cols.forEach((col) => {
            const vis = Array.from(col.children).filter((c) => c.offsetParent !== null);
            if (vis.length) vis[vis.length - 1].classList.add('fill');
        });
        host.classList.toggle('hidden', !mine.length);
        // self-check: an empty column next to one with 2+ cards = a bad measurement → once more
        const counts = cols.map((col) => Array.from(col.children).filter((c) => c.offsetParent !== null).length);
        if (counts.some((x) => x === 0) && counts.some((x) => x >= 2) && layoutTries < 3) {
            layoutTries++;
            requestAnimationFrame(layoutSettingsColumns);
            return;
        }
        layoutTries = 0;
        lastHeights = new Map(mine.map((c) => [c, c.offsetHeight]));
        renderSetNav();
    }

    // Karta změnila výšku (data se načetla, rozbalil se detail…) → přeskládat, ale ne při psaní
    let layoutTries = 0, lastHeights = new Map(), roT = null;
    const cardObserver = window.ResizeObserver ? new ResizeObserver(() => {
        clearTimeout(roT);
        roT = setTimeout(() => {
            if (!$('nastaveni').classList.contains('active')) return;
            const moved = Array.from(lastHeights.entries()).some(([c, h]) => c.offsetParent !== null && Math.abs(c.offsetHeight - h) > 24);
            if (moved) layoutSettingsColumns();
        }, 300);
    }) : null;
    function scheduleSettingsLayout() {
        requestAnimationFrame(() => requestAnimationFrame(layoutSettingsColumns));   // after the new tab is painted
    }

    // Rychlá navigace po kartách aktuální záložky (lepivá pod hlavičkou)
    function renderSetNav() {
        const nav = $('setNav');
        if (!nav) return;
        nav.textContent = '';
        const cards = Array.from(document.querySelectorAll('#nastaveni .card[data-nav]'))
            .filter((c) => c.offsetParent !== null)
            .sort((a, b) => (+a.dataset.order) - (+b.dataset.order));
        cards.forEach((c) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'set-chip';
            b.textContent = c.dataset.nav;
            b.addEventListener('click', () => {
                c.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
                const h = c.querySelector('h2');
                if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
            });
            nav.appendChild(b);
        });
        nav.classList.toggle('hidden', cards.length < 2);
    }

    // Poloha: hledání obce (geocoding přes hub), poloha zařízení (prohlížeč ji dá jen zabezpečené stránce), ruční souřadnice (pokročilé)
    function savePlace(lat, lon, name) {
        return action(() => api('POST', '/api/settings', { latitude: lat, longitude: lon, location_name: name }),
            name ? `Poloha nastavena: ${name}` : 'Poloha nastavena.')
            .then((r) => { if (r) { applySettings(r.settings); pollState(); } return r; });
    }

    async function geoSearch() {
        const q = $('geoQ').value.trim();
        const list = $('geoResults');
        if (q.length < 2) { toast('Zadejte aspoň 2 znaky.', true); return; }
        list.textContent = '';
        list.classList.remove('hidden');
        const wait = document.createElement('li');
        wait.className = 'muted';
        wait.textContent = 'Hledám…';
        list.appendChild(wait);
        let r = null;
        try { r = await api('GET', '/api/geocode?q=' + encodeURIComponent(q)); } catch (e) { wait.textContent = e.message; return; }
        list.textContent = '';
        if (!r.results.length) {
            const li = document.createElement('li');
            li.className = 'muted';
            li.textContent = 'Nic nenalezeno – zkuste jen název obce.';
            list.appendChild(li);
        }
        r.results.forEach((g) => {
            const li = document.createElement('li');
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'geo-item';
            const label = [g.name, g.admin1].filter(Boolean).join(', ');
            const strong = document.createElement('strong');
            strong.textContent = label;
            const meta = document.createElement('span');
            meta.className = 'muted small';
            meta.textContent = ` ${g.country || ''} · ${num(g.lat, 3)}, ${num(g.lon, 3)}`;
            b.append(strong, meta);
            b.addEventListener('click', () => savePlace(g.lat, g.lon, label).then((ok) => {
                if (ok) { list.classList.add('hidden'); $('geoQ').value = ''; layoutSettingsColumns(); }
            }));
            li.appendChild(b);
            list.appendChild(li);
        });
        layoutSettingsColumns();
    }

    function bindPlaceAndHydrawise() {
        $('geoSearch').addEventListener('click', geoSearch);
        $('geoQ').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); geoSearch(); } });
        const secure = window.isSecureContext && 'geolocation' in navigator;
        $('geoHere').classList.toggle('hidden', !secure);
        $('geoNote').textContent = secure ? '' : 'Vyhledejte obec podle názvu.';
        $('geoHere').addEventListener('click', () => {
            navigator.geolocation.getCurrentPosition(
                (pos) => savePlace(Math.round(pos.coords.latitude * 1e5) / 1e5, Math.round(pos.coords.longitude * 1e5) / 1e5, 'Poloha zařízení'),
                (err) => toast(err.code === 1 ? 'Přístup k poloze byl zamítnut.' : 'Polohu se nepodařilo zjistit.', true),
                { timeout: 15000, maximumAge: 600000 });
        });

        // Hydrawise: test klíče účtu (klíč se do prohlížeče nikdy nevrací)
        $('hwTest').addEventListener('click', async () => {
            const out = $('hwTestResult');
            const typed = $('hwForm').querySelector('[name=hydrawise_api_key]').value.trim();
            out.className = 'small field-note muted';
            out.textContent = 'Zkouším spojení…';
            try {
                const r = await api('POST', '/api/hydrawise/test', typed ? { api_key: typed } : {});
                out.className = 'small field-note ok-text';
                out.textContent = 'Připojeno' + (r.controllers && r.controllers.length ? ' – ovladač: ' + r.controllers.join(', ') : '.')
                    + (typed ? ' Klíč nezapomeňte uložit.' : '');
            } catch (e) {
                out.className = 'small field-note bad-text';
                out.textContent = e.message;
            }
            layoutSettingsColumns();
        });

        // Zóny: výběr podle názvu, uloží se relay_id
        $('zoneLoad').addEventListener('click', async () => {
            const btn = $('zoneLoad');
            btn.disabled = true;
            $('zoneInfo').textContent = 'Načítám zóny z Hydrawise…';
            try {
                const r = await api('GET', '/api/hydrawise/zones?' + nq());
                ZONES[CURRENT_NODE] = r.zones;
                renderZoneInfo(currentNodeCfg() || {}, $('calForm').elements.namedItem('hydrawise_relay_id').value);
                if (!r.zones.length) $('zoneInfo').textContent = 'Účet Hydrawise nemá žádné zóny.';
            } catch (e) {
                $('zoneInfo').textContent = e.message;
            } finally { btn.disabled = false; }
        });
        $('zoneSelect').addEventListener('change', () => {
            const relay = $('calForm').elements.namedItem('hydrawise_relay_id');
            relay.value = $('zoneSelect').value;
            relay.dispatchEvent(new Event('input'));
            zoneHint(relay.value, true);
        });
    }

    // Zóna: v seznamu názvy (po načtení z Hydrawise), pod ním číslo; bez načtení uložená zóna
    const ZONES = {};
    function zoneHint(relay, changed) {
        const zones = ZONES[CURRENT_NODE];
        const z = zones && zones.find((x) => x.relay_id === relay);
        let txt;
        if (!relay) txt = zones ? 'Bez zóny.' : 'Názvy zón načtete tlačítkem vedle.';
        else if (z) txt = (z.number != null ? `Zóna č. ${z.number} · ` : '') + `Relay ID ${relay}`;
        else txt = zones ? `Relay ID ${relay} v účtu Hydrawise nenalezeno.` : 'Názvy zón načtete tlačítkem vedle.';
        $('zoneInfo').textContent = txt + (changed ? ' Nezapomeňte uložit.' : '');
    }
    function renderZoneInfo(n, current) {
        const sel = $('zoneSelect');
        const relay = current != null ? current : (n.hydrawise_relay_id || '');
        const zones = ZONES[CURRENT_NODE];
        sel.textContent = '';
        const add = (v, t) => { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.appendChild(o); };
        add('', '– bez zóny –');
        if (zones) zones.forEach((z) => add(z.relay_id, z.name));
        if (relay && !(zones && zones.some((z) => z.relay_id === relay))) add(relay, `Zóna ${relay} (načtěte názvy)`);
        sel.value = relay;
        zoneHint(relay, false);
    }

    // Smazat naměřená data: čidlo / všechna, rozsah od–do nebo vše; nejdřív počet (dry run)
    function fillDeleteNodes() {
        const sel = $('delNode');
        const cur = sel.value;
        sel.textContent = '';
        const add = (v, t) => { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.appendChild(o); };
        add('', 'Všechna čidla');
        ((nodesCache && nodesCache.nodes) || []).forEach((n) => add(n.id, `${n.name} (${n.id})`));
        sel.value = Array.from(sel.options).some((o) => o.value === cur) ? cur : '';
    }

    function bindDataDelete() {
        const today = isoDate(new Date());
        $('delTo').max = today; $('delFrom').max = today;
        $('delRange').addEventListener('change', () => { $('delDates').classList.toggle('hidden', $('delRange').value === 'all'); layoutSettingsColumns(); });
        $('delBtn').addEventListener('click', async () => {
            const node = $('delNode').value || null;
            const body = { node, from: null, to: null, dry_run: true };
            let rangeTxt = 'vše';
            if ($('delRange').value !== 'all') {
                const f = $('delFrom').value, t = $('delTo').value;
                if (!f || !t || t < f) { toast('Zadejte platný rozsah od–do.', true); return; }
                body.from = Math.floor(new Date(f + 'T00:00:00').getTime() / 1000);
                body.to = Math.floor(new Date(t + 'T00:00:00').getTime() / 1000) + 86400;
                rangeTxt = `${fmtCzDate(f)} – ${fmtCzDate(t)}`;
            }
            let dry;
            try { dry = await api('POST', '/api/data/delete', body); } catch (e) { toast(e.message, true); return; }
            const who = node ? $('delNode').selectedOptions[0].textContent : 'všechna čidla';
            if (!dry.total) { toast(`Ve zvoleném rozsahu (${who}, ${rangeTxt}) nejsou žádná data.`); return; }
            const link = document.createElement('p');
            link.innerHTML = 'Doporučujeme nejdřív <a href="/api/backup/download" download>stáhnout zálohu</a>.';
            const ok = await confirmDialog({
                title: 'Smazat naměřená data?',
                body: [`Čidlo: ${who} · rozsah: ${rangeTxt}`,
                    `Smaže se ${num(dry.counts.measurements, 0)} měření, ${num(dry.counts.hourly, 0)} hodinových souhrnů a ${num(dry.counts.refills, 0)} záznamů o dopouštění (historie dopouštění v rozsahu jde také pryč).`,
                    'Nádrže, nastavení a záznamy v Logování zůstanou. Smazání nejde vrátit.', link],
                confirm: `Smazat ${num(dry.total, 0)} záznamů`, danger: true
            });
            if (!ok) return;
            body.dry_run = false;
            const r = await action(() => api('POST', '/api/data/delete', body), (x) => `Smazáno ${num(x.total, 0)} záznamů.`);
            if (r) { histCache.clear(); flowsCache.clear(); pollState(); }
        });
    }

    function renderBackups(d) {
        const body = $('backupBody');
        body.textContent = '';
        const list = (d && d.backups) || [];
        if (d && d.keep) $('bkKeep').textContent = String(d.keep);
        if (d && d.dir) $('bkDir').textContent = d.dir;
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
            b.addEventListener('click', async () => {
                if (!await confirmDialog({ title: 'Odpojit Telegram chat?', body: [`Chat „${c.title || c.chat_id}“ přestane dostávat upozornění z hubu.`], confirm: 'Odpojit', danger: true })) return;
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
        if ($('tgMineCard')) $('tgMineCard').classList.toggle('hidden', !(t && t.configured));
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
        if (tg) tgConfigured = !!tg.configured;
        if (st && st.settings) hwConfigured = !!st.settings.hydrawise_api_key;
        if (defs && $('defForm')) fillForm($('defForm'), defs.defaults);
        renderBackups(bk);
        if (tg) renderTelegramAdmin(tg);
        renderServis(sv);
        if (nodes) { nodesCache = nodes; renderNodes(nodes); applyNode(currentNodeCfg()); fillDeleteNodes(); }
        if (st) applySettings(st.settings);
        if (ha) renderHa(ha);
        if (sys) renderSystem(sys);
        if (!CURRENT_NODE && SET_SCOPE === 'tank') setSettingsScope('general');
        layoutSettingsColumns();
    }

    const nq = () => 'node=' + encodeURIComponent(CURRENT_NODE || '');

    function bindSettings() {
        document.querySelectorAll('#nastaveni .card').forEach((c, i) => {
            c.dataset.order = String(i);
            if (cardObserver && c.classList.contains('col-card')) cardObserver.observe(c);
        });
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
            if (!await confirmDialog({
                title: 'Nahrát zálohu?',
                body: [`Soubor: ${f.name}`, 'Naměřená data se přidají k současným, duplicity se přeskočí.',
                    full ? 'Přepíše se i nastavení a doplní se uživatelé ze zálohy.' : null],
                confirm: 'Nahrát zálohu', danger: full
            })) return;
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
        let resizeT = null;
        // only a width change can change the columns; mobile browsers fire resize on every
        // scroll (address bar) and when the keyboard opens – those must not touch the DOM
        let lastW = window.innerWidth;
        window.addEventListener('resize', () => {
            if (window.innerWidth === lastW) return;
            lastW = window.innerWidth;
            clearTimeout(resizeT); resizeT = setTimeout(layoutSettingsColumns, 150);
        });
        $('nastaveni').addEventListener('focusout', () => {
            setTimeout(() => { if (layoutPending && !editingSettings()) layoutSettingsColumns(); }, 0);
        });
        $('defForm').addEventListener('submit', (e) => {
            e.preventDefault();
            action(() => api('POST', '/api/node-defaults', formData(e.target))).then((r) => {
                if (!r) return;
                fillForm($('defForm'), r.defaults);
                toast(r.changed.length ? 'Výchozí nastavení uloženo – platí pro nádrže bez vlastní hodnoty.' : 'Beze změn.');
                loadSettingsTab(); pollState(); histCache.clear(); flowsCache.clear();
            });
        });
        document.querySelectorAll('#nastaveni form.settings-form').forEach((f) => f.addEventListener('submit', (e) => {
            e.preventDefault();
            const data = formData(e.target);
            if (e.target.id === 'sysForm') data.location_name = '';     // ručně zadané souřadnice: bez názvu místa
            action(() => api('POST', '/api/settings', data)).then((r) => {
                if (!r) return;
                applySettings(r.settings);
                toast(r.changed.length ? 'Nastavení uloženo.' : 'Beze změn.');
                pollState();
                if (r.changed.includes('telegram_token')) setTimeout(loadSettingsTab, 4000);   // bot se připojuje
            });
        }));
        document.querySelectorAll('#nastaveni select[name="tank_shape"]').forEach((sel) => {
            sel.addEventListener('change', () => {
                const root = sel.closest('#allDefaults, #tkTank');
                if (root) syncTankShape(root);
            });
        });
        syncTankShape($('tkTank'));
        bindPlaceAndHydrawise();
        bindDataDelete();
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
        $('nbInstallBtn').addEventListener('click', async () => {
            const key = $('nbKey').value.trim(), url = $('nbUrl').value.trim();
            if (!/^[A-Za-z0-9-]{20,80}$/.test(key)) { toast('Setup key má 20–80 znaků (písmena, číslice, pomlčky).', true); return; }
            if (url && !/^https:\/\//i.test(url)) { toast('Adresa serveru musí začínat https://', true); return; }
            if (!await confirmDialog({ title: 'Nainstalovat vzdálený servis?', body: ['Nainstaluje se NetBird a servisní přístup se povolí na 24 hodin.', 'Zákazník ho může kdykoli vypnout.'], confirm: 'Nainstalovat' })) return;
            action(() => api('POST', '/api/servis/install', { setup_key: key, management_url: url }), 'Instalace spuštěna.')
                .then((r) => { if (r) { $('nbKey').value = ''; api('GET', '/api/servis').then(renderServis).catch(() => {}); } });
        });
        $('servisSave').addEventListener('click', async () => {
            const mode = $('servisMode').value;
            if (mode !== 'off' && !await confirmDialog({ title: 'Povolit vzdálený servis?', body: [mode === '24h' ? 'Servisní technik se bude moci 24 hodin připojit k tomuto zařízení.' : 'Servisní technik se bude moci trvale připojit k tomuto zařízení, dokud přístup nevypnete.'], confirm: 'Povolit' })) return;
            action(() => api('POST', '/api/servis', { mode }), 'Uloženo.').then((r) => { if (r) renderServis(r); });
        });
        $('calAddBtn').addEventListener('click', () => {
            const v = $('calActualCm').value;
            if (v === '') { toast('Zadejte skutečnou hladinu v cm.', true); return; }
            action(() => api('POST', '/api/calibration/point?' + nq(), { actual_cm: v }), 'Kalibrační bod uložen.').then((r) => {
                if (r) { $('calActualCm').value = ''; loadSettingsTab(); pollState(); }
            });
        });
        $('recomputeBtn').addEventListener('click', async () => {
            if (!await confirmDialog({ title: 'Přepočítat historii?', body: ['Celá historie tohoto čidla se přepočítá podle aktuální kalibrace a vyhlazení. Může to chvíli trvat.'], confirm: 'Přepočítat' })) return;
            action(() => api('POST', '/api/calibration/recompute?' + nq(), {}), (r) => `Přepočteno ${r.updated} záznamů.`)
                .then(() => { histCache.clear(); flowsCache.clear(); pollState(); });
        });
        $('batCalBtn').addEventListener('click', () => {
            action(() => api('POST', '/api/calibration/battery?' + nq(), { actual_v: $('batActualV').value }), 'Baterie zkalibrována.')
                .then((r) => { if (r) { $('batTrim').textContent = num(r.battery_trim, 4); $('batActualV').value = ''; pollState(); } });
        });
        $('addNodeBtn').addEventListener('click', () => pairDialog(null));
        $('factoryBtn').addEventListener('click', factoryReset);
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
        $('haRotate').addEventListener('click', async () => {
            if (!await confirmDialog({ title: 'Vygenerovat nový token?', body: ['Starý token přestane v Home Assistantu fungovat; v jeho konfiguraci ho bude nutné nahradit novým.'], confirm: 'Nový token', danger: true })) return;
            action(() => api('POST', '/api/ha/rotate', {}), 'Nový token vygenerován.').then(loadSettingsTab);
        });
    }

    // Přidání čidla – celé v dialogu: kroky, ID ze štítku + povinný název, nová čidla v dosahu.
    // Dialog zůstává otevřený a kroky / tabulka se obnovují s refreshNodes (každé 4 s).
    let pairFill = null;            // otevřený dialog: „Spárovat“ v tabulce doplní ID
    async function pairDialog(knownId) {
        const panel = $('pairPanel');
        panel.classList.remove('hidden');
        await formDialog({
            title: 'Přidat čidlo', wide: true, cancel: 'Zavřít', confirm: 'Čekat na čidlo',
            body: ['Zadejte ID ze štítku čidla a jeho název, potvrďte a vložte do čidla baterii. Nové čidlo v dosahu se objeví v tabulce dole – stačí u něj kliknout „Spárovat“.',
                $('pairSteps')],
            fields: [{ name: 'id', label: 'ID ze štítku', value: knownId || '', maxLength: 6, trim: true, placeholder: '4 znaky, např. 3F2A', hint: 'Čtyři znaky 0–9 / A–F vytištěné na štítku čidla.' },
                { name: 'name', label: 'Název', maxLength: 40, trim: true, hint: 'Povinný – pod tímto jménem se nádrž ukáže v přehledu.' }],
            after: [panel],
            validate: (v) => (!/^(0x)?[0-9a-f]{1,4}$/i.test(v.id) ? { field: 'id', message: 'ID čidla jsou 4 znaky 0–9 / A–F (např. 3F2A).' }
                : !v.name ? { field: 'name', message: 'Zadejte název čidla.' } : null),
            submit: async (v) => {
                try { await api('POST', '/api/pairing/start', { node_id: v.id, name: v.name }); } catch (e) { return e.message; }
                return null;
            },
            stay: () => { toast('Čekám na čidlo – vložte do něj baterii.'); refreshNodes(); },
            onOpen: ({ inputs, submit, hideErr }) => {
                pairFill = (id) => {
                    hideErr();
                    inputs.id.value = id;
                    if (inputs.name.value.trim()) submit(); else inputs.name.focus();
                };
            }
        });
        pairFill = null;
        panel.classList.add('hidden');
        panel.prepend($('pairSteps'));
        $('pairHint').parentNode.after(panel);
    }

    // Tovární nastavení: 1. co se smaže + záloha, 2. potvrzení slovem OBNOVIT a heslem
    async function factoryReset() {
        const para = (t, strong) => { const p = document.createElement('p'); if (strong) { const b = document.createElement('strong'); b.textContent = t; p.appendChild(b); } else p.textContent = t; return p; };
        const list = (items) => { const u = document.createElement('ul'); u.className = 'dlg-list'; items.forEach((t) => { const li = document.createElement('li'); li.textContent = t; u.appendChild(li); }); return u; };
        const dl = document.createElement('p');
        const a = document.createElement('a');
        a.className = 'btn btn-primary'; a.href = '/api/backup/download'; a.setAttribute('download', ''); a.textContent = 'Stáhnout zálohu';
        dl.appendChild(a);
        const s1 = await formDialog({
            title: 'Obnovit tovární nastavení?',
            body: [para('Hub se vrátí do stavu po první instalaci. Nejde to vzít zpět.'),
                para('Smaže se:', true),
                list(['všechna naměřená data a historie', 'nastavení (poloha, klíč Hydrawise, Home Assistant)', 'všichni uživatelé včetně vás',
                    'klíče čidel – čidla bude nutné znovu spárovat', 'Telegram (token a propojené chaty)', 'zálohy uložené na kartě']),
                para('Zůstane:', true),
                list(['identita zařízení a heslo ze štítku']),
                para('Zálohy na kartě se smažou také, proto si ji stáhněte teď:'), dl],
            fields: [{ name: 'backup', type: 'checkbox', label: 'Zálohu mám / nepotřebuji' }],
            validate: (v) => (v.backup ? null : { field: 'backup', message: 'Potvrďte, že zálohu máte nebo ji nepotřebujete.' }),
            confirm: 'Pokračovat', danger: true
        });
        if (!s1) return;
        const s2 = await formDialog({
            title: 'Poslední krok',
            body: ['Napište slovo OBNOVIT a zadejte své heslo. Hub se pak restartuje a všechno uvedené se smaže.'],
            fields: [{ name: 'confirm', label: 'Potvrzení (napište OBNOVIT)', trim: true, placeholder: 'OBNOVIT' },
                { name: 'password', type: 'password', label: 'Vaše heslo', autocomplete: 'current-password' }],
            validate: (v) => (v.confirm !== 'OBNOVIT' ? { field: 'confirm', message: 'Napište přesně slovo OBNOVIT (velkými písmeny).' }
                : !v.password ? { field: 'password', message: 'Zadejte své heslo.' } : null),
            submit: async (v) => {
                try { await api('POST', '/api/system/factory-reset', { password: v.password, confirm: v.confirm }); } catch (e) { return e.message; }
                return null;
            },
            confirm: 'Obnovit tovární nastavení', danger: true
        });
        if (s2) waitForHub('factory');
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
                <td>${esc(st)}${!n.unpairing && n.age_s != null ? ' · ' + fmtAgo(n.age_s) : ''}</td><td>${sigTxt(n.rssi)}</td>
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
                btn('Odebrat bez čidla', 'btn-danger', async () => {
                    if (!await confirmDialog({
                        title: `Odebrat čidlo ${n.id} bez potvrzení?`,
                        body: ['Jen pro ztracené nebo zničené čidlo.',
                            'Hub si jeho klíč uschová – když se čidlo najde, stačí u něj v „Čidla v dosahu“ kliknout „Spárovat znovu“ a vyjmout a vložit baterii.'],
                        confirm: 'Odebrat čidlo', danger: true
                    })) return;
                    action(() => api('POST', '/api/nodes/remove', { node_id: n.id, force: true }), 'Čidlo odebráno.').then(done);
                });
            } else {
                btn('Nastavit', 'btn-outline', () => { selectNode(n.id); setSettingsScope('tank'); applyNode(currentNodeCfg()); window.scrollTo({ top: 0, behavior: 'smooth' }); });
                btn('Odpárovat', 'btn-danger', async () => {
                    if (!await confirmDialog({
                        title: `Odpárovat čidlo ${n.name} (${n.id})?`,
                        body: ['Dokončí se po vyjmutí a vložení baterie do čidla. Naměřená data zůstanou.'],
                        confirm: 'Odpárovat', danger: true
                    })) return;
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
            tr.innerHTML = `<td><code>${esc(q.id)}</code></td><td>${sigTxt(q.rssi)}</td><td>${fmtAgo(q.age_s)}</td><td>${esc(state)}</td><td class="td-actions"></td>`;
            if (!q.target && !foreign) {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm';
                b.textContent = q.known ? 'Znovu spárovat' : (q.node_paired ? 'Spárovat znovu' : 'Spárovat');
                // the table lives in the Add-sensor dialog: no nested confirmation, the button says it
                b.title = !q.known && q.node_paired ? 'Hub ho uvolní uschovaným klíčem a dá mu nový.'
                    : q.known ? 'Starý klíč se zruší a vygeneruje se nový.' : 'Doplní ID; zadejte název a potvrďte.';
                b.addEventListener('click', () => {
                    if (!q.known && !q.node_paired) {        // nové čidlo: název + ověření ID podle štítku
                        if (pairFill) pairFill(q.id); else pairDialog(q.id);
                        return;
                    }
                    startPairing(q.id, '');
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
            tr.innerHTML = `<td><code>${esc(h.id)}</code>${h.added ? ico('check', 'ico-sm') : ''}</td><td>${sigTxt(h.rssi)}</td>
                <td>${fmtAgo(h.age_s)}</td><td>${esc(h.count)}</td><td class="muted small">${h.kind === 'pair' ? 'nabízí párování' : (h.added ? 'data' : 'cizí čidlo')}</td><td class="td-actions"></td>`;
            const cell = tr.lastElementChild;
            if (foreign && targetIds.has(h.id)) {
                cell.innerHTML = '<span class="muted small">Vyjměte a vložte do čidla baterii.</span>';
            } else if (foreign && h.archived) {
                // removed here (or its first data were lost): the hub still holds its key
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'btn btn-primary btn-sm'; b.textContent = 'Spárovat znovu';
                b.title = h.key_match ? 'Čidlo vysílá s klíčem uschovaným v hubu' : 'Hub má uschovaný starší klíč tohoto ID';
                b.addEventListener('click', async () => {
                    if (!await confirmDialog({
                        title: `Spárovat znovu čidlo ${h.id}${h.archived_name ? ' (' + h.archived_name + ')' : ''}?`,
                        body: ['Pak do čidla vyjměte a vložte baterii – hub ho uvolní uschovaným klíčem a spáruje s novým.'],
                        confirm: 'Spárovat znovu'
                    })) return;
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
        const hint = $('pairHint');
        hint.dataset.idle = hint.dataset.idle || hint.textContent;
        hint.textContent = offering ? 'Nové čidlo nabízí párování – otevřete „Přidat čidlo“.'
            : waiting ? `Čekám na čidlo ${targets.map((t) => t.id).join(', ')}.` : hint.dataset.idle;
        hint.classList.toggle('muted', !offering);
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
            <dt>Vytížení procesoru</dt><dd>${d.cpu_pct != null ? num(d.cpu_pct, 1, ' %') : '–'}</dd>
            <dt>Operační paměť (RAM)</dt><dd>${d.ram_total_mb ? `${num(d.ram_used_mb, 0, ' MB')} / ${num(d.ram_total_mb, 0, ' MB')} (${num(d.ram_pct, 0, ' %')})` : (d.rss_kb ? num(d.rss_kb / 1024, 1, ' MB') : '–')}</dd>
            <dt>Paměť procesu</dt><dd>${d.rss_kb ? num(d.rss_kb / 1024, 1, ' MB') : '–'}</dd>
            <dt>Databáze / volné místo</dt><dd>${num(d.db_mb, 1, ' MB')} / ${num(d.disk_free_mb, 0, ' MB')}</dd>`;
        const dw = $('sysDiskWarn');
        dw.classList.toggle('hidden', !d.disk_low);
        if (d.disk_low) {
            dw.textContent = `Málo místa na kartě: volných ${num(d.disk_free_mb, 0, ' MB')} (hranice ${num(d.low_disk_mb || 300, 0, ' MB')}). `
                + 'Starší podrobná měření se postupně mažou, hodinové průměry zůstávají. Stáhněte si zálohu nebo smažte stará data.';
        }
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
        SERVICE_POWEROFF: ['power', 'Vypnutí zařízení', 'warn'], DATA_DELETED: ['archive', 'Smazání naměřených dat', 'warn'],
        TELEGRAM_LINKED: ['antenna', 'Telegram propojen', 'ok'], TELEGRAM_UNLINKED: ['antenna', 'Telegram odpojen', ''],
        SERVICE_ACCESS: ['lock', 'Vzdálený servis', 'warn'], SERVICE_RESTART: ['restart', 'Restart aplikace', ''],
        SERVICE_REBOOT: ['restart', 'Restart zařízení', 'warn'],
        UPDATE_AVAILABLE: ['arrow-up', 'Dostupná aktualizace', 'info'], UPDATE_INSTALLED: ['check', 'Aktualizace nainstalována', 'ok'],
        UPDATE_ROLLED_BACK: ['restart', 'Aktualizace vrácena', 'bad'], UPDATE_ERROR: ['alert', 'Chyba aktualizace', 'bad'],
        UPDATE_UP_TO_DATE: ['check', 'Bez aktualizace', ''], FACTORY_RESET: ['restart', 'Tovární nastavení', 'warn'],
        TLS_CERT_ISSUED: ['lock', 'Certifikát HTTPS', ''],          // jen staré záznamy (1.7–1.9)
        NODE_PAIRING_CANCELLED: ['antenna', 'Párování zrušeno', '']
    };
    const LOG = { page: 1, cat: '', items: 0 };
    const DAYS = ['neděle', 'pondělí', 'úterý', 'středa', 'čtvrtek', 'pátek', 'sobota'];
    const DAYS_EN = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

    function dayTitle(ms) {
        const d = new Date(ms), today = new Date();
        const y = new Date(Date.now() - 86400e3);
        const same = (a, b) => a.toDateString() === b.toDateString();
        const isEn = window.I18N && window.I18N.getLang() === 'en';
        if (same(d, today)) return isEn ? 'Today' : 'Dnes';
        if (same(d, y)) return isEn ? 'Yesterday' : 'Včera';
        const dayName = isEn ? DAYS_EN[d.getDay()] : DAYS[d.getDay()];
        return `${dayName} ${d.getDate()}. ${d.getMonth() + 1}. ${d.getFullYear()}`;
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
                const actionTitle = (window.I18N && window.I18N.getLang() === 'en' && window.I18N.ACTIONS_EN[l.action]) || text;
                const sys = !l.username || l.username.startsWith('system');
                const row = document.createElement('div');
                row.className = 'log-row ' + (sev ? 'sev-' + sev : '');
                const t = new Date(ms);
                row.innerHTML = `<span class="log-time">${pad(t.getHours())}:${pad(t.getMinutes())}</span>
                    <span class="log-what"><span class="log-icon">${ico(icon)}</span><span class="log-title">${esc(actionTitle)}</span></span>
                    <span class="log-detail">${esc(l.detail || '')}</span>
                    <span class="log-who"><span class="log-user ${sys ? 'sys' : ''}">${sys ? 'automatika' : esc(l.username)}</span>
                    ${l.ip && !sys ? `<span class="log-ip">${esc(l.ip)}</span>` : ''}</span>`;
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
            let curRole = u.role;
            sel.addEventListener('change', async () => {
                const to = sel.value;
                const ok = await confirmDialog({
                    title: 'Změnit roli uživatele?',
                    body: [`Uživatel ${u.username} bude mít roli „${to === 'admin' ? 'Administrátor' : 'Uživatel (jen čtení)'}“.`,
                        to === 'admin' ? 'Administrátor může měnit nastavení, ovládat dopouštění a spravovat uživatele.'
                            : 'Uživatel uvidí data, ale nastavení ani dopouštění měnit nemůže.'],
                    confirm: 'Změnit roli'
                });
                if (!ok) { sel.value = curRole; return; }
                const r = await action(() => api('POST', '/api/users/role', { username: u.username, role: to }), 'Role změněna.');
                if (r) curRole = to; else { sel.value = curRole; loadUsers(); }
            });
            if (!self) {
                const cell = tr.querySelector('.td-actions');
                const pw = document.createElement('button');
                pw.type = 'button'; pw.className = 'btn btn-outline btn-sm'; pw.textContent = 'Nové heslo';
                pw.addEventListener('click', async () => {
                    const r = await formDialog({
                        title: `Nové heslo pro ${u.username}`,
                        body: ['Uživatel bude po změně hesla odhlášen ze všech zařízení.'],
                        fields: [{ name: 'password', type: 'password', label: 'Nové heslo (min. 10 znaků)', autocomplete: 'new-password' },
                            { name: 'password2', type: 'password', label: 'Potvrzení hesla', autocomplete: 'new-password' }],
                        validate: (v) => (v.password.length < 10 ? { field: 'password', message: 'Heslo musí mít alespoň 10 znaků.' }
                            : v.password !== v.password2 ? { field: 'password2', message: 'Hesla se neshodují.' } : null),
                        submit: async (v) => {
                            try { await api('POST', '/api/users/password', { username: u.username, password: v.password }); } catch (e) { toast(e.message, true); return e.message; }
                            return null;
                        },
                        confirm: 'Nastavit heslo'
                    });
                    if (r) toast('Heslo nastaveno, uživatel byl odhlášen.');
                });
                const del = document.createElement('button');
                del.type = 'button'; del.className = 'btn btn-danger btn-sm'; del.textContent = 'Smazat';
                del.addEventListener('click', async () => {
                    const ok = await confirmDialog({
                        title: 'Smazat uživatele?',
                        body: [`Uživatel ${u.username} bude smazán a odhlášen ze všech zařízení. Nejde to vzít zpět.`],
                        confirm: 'Smazat uživatele', danger: true
                    });
                    if (!ok) return;
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
        applyLang(localStorage.getItem('lang') || 'cs', false);
        try {
            Object.assign(ME, await api('GET', '/api/me'));
            try { CURRENT_NODE = sessionStorage.getItem('node'); VIEW_ALL = sessionStorage.getItem('viewAll') === '1'; } catch (e) { /* ignore */ }
        } catch (e) { return; }
        applyTheme(ME.theme, false);
        applyLang(ME.lang || localStorage.getItem('lang') || 'cs', true);
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
        bindThemeSwitch();
        bindLangSwitch();
        bindHelpTips();
        $('diskBanner').addEventListener('click', (e) => { if (e.target.closest('.disk-jump')) { e.preventDefault(); jumpToBackup(); } });
        if (ME.role === 'admin') bindPowerMenu();
        // přepnutí světlý/tmavý motiv → grafy znovu s novými barvami
        if (window.matchMedia) {
            const mq = window.matchMedia('(prefers-color-scheme: dark)');
            const onScheme = () => { if (ME.theme && ME.theme !== 'auto') return;   // vynucený motiv
                readTheme(); if ($('dashboard').classList.contains('active')) loadAllCharts(); };
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
        loadTelegramMine();

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
