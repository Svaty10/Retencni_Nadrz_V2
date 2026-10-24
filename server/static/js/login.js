/* Přihlášení a vytvoření vlastního účtu – bez inline skriptů (CSP). */
(function () {
    'use strict';
    const err = document.getElementById('err');
    const btn = document.getElementById('btn');

    function showError(msg) {
        err.textContent = msg;
        err.classList.remove('hidden');
    }

    async function post(url, body) {
        const r = await fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        let data = {};
        try { data = await r.json(); } catch (e) { /* ignore */ }
        if (!r.ok || data.ok === false) throw new Error(data.error || ('Chyba ' + r.status));
        return data;
    }

    async function guard(fn) {
        btn.disabled = true;
        err.classList.add('hidden');
        try {
            const res = await fn();
            window.location.replace(res && res.setup ? '/setup' : '/');
        } catch (e) {
            showError(e.message || 'Chyba spojení se serverem.');
        } finally {
            btn.disabled = false;
        }
    }

    const login = document.getElementById('loginForm');
    if (login) {
        fetch('/api/auth/mode', { credentials: 'same-origin' }).then((r) => r.json()).then((m) => {
            // odkaz na certifikát hubu, jen když už existuje
            const certHint = document.getElementById('certHint');
            if (certHint && m.ca) certHint.classList.remove('hidden');
            if (m.first_login) {
                document.getElementById('firstInfo').classList.remove('hidden');
                const u = document.getElementById('username');
                if (!u.value) u.value = 'admin';
                document.getElementById('password').focus();
            }
        }).catch(() => { /* offline */ });
        login.addEventListener('submit', function (e) {
            e.preventDefault();
            guard(() => post('/api/auth/login', {
                username: document.getElementById('username').value.trim(),
                password: document.getElementById('password').value
            }));
        });
    }

    const setup = document.getElementById('setupForm');
    if (setup) {
        setup.addEventListener('submit', function (e) {
            e.preventDefault();
            const p1 = document.getElementById('password').value;
            if (p1 !== document.getElementById('password2').value) {
                showError('Hesla se neshodují.');
                return;
            }
            guard(() => post('/api/auth/setup', {
                username: document.getElementById('username').value.trim(),
                password: p1
            }));
        });
    }
})();
