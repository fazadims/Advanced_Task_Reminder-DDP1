(() => {
  const btn = document.getElementById('enable-desktop-alerts');
  const box = document.getElementById('alert-toast-container');
  const pollUrl = box?.dataset.pollUrl || '/alerts/poll/';
  const seen = new Set();
  const TONES = { 3: 'bg-red-50 border-red-300 text-red-900', 2: 'bg-amber-50 border-amber-300 text-amber-900', 1: 'bg-blue-50 border-blue-300 text-blue-900' };
  const csrf = () => {
    try { return JSON.parse(document.body.getAttribute('hx-headers') || '{}')['X-CSRFToken'] || ''; }
    catch (_) { return (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ''; }
  };
  const syncBtn = () => {
    if (!btn || !('Notification' in window)) return;
    if (Notification.permission === 'granted') btn.textContent = 'Desktop Alerts On';
    else if (Notification.permission === 'denied') btn.textContent = 'Alerts Blocked';
  };
  btn?.addEventListener('click', () => ('Notification' in window) && Notification.requestPermission().then(syncBtn));
  syncBtn();

  const act = (url, body, card) => fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf() }, body,
  }).finally(() => card.remove());
  const mkBtn = (text, fn) => {
    const b = Object.assign(document.createElement('button'), {
      type: 'button', textContent: text,
      className: 'px-2 py-1 rounded bg-white/80 hover:bg-white border border-current/20 text-xs font-medium',
    });
    b.addEventListener('click', fn); return b;
  };
  const renderToast = (item) => {
    const card = Object.assign(document.createElement('div'), {
      className: `rounded-xl border shadow-lg p-3 text-xs space-y-1.5 ${TONES[item.escalation_level] || TONES[1]}`,
    });
    const title = Object.assign(document.createElement('p'), { className: 'font-semibold', textContent: item.title || item.task_title || 'Task Reminder' });
    const msg = Object.assign(document.createElement('p'), { textContent: item.message || '' });
    const row = Object.assign(document.createElement('div'), { className: 'flex items-center gap-1.5 pt-1' });
    const snooze = item.snooze_url || `/alerts/${item.id}/snooze/`;
    const dismiss = item.dismiss_url || `/alerts/${item.id}/dismiss/`;
    row.append(mkBtn('Snooze 15m', () => act(snooze, 'minutes=15', card)), mkBtn('Snooze 1h', () => act(snooze, 'minutes=60', card)), mkBtn('Dismiss', () => act(dismiss, '', card)));
    card.append(title, msg, row);
    return card;
  };
  async function poll() {
    if (!box) return;
    try {
      const res = await fetch(pollUrl, { headers: { Accept: 'application/json' } });
      if (!res.ok || !(res.headers.get('content-type') || '').includes('application/json')) return;
      const data = await res.json();
      const items = Array.isArray(data) ? data : (data.alerts || data.items || data.reminders || []);
      box.innerHTML = '';
      items.forEach((item) => {
        box.appendChild(renderToast(item));
        if ('Notification' in window && Notification.permission === 'granted' && !seen.has(item.id)) {
          seen.add(item.id);
          new Notification(item.title || item.task_title || 'ATR Reminder', { body: item.message || '', tag: 'atr-' + item.id });
        }
      });
    } catch (_) {}
  }
  poll(); setInterval(poll, 60000);
})();
