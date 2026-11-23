(() => {
  const app = document.getElementById('pomodoro-app');
  if (!app) return;
  const { taskId, startUrl, finishUrlTemplate, csrf } = app.dataset;
  const $ = (id) => document.getElementById(id);
  const display = $('timer-display'), statusEl = $('timer-status'), todayEl = $('today-focus-minutes');
  const cpSelect = $('checkpoint-select'), markDone = $('mark-checkpoint-done');
  const customWrap = $('custom-minutes-wrapper'), customIn = $('custom-minutes');
  const btnStart = $('btn-start'), btnPause = $('btn-pause'), btnDone = $('btn-complete'), btnStop = $('btn-abandon');
  const modeBtns = [...app.querySelectorAll('.mode-btn')];
  const initialTitle = document.title;

  let mode = 'POMODORO_25_5', plannedMin = 25, remSec = 1500, elapsedSec = 0;
  let running = false, busy = false, anchorMs = 0, anchorRem = 1500, sessionId = null, tickId = null;

  const getCsrf = () => {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return (match && match[1]) ? decodeURIComponent(match[1]) : (csrf || '');
  };
  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  const render = () => { if (display) display.textContent = fmt(remSec); };
  const setStatus = (t) => { if (statusEl) statusEl.textContent = t; };
  const setMins = (m) => {
    const parsed = Number(m);
    plannedMin = Number.isFinite(parsed) && parsed > 0 ? Math.max(1, Math.min(180, Math.round(parsed))) : 25;
    remSec = plannedMin * 60;
    elapsedSec = 0;
    render();
  };
  const syncUI = () => {
    const locked = Boolean(running || sessionId || busy);
    if (btnStart) {
      btnStart.disabled = running || busy;
      btnStart.textContent = (!running && sessionId) ? 'Resume' : 'Start';
    }
    if (btnPause) btnPause.disabled = !running || busy;
    if (btnDone) btnDone.disabled = (!sessionId && !running) || busy;
    if (btnStop) btnStop.disabled = (!sessionId && !running) || busy;
    if (cpSelect) cpSelect.disabled = locked;
    if (customIn) customIn.disabled = locked;
    modeBtns.forEach((b) => {
      b.disabled = locked;
      b.setAttribute('aria-pressed', String((b.dataset.mode || '') === mode));
    });
  };
  const postJson = (url, body) => fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
    body: JSON.stringify(body),
  }).then((r) => (r.ok ? r.json() : null)).catch(() => null);

  const chime = () => {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator(), g = ctx.createGain();
      osc.frequency.value = 880;
      g.gain.setValueAtTime(0.15, ctx.currentTime);
      osc.connect(g);
      g.connect(ctx.destination);
      osc.onended = () => ctx.close().catch(() => {});
      osc.start();
      osc.stop(ctx.currentTime + 0.35);
    } catch (_) {}
  };
  const selectMode = (btn) => {
    if (running || sessionId || busy) return;
    mode = btn.dataset.mode || 'POMODORO_25_5';
    const custom = mode === 'CUSTOM';
    customWrap?.classList.toggle('hidden', !custom);
    customWrap?.classList.toggle('flex', custom);
    setMins(custom ? customIn?.value : btn.dataset.minutes);
    modeBtns.forEach((b) => {
      const on = b === btn;
      b.classList.toggle('border-indigo-600', on);
      b.classList.toggle('bg-indigo-50', on);
      b.classList.toggle('text-indigo-700', on);
      b.classList.toggle('border-slate-200', !on);
      b.classList.toggle('bg-white', !on);
      b.classList.toggle('text-slate-700', !on);
      b.setAttribute('aria-pressed', String(on));
    });
  };
  const start = async () => {
    if (running || busy) return;
    if (!sessionId) {
      busy = true;
      setStatus('Starting session...');
      syncUI();
      const res = await postJson(startUrl || '/tasks/focus/start/', {
        task_id: Number(taskId),
        checkpoint_id: cpSelect?.value ? Number(cpSelect.value) : null,
        mode,
        planned_minutes: plannedMin,
      });
      sessionId = res?.session_id ?? res?.id ?? null;
      if (!sessionId) {
        busy = false;
        setStatus('Failed to start focus session. Please try again.');
        syncUI();
        return;
      }
      busy = false;
    }
    if (tickId) { clearInterval(tickId); tickId = null; }
    running = true;
    anchorMs = Date.now();
    anchorRem = remSec;
    setStatus('Focusing...');
    document.title = `${fmt(remSec)} · Focus`;
    syncUI();
    tickId = setInterval(() => {
      remSec = Math.max(0, anchorRem - Math.floor((Date.now() - anchorMs) / 1000));
      render();
      document.title = `${fmt(remSec)} · Focus`;
      if (remSec === 0) {
        chime();
        finish('COMPLETED');
      }
    }, 250);
  };
  const pause = () => {
    if (!running || busy) return;
    if (tickId) { clearInterval(tickId); tickId = null; }
    running = false;
    const d = Math.floor((Date.now() - anchorMs) / 1000);
    elapsedSec += d;
    remSec = Math.max(0, anchorRem - d);
    render();
    document.title = initialTitle;
    setStatus('Paused');
    syncUI();
  };
  const finish = async (status) => {
    if (busy || (!sessionId && !running)) return;
    busy = true;
    if (running) {
      if (tickId) { clearInterval(tickId); tickId = null; }
      running = false;
      const d = Math.floor((Date.now() - anchorMs) / 1000);
      elapsedSec += d;
      remSec = Math.max(0, anchorRem - d);
      render();
    }
    syncUI();
    const elapsed_seconds = Math.max(elapsedSec, plannedMin * 60 - remSec);
    if (sessionId) {
      setStatus('Saving session...');
      const url = (finishUrlTemplate || '/tasks/focus/0/finish/')
        .replace(/\/0\/finish\/?$/, `/${sessionId}/finish/`)
        .replace(/__ID__|SESSION_ID|\{session_id\}/, String(sessionId));
      const res = await postJson(url, {
        status,
        elapsed_seconds,
        mark_checkpoint_done: Boolean(markDone?.checked && status === 'COMPLETED'),
      });
      if (!res) {
        busy = false;
        document.title = initialTitle;
        setStatus('Failed to save session. Click Complete/Abandon to retry.');
        syncUI();
        return;
      }
      if (todayEl && res.today_focus_minutes != null) todayEl.textContent = res.today_focus_minutes;
      if (res.checkpoint_marked && cpSelect) {
        const opt = cpSelect.options[cpSelect.selectedIndex];
        if (opt && opt.value) {
          const text = opt.textContent.trim();
          if (!text.startsWith('[Done]')) opt.textContent = `[Done] ${text}`;
          opt.disabled = true;
        }
        cpSelect.value = '';
        if (markDone) markDone.checked = false;
      }
    }
    sessionId = null;
    busy = false;
    document.title = initialTitle;
    setMins(plannedMin);
    setStatus(status === 'COMPLETED' ? 'Session completed!' : 'Session abandoned.');
    syncUI();
  };

  window.addEventListener('beforeunload', (e) => {
    if (running || sessionId) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  modeBtns.forEach((b) => b.addEventListener('click', () => selectMode(b)));
  customIn?.addEventListener('input', () => {
    if (!running && !sessionId && !busy && mode === 'CUSTOM') setMins(customIn.value);
  });
  btnStart?.addEventListener('click', start);
  btnPause?.addEventListener('click', pause);
  btnDone?.addEventListener('click', () => finish('COMPLETED'));
  btnStop?.addEventListener('click', () => finish('ABANDONED'));
  render();
  syncUI();
})();
