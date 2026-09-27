'use strict';
document.getElementById('loginForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = document.getElementById('go'), err = document.getElementById('err');
  btn.disabled = true; err.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: document.getElementById('login').value, password: document.getElementById('password').value }),
    });
    if (r.ok) { location.href = '/'; return; }
    err.textContent = (await r.json()).error || 'Ошибка входа';
  } catch (e) { err.textContent = 'Сервер недоступен'; }
  btn.disabled = false;
});
