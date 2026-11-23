(() => {
  'use strict';

  /* ========================================================================
     1. Ephemeral UX Toast System (window.atrToast)
     ======================================================================== */
  const TONE_STYLES = {
    success: {
      card: 'bg-white border-emerald-200 text-slate-800',
      iconWrap: 'bg-emerald-50 text-emerald-600',
      svg: '<svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z" clip-rule="evenodd"/></svg>',
      role: 'status',
    },
    info: {
      card: 'bg-white border-indigo-200 text-slate-800',
      iconWrap: 'bg-indigo-50 text-indigo-600',
      svg: '<svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a.75.75 0 000 1.5h.253a.25.25 0 01.244.304l-.459 2.066A1.75 1.75 0 0010.747 15H11a.75.75 0 000-1.5h-.253a.25.25 0 01-.244-.304l.459-2.066A1.75 1.75 0 009.253 9H9z" clip-rule="evenodd"/></svg>',
      role: 'status',
    },
    warning: {
      card: 'bg-white border-amber-200 text-slate-800',
      iconWrap: 'bg-amber-50 text-amber-600',
      svg: '<svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/></svg>',
      role: 'status',
    },
    error: {
      card: 'bg-white border-rose-200 text-slate-800',
      iconWrap: 'bg-rose-50 text-rose-600',
      svg: '<svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z" clip-rule="evenodd"/></svg>',
      role: 'alert',
    },
  };

  function getToastContainer() {
    let container = document.getElementById('ux-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'ux-toast-container';
      container.setAttribute('aria-live', 'polite');
      container.className = 'fixed top-16 right-4 z-50 w-80 max-w-[calc(100vw-2rem)] space-y-2 pointer-events-none';
      document.body.appendChild(container);
    }
    return container;
  }

  function dismissToast(el) {
    if (!el || el.dataset.closing === '1') return;
    el.dataset.closing = '1';
    el.classList.add('atr-toast-exit');
    window.setTimeout(() => {
      if (el.isConnected) el.remove();
    }, 220);
  }

  function atrToast(message, tone = 'info') {
    if (!message) return;
    const container = getToastContainer();
    const style = TONE_STYLES[tone] || TONE_STYLES.info;

    // Cap visible UX toasts to avoid screen clutter
    while (container.children.length >= 4) {
      container.firstElementChild.remove();
    }

    const toast = document.createElement('div');
    toast.setAttribute('role', style.role);
    toast.className = [
      'pointer-events-auto atr-toast-enter flex items-start gap-2.5 px-3.5 py-2.5',
      'rounded-xl border shadow-md text-xs sm:text-[13px] leading-snug',
      style.card,
    ].join(' ');

    const icon = document.createElement('span');
    icon.className = `shrink-0 mt-0.5 w-6 h-6 rounded-lg flex items-center justify-center ${style.iconWrap}`;
    icon.innerHTML = style.svg;

    const text = document.createElement('p');
    text.className = 'flex-1 font-medium text-slate-800 pt-0.5 break-words';
    text.textContent = String(message);

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Tutup notifikasi');
    closeBtn.className = 'shrink-0 -mr-1 p-1 rounded-lg text-slate-500 hover:text-slate-700 hover:bg-slate-100 leading-none text-base font-medium';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', () => dismissToast(toast));

    toast.append(icon, text, closeBtn);
    container.appendChild(toast);

    let timer = null;
    const scheduleClose = (ms = 3500) => {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => dismissToast(toast), ms);
    };

    toast.addEventListener('mouseenter', () => {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    });
    toast.addEventListener('mouseleave', () => scheduleClose(1800));
    scheduleClose(3500);
  }

  window.atrToast = atrToast;

  // Listen for HTMX HX-Trigger: showToast events
  document.body.addEventListener('showToast', (evt) => {
    const detail = evt.detail;
    if (!detail) return;
    if (typeof detail === 'string') {
      atrToast(detail, 'info');
    } else if (typeof detail === 'object') {
      const msg = detail.message || detail.value || detail.text;
      if (msg) atrToast(msg, detail.tone || 'info');
    }
  });

  /* ========================================================================
     2. Global HTMX Lifecycle Hooks (Progress Bar, Errors, 400/422 Swap, CSRF)
     ======================================================================== */
  const topBar = document.getElementById('htmx-top-bar');
  const activeXhrs = new Set();
  let finishTimer = null;

  function startTopBar(evt) {
    const xhr = evt && evt.detail && evt.detail.xhr;
    if (xhr) activeXhrs.add(xhr);
    if (!topBar) return;
    if (finishTimer) {
      window.clearTimeout(finishTimer);
      finishTimer = null;
    }
    topBar.classList.remove('is-finishing');
    topBar.classList.add('is-loading');
  }

  function stopTopBar(evt) {
    const xhr = evt && evt.detail && evt.detail.xhr;
    if (xhr) {
      if (!activeXhrs.has(xhr)) return;
      activeXhrs.delete(xhr);
    } else {
      activeXhrs.clear();
    }
    if (!topBar || activeXhrs.size > 0) return;
    topBar.classList.remove('is-loading');
    topBar.classList.add('is-finishing');
    finishTimer = window.setTimeout(() => {
      topBar.classList.remove('is-finishing');
      finishTimer = null;
    }, 360);
  }

  document.body.addEventListener('htmx:beforeRequest', startTopBar);
  document.body.addEventListener('htmx:afterRequest', stopTopBar);
  document.body.addEventListener('htmx:responseError', (evt) => {
    stopTopBar(evt);
    const status = evt.detail && evt.detail.xhr ? evt.detail.xhr.status : 0;
    if (status !== 400 && status !== 422) {
      atrToast('Terjadi kesalahan server. Silakan coba lagi.', 'error');
    }
  });
  document.body.addEventListener('htmx:sendError', (evt) => {
    stopTopBar(evt);
    atrToast('Koneksi terputus. Periksa internet kamu dan coba lagi.', 'error');
  });
  document.body.addEventListener('htmx:timeout', (evt) => {
    stopTopBar(evt);
    atrToast('Permintaan terlalu lama. Silakan coba lagi.', 'error');
  });
  document.body.addEventListener('htmx:abort', stopTopBar);

  document.body.addEventListener('htmx:beforeSwap', (evt) => {
    const xhr = evt.detail && evt.detail.xhr;
    const status = xhr ? xhr.status : 0;
    if (xhr && xhr.responseURL && xhr.responseURL.includes('/login/') && !window.location.pathname.includes('/login/')) {
      evt.detail.shouldSwap = false;
      window.location.href = xhr.responseURL;
      return;
    }
    if (status === 400 || status === 422) {
      evt.detail.shouldSwap = true;
      evt.detail.isError = false;
    }
  });

  document.body.addEventListener('htmx:configRequest', (evt) => {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match && match[1]) {
      evt.detail.headers['X-CSRFToken'] = decodeURIComponent(match[1]);
    }
  });

  /* ========================================================================
     3. Dismissible Django Flash Banners (Manual + 6s Auto-Fade on Info/Success)
     ======================================================================== */
  function dismissFlash(item) {
    if (!item || item.dataset.closing === '1') return;
    item.dataset.closing = '1';
    item.style.maxHeight = `${item.offsetHeight}px`;
    // Force reflow before adding exit class
    void item.offsetHeight;
    item.classList.add('atr-flash-exit');
    window.setTimeout(() => {
      if (item.isConnected) item.remove();
    }, 260);
  }

  document.addEventListener('click', (evt) => {
    const dismissBtn = evt.target.closest('[data-dismiss-flash]');
    if (!dismissBtn) return;
    const flashItem = dismissBtn.closest('[data-flash-item]');
    if (flashItem) dismissFlash(flashItem);
  });

  document.querySelectorAll('[data-flash-item][data-flash-auto-dismiss="true"]').forEach((item) => {
    let timer = null;
    const schedule = (ms) => {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => dismissFlash(item), ms);
    };
    item.addEventListener('mouseenter', () => {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    });
    item.addEventListener('focusin', () => {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    });
    item.addEventListener('mouseleave', () => schedule(2500));
    item.addEventListener('focusout', () => schedule(2500));
    schedule(6000);
  });

  /* ========================================================================
     4. Universal 1-Click Copy Buttons ([data-copy-text])
     ======================================================================== */
  async function copyToClipboard(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function' && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    ta.style.top = '0';
    document.body.appendChild(ta);
    ta.focus({ preventScroll: true });
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    if (!ok) throw new Error('Clipboard copy failed');
  }

  document.addEventListener('click', async (evt) => {
    const btn = evt.target.closest('[data-copy-text]');
    if (!btn) return;
    const text = btn.dataset.copyText;
    if (text === undefined || text === null) return;

    try {
      await copyToClipboard(text);
      const labelTarget = btn.querySelector('[data-copy-label]') || btn;
      if (btn.dataset.originalLabel === undefined) {
        btn.dataset.originalLabel = labelTarget.textContent;
      }
      labelTarget.textContent = btn.dataset.copiedLabel || 'Tersalin!';

      if (btn._copyTimer) window.clearTimeout(btn._copyTimer);
      btn._copyTimer = window.setTimeout(() => {
        if (btn.dataset.originalLabel !== undefined) {
          labelTarget.textContent = btn.dataset.originalLabel;
        }
        btn._copyTimer = null;
      }, 2000);

      atrToast(btn.dataset.copyToast || 'Berhasil disalin ke clipboard!', 'success');
    } catch (_) {
      atrToast('Gagal menyalin ke clipboard.', 'error');
    }
  });

  /* ========================================================================
     5. Inline Editor Ergonomics ([data-toggle-editor], [data-close-editor] & Escape)
     ======================================================================== */
  function closeInlineEditor(editor, { restoreFocus = false } = {}) {
    if (!editor || editor.classList.contains('hidden')) return;
    editor.classList.add('hidden');
    const form = editor.tagName === 'FORM' ? editor : editor.querySelector('form');
    if (form && typeof form.reset === 'function') {
      form.reset();
    }
    editor.querySelectorAll('[data-orig-value]').forEach((field) => {
      field.value = field.dataset.origValue;
    });
    editor.querySelectorAll('[data-editor-error]').forEach((errEl) => errEl.remove());

    let primaryTrigger = null;
    if (editor.id) {
      const safeId = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
        ? CSS.escape(editor.id)
        : editor.id.replace(/[^a-zA-Z0-9_-]/g, '');
      document.querySelectorAll(`[data-toggle-editor="#${safeId}"]`).forEach((trigger) => {
        trigger.setAttribute('aria-expanded', 'false');
        if (!primaryTrigger) primaryTrigger = trigger;
      });
    }
    if (restoreFocus && primaryTrigger && typeof primaryTrigger.focus === 'function') {
      primaryTrigger.focus();
    }
  }

  document.addEventListener('click', (evt) => {
    const trigger = evt.target.closest('[data-toggle-editor]');
    if (trigger) {
      const selector = trigger.dataset.toggleEditor;
      if (!selector) return;

      const target = document.querySelector(selector);
      if (!target) return;

      const willOpen = target.classList.contains('hidden');
      if (willOpen) {
        target.classList.remove('hidden');
        trigger.setAttribute('aria-expanded', 'true');
        const input = target.querySelector('input[type="text"]') || target.querySelector('input:not([type="hidden"]), textarea');
        if (input) {
          input.focus();
          if (typeof input.select === 'function') {
            input.select();
          }
        }
      } else {
        closeInlineEditor(target);
        trigger.setAttribute('aria-expanded', 'false');
      }
      return;
    }

    const closeBtn = evt.target.closest('[data-close-editor]');
    if (!closeBtn) return;
    const selector = closeBtn.dataset.closeEditor;
    const target = selector
      ? document.querySelector(selector)
      : closeBtn.closest('[data-inline-editor]');
    if (target) {
      closeInlineEditor(target, { restoreFocus: true });
    }
  });

  document.addEventListener('keydown', (evt) => {
    if (evt.key !== 'Escape') return;

    const openEditors = [...document.querySelectorAll('[data-inline-editor]:not(.hidden)')];
    if (openEditors.length) {
      const focusedEditor = openEditors.find((ed) => ed.contains(document.activeElement));
      openEditors.forEach((editor) => {
        closeInlineEditor(editor, {
          restoreFocus: focusedEditor ? editor === focusedEditor : true,
        });
      });
    }

    const openPanels = [...document.querySelectorAll('[data-dismiss-on-escape]:not(.hidden)')];
    if (openPanels.length) {
      const focusedPanel = openPanels.find((panel) => panel.contains(document.activeElement));
      openPanels.forEach((panel) => {
        panel.classList.add('hidden');
        if (panel.id) {
          const safeId = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
            ? CSS.escape(panel.id)
            : panel.id.replace(/[^a-zA-Z0-9_-]/g, '');
          document.querySelectorAll(`[aria-controls="${safeId}"][aria-expanded]`).forEach((ctrl) => {
            ctrl.setAttribute('aria-expanded', 'false');
          });
        }
        const returnSelector = panel.dataset.returnFocus;
        if (returnSelector) {
          const returnTarget = document.querySelector(returnSelector);
          if (returnTarget) {
            if (returnTarget.hasAttribute('aria-expanded')) {
              returnTarget.setAttribute('aria-expanded', 'false');
            }
            if ((!focusedPanel || panel === focusedPanel) && typeof returnTarget.focus === 'function') {
              returnTarget.focus();
            }
          }
        }
      });
    }
  });

  /* ========================================================================
     6. Password Show/Hide Toggle ([data-toggle-password])
     ======================================================================== */
  document.addEventListener('click', (evt) => {
    const btn = evt.target.closest('[data-toggle-password]');
    if (!btn) return;
    const targetRef = btn.dataset.togglePassword;
    let input = null;
    if (targetRef) {
      input = targetRef.startsWith('#') || targetRef.startsWith('.') || targetRef.startsWith('[')
        ? document.querySelector(targetRef)
        : document.getElementById(targetRef);
    }
    if (!input) {
      const container = btn.closest('[data-password-field]') || btn.parentElement;
      input = container ? container.querySelector('input[type="password"], input[type="text"]') : null;
    }
    if (!input) return;

    const isHidden = input.type === 'password';
    input.type = isHidden ? 'text' : 'password';
    btn.setAttribute('aria-pressed', isHidden ? 'true' : 'false');
    const labelEl = btn.querySelector('[data-toggle-password-label]') || btn;
    labelEl.textContent = isHidden ? 'Sembunyikan' : 'Lihat';
  });
})();
