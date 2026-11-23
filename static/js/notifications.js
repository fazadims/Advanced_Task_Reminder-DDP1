(() => {
  const btn = document.getElementById('enable-desktop-alerts');
  const dot = document.getElementById('desktop-alert-dot');
  const box = document.getElementById('alert-toast-container');
  const pollUrl = box?.dataset.pollUrl || '/alerts/poll/';
  const SEEN_KEY = 'atr_seen_notifications';
  const TONES = {
    3: 'bg-rose-50 border-rose-300 text-rose-900',
    2: 'bg-amber-50 border-amber-300 text-amber-900',
    1: 'bg-indigo-50 border-indigo-200 text-slate-900',
  };
  const BADGES = {
    3: 'bg-rose-600 text-white',
    2: 'bg-amber-700 text-white',
    1: 'bg-indigo-600 text-white',
  };

  const getInitialAlertCount = () => {
    const initialBadge = document.querySelector('#nav-alert-badge, [data-nav-alert-badge]');
    if (!initialBadge || initialBadge.classList.contains('hidden')) return 0;
    return parseInt(initialBadge.textContent || '0', 10) || 0;
  };

  let currentAlertCount = getInitialAlertCount();

  const notifyUx = (message, tone = 'info') => {
    if (typeof window.atrToast === 'function') {
      window.atrToast(message, tone);
    }
  };

  const updateNavBadge = (count) => {
    const safeCount = Math.max(0, Number.isFinite(count) ? count : 0);
    currentAlertCount = safeCount;
    document.querySelectorAll('#nav-alert-badge, [data-nav-alert-badge]').forEach((badge) => {
      if (safeCount > 0) {
        badge.textContent = safeCount > 99 ? '99+' : String(safeCount);
        badge.classList.remove('hidden');
      } else {
        badge.textContent = '0';
        badge.classList.add('hidden');
      }
    });
  };

  const loadSeen = () => {
    try {
      const raw = window.sessionStorage.getItem(SEEN_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(parsed) ? parsed : []);
    } catch (_) {
      return new Set();
    }
  };
  const seen = loadSeen();
  const markSeen = (key) => {
    seen.add(key);
    try {
      window.sessionStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-200)));
    } catch (_) {}
  };

  const csrf = () => {
    const cookieMatch = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (cookieMatch && cookieMatch[1]) return decodeURIComponent(cookieMatch[1]);
    try {
      return JSON.parse(document.body.getAttribute('hx-headers') || '{}')['X-CSRFToken'] || '';
    } catch (_) {
      return '';
    }
  };

  const desktopSupported = ('Notification' in window) && Boolean(window.isSecureContext);
  const setDotColor = (colorClass) => {
    if (!dot) return;
    dot.classList.remove('bg-slate-300', 'bg-emerald-500', 'bg-rose-500');
    dot.classList.add(colorClass);
  };

  const syncBtn = () => {
    if (!btn) return;
    if (!desktopSupported) {
      btn.disabled = true;
      btn.classList.add('hidden');
      return;
    }
    const labelEl = btn.querySelector('[data-label]');
    if (Notification.permission === 'granted') {
      if (labelEl) labelEl.textContent = 'Aktif';
      setDotColor('bg-emerald-500');
      btn.title = 'Notifikasi desktop aktif';
    } else if (Notification.permission === 'denied') {
      if (labelEl) labelEl.textContent = 'Diblokir';
      setDotColor('bg-rose-500');
      btn.title = 'Notifikasi desktop diblokir oleh browser';
    } else {
      if (labelEl) labelEl.textContent = 'Notifikasi';
      setDotColor('bg-slate-300');
      btn.title = 'Aktifkan notifikasi desktop';
    }
  };

  btn?.addEventListener('click', () => {
    if (!desktopSupported) return;
    if (Notification.permission === 'granted') {
      notifyUx('Notifikasi desktop sudah aktif.', 'info');
      return;
    }
    if (Notification.permission === 'denied') {
      notifyUx('Notifikasi desktop diblokir. Izinkan lewat pengaturan browser.', 'warning');
      return;
    }
    try {
      const handlePermission = (permission) => {
        syncBtn();
        const status = permission || Notification.permission;
        if (status === 'granted') {
          notifyUx('Notifikasi desktop berhasil diaktifkan!', 'success');
        } else if (status === 'denied') {
          notifyUx('Izin notifikasi desktop ditolak.', 'warning');
        }
      };
      const req = Notification.requestPermission();
      if (req && typeof req.then === 'function') {
        req.then(handlePermission).catch(() => {});
      } else {
        handlePermission(req);
      }
    } catch (_) {}
  });
  syncBtn();

  const showCardError = (card, message) => {
    let errEl = card.querySelector('.toast-error');
    if (!errEl) {
      errEl = Object.assign(document.createElement('p'), {
        className: 'toast-error text-rose-700 font-semibold pt-1',
      });
      card.appendChild(errEl);
    }
    errEl.textContent = message;
  };

  const act = async (url, body, card, feedbackMsg, feedbackTone = 'info') => {
    const btns = [...card.querySelectorAll('button')];
    btns.forEach((b) => { b.disabled = true; });
    const existingErr = card.querySelector('.toast-error');
    if (existingErr) existingErr.remove();
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRFToken': csrf(),
          Accept: 'application/json',
        },
        body,
      });
      if (res.ok) {
        card.remove();
        updateNavBadge(Math.max(0, currentAlertCount - 1));
        if (feedbackMsg) notifyUx(feedbackMsg, feedbackTone);
        poll(true);
        if (document.getElementById('inbox-sections') && window.htmx) {
          window.htmx.ajax('GET', window.location.pathname, {
            target: '#inbox-sections',
            select: '#inbox-sections',
            swap: 'outerHTML',
          });
        }
        return;
      }
      let errMsg = 'Aksi gagal. Silakan coba lagi.';
      try {
        const ct = res.headers.get('content-type') || '';
        if (ct.includes('application/json')) {
          const payload = await res.json();
          if (payload && payload.error) errMsg = String(payload.error);
        }
      } catch (_) {}
      showCardError(card, errMsg);
    } catch (_) {
      showCardError(card, 'Gangguan jaringan. Silakan coba lagi.');
    } finally {
      if (card.isConnected) {
        btns.forEach((b) => { b.disabled = false; });
      }
    }
  };

  const mkBtn = (text, fn, extraClass = '') => {
    const b = Object.assign(document.createElement('button'), {
      type: 'button',
      textContent: text,
      className: extraClass || 'px-2 py-1 rounded-lg bg-white/90 hover:bg-white border border-slate-300 text-xs font-medium text-slate-700 disabled:opacity-50',
    });
    b.addEventListener('click', fn);
    return b;
  };

  const renderToast = (item, signature) => {
    const level = item.escalation_level || 1;
    const card = Object.assign(document.createElement('div'), {
      className: `atr-toast-enter rounded-xl border shadow-lg p-3 text-xs space-y-1.5 ${TONES[level] || TONES[1]}`,
    });
    card.dataset.reminderId = String(item.id);
    card.dataset.signature = signature;

    const header = Object.assign(document.createElement('div'), {
      className: 'flex items-center justify-between gap-2',
    });
    const badge = Object.assign(document.createElement('span'), {
      className: `inline-block px-1.5 py-0.5 rounded font-bold text-[10px] uppercase ${BADGES[level] || BADGES[1]}`,
      textContent: `L${level}`,
    });
    header.appendChild(badge);
    if (item.task_url) {
      const openLink = Object.assign(document.createElement('a'), {
        href: item.task_url,
        textContent: 'Buka →',
        className: 'text-indigo-700 hover:underline font-semibold shrink-0',
      });
      header.appendChild(openLink);
    }

    const title = Object.assign(document.createElement('p'), {
      className: 'font-semibold',
      textContent: item.title || item.task_title || 'Pengingat Tugas',
    });

    card.append(header, title);

    if (item.task_title && item.task_title !== item.title) {
      const subtitle = Object.assign(document.createElement('p'), {
        className: 'text-[11px] opacity-80 font-medium',
        textContent: item.task_title,
      });
      card.appendChild(subtitle);
    }

    if (item.message) {
      const msg = Object.assign(document.createElement('p'), { textContent: item.message });
      card.appendChild(msg);
    }

    const row = Object.assign(document.createElement('div'), {
      className: 'flex flex-wrap items-center gap-1.5 pt-1',
    });
    const snooze = item.snooze_url || `/alerts/${item.id}/snooze/`;
    const dismiss = item.dismiss_url || `/alerts/${item.id}/dismiss/`;
    const done = item.done_url || `/alerts/${item.id}/done/`;

    if ((item.snooze_count || 0) >= 3) {
      const limitPill = Object.assign(document.createElement('span'), {
        textContent: 'Batas tunda 3/3',
        className: 'inline-flex items-center px-2 py-1 rounded-lg bg-rose-100 border border-rose-200 text-rose-700 text-[11px] font-semibold',
      });
      row.append(limitPill);
    } else {
      row.append(
        mkBtn('Tunda 15m', () => act(snooze, 'minutes=15', card, 'Pengingat ditunda 15 menit', 'info')),
        mkBtn('Tunda 1j', () => act(snooze, 'minutes=60', card, 'Pengingat ditunda 1 jam', 'info')),
      );
    }

    row.append(
      mkBtn(
        'Selesai',
        () => act(done, '', card, 'Ditandai selesai!', 'success'),
        'px-2 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white border border-emerald-700 text-xs font-semibold disabled:opacity-50',
      ),
      mkBtn('Tutup', () => act(dismiss, '', card, 'Pengingat ditutup', 'info')),
    );
    card.appendChild(row);
    return card;
  };

  let inFlight = false;
  let queuedPoll = false;
  let pollTimer = null;

  async function poll(force = false) {
    if (!box || (document.hidden && !force)) return;
    if (inFlight) {
      if (force) queuedPoll = true;
      return;
    }
    inFlight = true;
    try {
      const res = await fetch(pollUrl, { headers: { Accept: 'application/json' } });
      if ((res.redirected && res.url && res.url.includes('/login/')) || res.status === 401 || res.status === 403) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        return;
      }
      if (!res.ok || !(res.headers.get('content-type') || '').includes('application/json')) return;
      const data = await res.json();
      const items = Array.isArray(data) ? data : (data.alerts || data.items || data.reminders || []);

      updateNavBadge(items.length);

      const activeIds = new Set(items.map((item) => String(item.id)));
      [...box.querySelectorAll('[data-reminder-id]')].forEach((el) => {
        if (!activeIds.has(el.dataset.reminderId)) el.remove();
      });

      items.forEach((item) => {
        const idStr = String(item.id);
        const safeId = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
          ? CSS.escape(idStr)
          : idStr.replace(/[^a-zA-Z0-9_-]/g, '');
        const sig = `${item.escalation_level || 1}:${item.snooze_count || 0}:${item.title || ''}:${item.message || ''}`;
        const existing = box.querySelector(`[data-reminder-id="${safeId}"]`);
        if (!existing) {
          box.appendChild(renderToast(item, sig));
        } else if (existing.dataset.signature !== sig) {
          existing.replaceWith(renderToast(item, sig));
        }

        const seenKey = `${item.id}:${item.snooze_count || 0}:${item.escalation_level || 1}`;
        if (desktopSupported && Notification.permission === 'granted' && !seen.has(seenKey)) {
          markSeen(seenKey);
          try {
            const n = new Notification(item.title || item.task_title || 'Pengingat ATR', {
              body: item.message || '',
              tag: `atr-${item.id}`,
            });
            n.onclick = () => {
              window.focus();
              if (item.task_url) window.location.href = item.task_url;
              n.close();
            };
          } catch (_) {}
        }
      });
    } catch (_) {
    } finally {
      inFlight = false;
      if (queuedPoll) {
        queuedPoll = false;
        poll(true);
      }
    }
  }

  document.body.addEventListener('remindersChanged', () => {
    poll(true);
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && pollTimer) poll();
  });
  pollTimer = setInterval(poll, 60000);
  poll();
})();
