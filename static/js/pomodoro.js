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
  let mode = 'POMODORO_25_5', plannedMin = 25, remSec = 1500, elapsedSec = 0;
  let running = false, anchorMs = 0, anchorRem = 1500, sessionId = null, tickId = null;

  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  const render = () => { if (display) display.textContent = fmt(remSec); };
  const setStatus = (t) => { if (statusEl) statusEl.textContent = t; };
  const setMins = (m) => { plannedMin = Math.max(1, Math.min(180, Number(m) || 25)); remSec = plannedMin * 60; elapsedSec = 0; render(); };
  const syncUI = () => {
    if (btnStart) btnStart.disabled = running; if (btnPause) btnPause.disabled = !running;
    if (btnDone) btnDone.disabled = !sessionId && !running; if (btnStop) btnStop.disabled = !sessionId && !running;
  };
  const postJson = (url, body) => fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' }, body: JSON.stringify(body),
  }).then((r) => (r.ok ? r.json() : null)).catch(() => null);

  const chime = () => {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator(), g = ctx.createGain();
      osc.frequency.value = 880; g.gain.setValueAtTime(0.15, ctx.currentTime);
      osc.connect(g); g.connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + 0.35);
    } catch (_) {}
  };
  const selectMode = (btn) => {
    if (running || sessionId) return;
    mode = btn.dataset.mode || 'POMODORO_25_5';
    const custom = mode === 'CUSTOM';
    customWrap?.classList.toggle('hidden', !custom); customWrap?.classList.toggle('flex', custom);
    setMins(custom ? customIn?.value : btn.dataset.minutes);
    modeBtns.forEach((b) => {
      const on = b === btn;
      b.classList.toggle('border-indigo-600', on); b.classList.toggle('bg-indigo-50', on); b.classList.toggle('text-indigo-700', on);
      b.classList.toggle('border-slate-200', !on); b.classList.toggle('bg-white', !on); b.classList.toggle('text-slate-700', !on);
    });
  };
  const start = async () => {
    if (running) return;
    if (!sessionId) {
      const res = await postJson(startUrl || '/tasks/focus/start/', {
        task_id: Number(taskId), checkpoint_id: cpSelect?.value ? Number(cpSelect.value) : null, mode, planned_minutes: plannedMin,
      });
      sessionId = res?.session_id ?? res?.id ?? null;
    }
    running = true; anchorMs = Date.now(); anchorRem = remSec; setStatus('Focusing...'); syncUI();
    tickId = setInterval(() => {
      remSec = Math.max(0, anchorRem - Math.floor((Date.now() - anchorMs) / 1000));
      render(); document.title = `${fmt(remSec)} · Focus`;
      if (remSec === 0) { chime(); finish('COMPLETED'); }
    }, 250);
  };
  const pause = () => {
    if (!running) return;
    clearInterval(tickId); running = false;
    const d = Math.floor((Date.now() - anchorMs) / 1000);
    elapsedSec += d; remSec = Math.max(0, anchorRem - d); render(); setStatus('Paused'); syncUI();
  };
  const finish = async (status) => {
    if (running) { clearInterval(tickId); running = false; elapsedSec += Math.floor((Date.now() - anchorMs) / 1000); }
    const elapsed_seconds = Math.max(elapsedSec, plannedMin * 60 - remSec);
    if (sessionId) {
      const url = (finishUrlTemplate || '/tasks/focus/__ID__/finish/').replace(/__ID__|SESSION_ID|\{session_id\}/, String(sessionId));
      const res = await postJson(url, { status, elapsed_seconds, mark_checkpoint_done: Boolean(markDone?.checked && status === 'COMPLETED') });
      if (todayEl && res?.today_focus_minutes != null) todayEl.textContent = res.today_focus_minutes;
    }
    sessionId = null; setMins(plannedMin); setStatus(status === 'COMPLETED' ? 'Session completed!' : 'Session abandoned.'); syncUI();
  };

  modeBtns.forEach((b) => b.addEventListener('click', () => selectMode(b)));
  customIn?.addEventListener('input', () => { if (!running && !sessionId && mode === 'CUSTOM') setMins(customIn.value); });
  btnStart?.addEventListener('click', start); btnPause?.addEventListener('click', pause);
  btnDone?.addEventListener('click', () => finish('COMPLETED')); btnStop?.addEventListener('click', () => finish('ABANDONED'));
  render(); syncUI();
})();
