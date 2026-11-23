(() => {
  const app = document.getElementById('pomodoro-app');
  if (!app) return;

  const { taskId, startUrl, finishUrlTemplate, csrf } = app.dataset;
  const $ = (id) => document.getElementById(id);

  const display = $('timer-display');
  const statusEl = $('timer-status');
  const statusDot = $('timer-status-dot');
  const todayEl = $('today-focus-minutes');
  const phasePill = $('phase-pill');
  const ringEl = $('timer-ring-progress');
  const cpSelect = $('checkpoint-select');
  const markDone = $('mark-checkpoint-done');
  const markCpHint = $('mark-checkpoint-hint');
  const customWrap = $('custom-minutes-wrapper');
  const customIn = $('custom-minutes');
  const focusControls = $('focus-controls');
  const breakControls = $('break-controls');
  const completeBanner = $('session-complete-banner');
  const completeMinutesText = $('complete-minutes-text');
  const completeCpBadge = $('complete-checkpoint-badge');
  const btnStart = $('btn-start');
  const btnPause = $('btn-pause');
  const btnDone = $('btn-complete');
  const btnStop = $('btn-abandon');
  const btnStartBreak = $('btn-start-break');
  const btnNextFocus = $('btn-next-focus');
  const btnBreakPause = $('btn-break-pause');
  const btnSkipBreak = $('btn-skip-break');
  const modeBtns = [...app.querySelectorAll('.mode-btn')];
  const initialTitle = document.title;

  const RING_RADIUS = 96;
  const CIRC = 2 * Math.PI * RING_RADIUS; // ~603.19
  const STROKE_FOCUS = '#4F46E5';
  const STROKE_BREAK = '#10B981';
  const STROKE_PAUSED = '#F59E0B';

  if (ringEl) {
    ringEl.style.strokeDasharray = CIRC.toFixed(2);
  }

  let phase = 'FOCUS'; // 'FOCUS' | 'BREAK'
  let mode = 'POMODORO_25_5';
  let plannedMin = 25;
  let breakMin = 5;
  let totalSec = 1500;
  let remSec = 1500;
  let elapsedSec = 0;
  let running = false;
  let breakPaused = false;
  let busy = false;
  let anchorMs = 0;
  let anchorRem = 1500;
  let sessionId = null;
  let tickId = null;
  let sharedAudioCtx = null;

  const getCsrf = () => {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return (match && match[1]) ? decodeURIComponent(match[1]) : (csrf || '');
  };

  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

  const updateRing = () => {
    if (!ringEl) return;
    const progress = totalSec > 0 ? Math.max(0, Math.min(1, remSec / totalSec)) : 0;
    const offset = CIRC * (1 - progress);
    ringEl.style.strokeDashoffset = offset.toFixed(2);

    const isPaused = (!running && Boolean(sessionId)) || (phase === 'BREAK' && breakPaused);
    if (isPaused) {
      ringEl.style.stroke = STROKE_PAUSED;
    } else if (phase === 'BREAK') {
      ringEl.style.stroke = STROKE_BREAK;
    } else {
      ringEl.style.stroke = STROKE_FOCUS;
    }
  };

  const updatePhaseVisuals = () => {
    const isBreak = phase === 'BREAK';
    const isPaused = (!running && Boolean(sessionId)) || (isBreak && breakPaused);

    if (phasePill) {
      phasePill.textContent = isBreak ? 'Waktu Istirahat' : 'Sesi Fokus';
      phasePill.classList.toggle('bg-indigo-50', !isBreak);
      phasePill.classList.toggle('text-indigo-700', !isBreak);
      phasePill.classList.toggle('border-indigo-200', !isBreak);
      phasePill.classList.toggle('bg-emerald-50', isBreak);
      phasePill.classList.toggle('text-emerald-700', isBreak);
      phasePill.classList.toggle('border-emerald-200', isBreak);
    }

    if (statusDot) {
      statusDot.classList.remove(
        'bg-slate-300',
        'bg-indigo-500',
        'bg-emerald-500',
        'bg-amber-500',
        'animate-pulse'
      );
      if (isPaused) {
        statusDot.classList.add('bg-amber-500');
      } else if (running && isBreak) {
        statusDot.classList.add('bg-emerald-500', 'animate-pulse');
      } else if (running) {
        statusDot.classList.add('bg-indigo-500', 'animate-pulse');
      } else {
        statusDot.classList.add('bg-slate-300');
      }
    }

    updateRing();
  };

  const render = () => {
    if (display) display.textContent = fmt(remSec);
    updateRing();
  };

  const setStatus = (t) => {
    if (statusEl) statusEl.textContent = t;
  };

  const hideCompleteBanner = () => {
    if (completeBanner) completeBanner.classList.add('hidden');
  };

  const showCompleteBanner = (loggedMinutes, checkpointMarked) => {
    if (!completeBanner) return;
    if (completeMinutesText) {
      completeMinutesText.textContent = `+${loggedMinutes} menit dicatat!`;
    }
    if (completeCpBadge) {
      completeCpBadge.classList.toggle('hidden', !checkpointMarked);
    }
    if (btnStartBreak) {
      btnStartBreak.textContent = `Mulai Istirahat ${breakMin} Menit`;
    }
    completeBanner.classList.remove('hidden');
  };

  const setMins = (m) => {
    const raw = m === null || m === undefined ? '' : String(m).trim();
    const parsed = raw === '' ? NaN : Number(raw);
    plannedMin = Number.isFinite(parsed)
      ? Math.max(1, Math.min(180, Math.round(parsed)))
      : 25;
    totalSec = plannedMin * 60;
    remSec = totalSec;
    elapsedSec = 0;
    render();
  };

  const syncCheckpointToggle = () => {
    if (!markDone) return;
    const hasCp = Boolean(cpSelect && cpSelect.value);
    if (!hasCp) {
      markDone.checked = false;
      markDone.disabled = true;
    } else {
      markDone.disabled = Boolean(busy || phase === 'BREAK');
    }
    if (markCpHint) {
      markCpHint.classList.toggle('hidden', hasCp);
    }
  };

  const syncUI = () => {
    const isBreak = phase === 'BREAK';
    const locked = Boolean(running || sessionId || busy || isBreak);
    const awaitingFinish = Boolean(sessionId && remSec === 0);

    if (focusControls) focusControls.classList.toggle('hidden', isBreak);
    if (breakControls) breakControls.classList.toggle('hidden', !isBreak);

    if (btnStart) {
      btnStart.disabled = running || busy || isBreak || awaitingFinish;
      btnStart.textContent = (!running && sessionId) ? 'Lanjutkan' : 'Mulai';
    }
    if (btnPause) btnPause.disabled = !running || busy || isBreak;
    if (btnDone) btnDone.disabled = (!sessionId && !running) || busy || isBreak;
    if (btnStop) btnStop.disabled = (!sessionId && !running) || busy || isBreak;
    if (btnBreakPause) {
      btnBreakPause.disabled = busy || !isBreak;
      btnBreakPause.textContent = breakPaused ? 'Lanjutkan Istirahat' : 'Jeda Istirahat';
    }

    if (cpSelect) cpSelect.disabled = locked;
    if (customIn) customIn.disabled = locked;
    modeBtns.forEach((b) => {
      b.disabled = locked;
      b.setAttribute('aria-pressed', String((b.dataset.mode || '') === mode));
    });

    syncCheckpointToggle();
    updatePhaseVisuals();
  };

  const postJson = async (url, body) => {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
        body: JSON.stringify(body),
      });
      let data = null;
      try {
        data = await r.json();
      } catch (_) {
        data = null;
      }
      return { ok: r.ok, status: r.status, data };
    } catch (_) {
      return { ok: false, status: 0, data: null };
    }
  };

  const unlockAudio = () => {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return null;
      if (!sharedAudioCtx || sharedAudioCtx.state === 'closed') {
        sharedAudioCtx = new AudioCtx();
      }
      if (sharedAudioCtx.state === 'suspended') {
        sharedAudioCtx.resume().catch(() => {});
      }
      return sharedAudioCtx;
    } catch (_) {
      return null;
    }
  };

  const chime = () => {
    try {
      const ctx = unlockAudio();
      if (!ctx) return;

      const playNotes = () => {
        const now = ctx.currentTime;
        const notes = [
          { freq: 523.25, start: now, duration: 0.34, peak: 0.14 },       // C5
          { freq: 659.25, start: now + 0.18, duration: 0.48, peak: 0.15 }, // E5
        ];

        notes.forEach((n) => {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = 'sine';
          osc.frequency.setValueAtTime(n.freq, n.start);

          gain.gain.setValueAtTime(0.0001, n.start);
          gain.gain.linearRampToValueAtTime(n.peak, n.start + 0.035);
          gain.gain.exponentialRampToValueAtTime(0.0001, n.start + n.duration);

          osc.connect(gain);
          gain.connect(ctx.destination);

          osc.start(n.start);
          osc.stop(n.start + n.duration + 0.02);
        });
      };

      if (ctx.state === 'suspended') {
        ctx.resume().then(playNotes).catch(() => {});
      } else {
        playNotes();
      }
    } catch (_) {}
  };

  const selectMode = (btn) => {
    if (running || sessionId || busy || phase === 'BREAK') return;
    hideCompleteBanner();
    mode = btn.dataset.mode || 'POMODORO_25_5';
    const parsedBreak = Number(btn.dataset.breakMinutes);
    breakMin = Number.isFinite(parsedBreak) && parsedBreak > 0 ? Math.round(parsedBreak) : 5;
    const custom = mode === 'CUSTOM';
    customWrap?.classList.toggle('hidden', !custom);
    customWrap?.classList.toggle('flex', custom);
    setMins(custom ? customIn?.value : btn.dataset.minutes);
    if (custom && customIn) {
      customIn.value = String(plannedMin);
    }

    modeBtns.forEach((b) => {
      const on = b === btn;
      b.classList.toggle('border-indigo-600', on);
      b.classList.toggle('bg-indigo-50', on);
      b.classList.toggle('text-indigo-700', on);
      b.classList.toggle('ring-1', on);
      b.classList.toggle('ring-indigo-200', on);
      b.classList.toggle('border-slate-200', !on);
      b.classList.toggle('bg-white', !on);
      b.classList.toggle('text-slate-700', !on);
      b.setAttribute('aria-pressed', String(on));
    });
    setStatus('Siap memulai sesi fokus');
    syncUI();
  };

  const clearTimer = () => {
    if (tickId) {
      clearInterval(tickId);
      tickId = null;
    }
  };

  const start = async () => {
    if (running || busy || phase === 'BREAK' || (sessionId && remSec === 0)) return;
    unlockAudio();
    hideCompleteBanner();
    if (!sessionId) {
      busy = true;
      setStatus('Memulai sesi fokus...');
      syncUI();
      const res = await postJson(startUrl || '/tasks/focus/start/', {
        task_id: Number(taskId),
        checkpoint_id: cpSelect?.value ? Number(cpSelect.value) : null,
        mode,
        planned_minutes: plannedMin,
      });
      sessionId = res.ok ? (res.data?.session_id ?? res.data?.id ?? null) : null;
      if (!sessionId) {
        busy = false;
        setStatus('Gagal memulai sesi fokus. Silakan coba lagi.');
        syncUI();
        return;
      }
      busy = false;
    }

    clearTimer();
    running = true;
    anchorMs = Date.now();
    anchorRem = remSec;
    setStatus('Sedang fokus — tetap tenang dan mengalir');
    document.title = `${fmt(remSec)} · Fokus`;
    syncUI();

    tickId = setInterval(() => {
      remSec = Math.max(0, anchorRem - Math.floor((Date.now() - anchorMs) / 1000));
      render();
      document.title = `${fmt(remSec)} · Fokus`;
      if (remSec === 0) {
        chime();
        finish('COMPLETED');
      }
    }, 250);
  };

  const pause = () => {
    if (!running || busy || phase === 'BREAK') return;
    clearTimer();
    running = false;
    const d = Math.floor((Date.now() - anchorMs) / 1000);
    elapsedSec += d;
    remSec = Math.max(0, anchorRem - d);
    render();
    document.title = initialTitle;
    setStatus('Sesi dijeda — tekan Lanjutkan atau Space untuk meneruskan');
    syncUI();
  };

  const finish = async (status) => {
    if (busy || phase === 'BREAK' || (!sessionId && !running)) return;
    busy = true;
    if (running) {
      clearTimer();
      running = false;
      const d = Math.floor((Date.now() - anchorMs) / 1000);
      elapsedSec += d;
      remSec = Math.max(0, anchorRem - d);
      render();
    }
    syncUI();

    const elapsed_seconds = Math.max(elapsedSec, plannedMin * 60 - remSec);
    let loggedMin = plannedMin;
    let cpMarked = false;

    if (sessionId) {
      setStatus('Menyimpan sesi fokus...');
      const url = (finishUrlTemplate || '/tasks/focus/0/finish/')
        .replace(/\/0\/finish\/?$/, `/${sessionId}/finish/`)
        .replace(/__ID__|SESSION_ID|\{session_id\}/, String(sessionId));
      const res = await postJson(url, {
        status,
        elapsed_seconds,
        mark_checkpoint_done: Boolean(markDone?.checked && status === 'COMPLETED'),
      });
      if (res.status === 409) {
        sessionId = null;
        busy = false;
        document.title = initialTitle;
        hideCompleteBanner();
        setMins(plannedMin);
        setStatus('Sesi fokus ini sudah tercatat sebelumnya. Siap memulai sesi baru.');
        window.atrToast?.('Sesi fokus ini sudah tercatat sebelumnya.', 'info');
        syncUI();
        return;
      }
      if (!res.ok || !res.data) {
        busy = false;
        document.title = initialTitle;
        setStatus('Gagal menyimpan sesi. Klik Selesai atau Batalkan untuk mencoba lagi.');
        syncUI();
        return;
      }
      const payload = res.data;
      if (payload.actual_minutes != null) loggedMin = payload.actual_minutes;
      cpMarked = Boolean(payload.checkpoint_marked);
      if (todayEl && payload.today_focus_minutes != null) {
        todayEl.textContent = payload.today_focus_minutes;
      }
      if (cpMarked && cpSelect) {
        const opt = cpSelect.options[cpSelect.selectedIndex];
        if (opt && opt.value) {
          const text = opt.textContent.trim();
          if (!text.startsWith('[Selesai]') && !text.startsWith('[Done]')) {
            opt.textContent = `[Selesai] ${text}`;
          }
          opt.disabled = true;
        }
        cpSelect.value = '';
        if (markDone) markDone.checked = false;
      }
      if (cpMarked) {
        // Checkpoint completed -> refresh alert toasts & nav badge immediately.
        document.body.dispatchEvent(new CustomEvent('remindersChanged'));
      }
    }

    sessionId = null;
    busy = false;
    document.title = initialTitle;
    setMins(plannedMin);

    if (status === 'COMPLETED') {
      setStatus('Sesi fokus selesai! Ambil jeda sejenak sebelum sesi berikutnya.');
      showCompleteBanner(loggedMin, cpMarked);
      if (typeof window.atrToast === 'function') {
        const suffix = cpMarked ? ' Checkpoint terpilih juga ditandai selesai!' : '';
        window.atrToast(`Mantap! +${loggedMin} menit fokus tercatat.${suffix}`, 'success');
      }
    } else {
      hideCompleteBanner();
      setStatus('Sesi dibatalkan. Siap memulai lagi kapan saja.');
      if (typeof window.atrToast === 'function') {
        window.atrToast('Sesi fokus dibatalkan.', 'info');
      }
    }
    syncUI();
  };

  const endBreak = (completedNaturally) => {
    clearTimer();
    phase = 'FOCUS';
    running = false;
    breakPaused = false;
    document.title = initialTitle;
    hideCompleteBanner();
    setMins(plannedMin);
    setStatus(
      completedNaturally
        ? 'Istirahat selesai! Siap lanjut sesi fokus berikutnya.'
        : 'Istirahat dilewati. Siap memulai sesi fokus.'
    );
    syncUI();
    if (completedNaturally && typeof window.atrToast === 'function') {
      window.atrToast('Waktu istirahat selesai! Siap fokus kembali.', 'info');
    }
  };

  const tickBreak = () => {
    clearTimer();
    running = true;
    breakPaused = false;
    anchorMs = Date.now();
    anchorRem = remSec;
    setStatus(`Waktu istirahat (${breakMin} menit) — regangkan bahu & minum air`);
    document.title = `${fmt(remSec)} · Istirahat`;
    syncUI();

    tickId = setInterval(() => {
      remSec = Math.max(0, anchorRem - Math.floor((Date.now() - anchorMs) / 1000));
      render();
      document.title = `${fmt(remSec)} · Istirahat`;
      if (remSec === 0) {
        chime();
        endBreak(true);
      }
    }, 250);
  };

  const startBreak = () => {
    if (busy || sessionId) return;
    unlockAudio();
    hideCompleteBanner();
    phase = 'BREAK';
    totalSec = breakMin * 60;
    remSec = totalSec;
    elapsedSec = 0;
    render();
    tickBreak();
  };

  const toggleBreakPause = () => {
    if (phase !== 'BREAK') return;
    if (running) {
      clearTimer();
      running = false;
      breakPaused = true;
      const d = Math.floor((Date.now() - anchorMs) / 1000);
      remSec = Math.max(0, anchorRem - d);
      render();
      document.title = initialTitle;
      setStatus('Istirahat dijeda — klik Lanjutkan Istirahat atau tekan Space');
      syncUI();
    } else {
      unlockAudio();
      tickBreak();
    }
  };

  window.addEventListener('beforeunload', (e) => {
    if (phase === 'FOCUS' && (running || sessionId)) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const target = e.target;
    if (
      target instanceof Element
      && target.closest('input, select, textarea, button, a, [contenteditable="true"]')
    ) {
      return;
    }

    if (e.code === 'Space' || e.key === ' ') {
      e.preventDefault();
      if (phase === 'BREAK') {
        toggleBreakPause();
      } else if (running) {
        pause();
      } else {
        start();
      }
    } else if (e.key === 'r' || e.key === 'R') {
      if (phase === 'BREAK') {
        e.preventDefault();
        endBreak(false);
      } else if (!running && !sessionId && !busy) {
        e.preventDefault();
        hideCompleteBanner();
        setMins(plannedMin);
        setStatus('Timer direset. Siap memulai sesi fokus.');
        syncUI();
      }
    }
  });

  const clampCustomMinutes = () => {
    if (!customIn) return;
    if (!running && !sessionId && !busy && phase === 'FOCUS' && mode === 'CUSTOM') {
      setMins(customIn.value);
      customIn.value = String(plannedMin);
    }
  };

  modeBtns.forEach((b) => b.addEventListener('click', () => selectMode(b)));
  customIn?.addEventListener('input', () => {
    if (!running && !sessionId && !busy && phase === 'FOCUS' && mode === 'CUSTOM') {
      setMins(customIn.value);
    }
  });
  customIn?.addEventListener('blur', clampCustomMinutes);
  customIn?.addEventListener('change', clampCustomMinutes);
  cpSelect?.addEventListener('change', syncCheckpointToggle);

  btnStart?.addEventListener('click', start);
  btnPause?.addEventListener('click', pause);
  btnDone?.addEventListener('click', () => finish('COMPLETED'));
  btnStop?.addEventListener('click', () => finish('ABANDONED'));
  btnStartBreak?.addEventListener('click', startBreak);
  btnNextFocus?.addEventListener('click', () => {
    hideCompleteBanner();
    setMins(plannedMin);
    start();
  });
  btnBreakPause?.addEventListener('click', toggleBreakPause);
  btnSkipBreak?.addEventListener('click', () => endBreak(false));

  render();
  syncUI();
})();
