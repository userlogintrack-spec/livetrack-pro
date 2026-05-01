    function showToast(msg, type) {
        var wrap = document.getElementById('toastWrap');
        var el = document.createElement('div');
        el.style.cssText = 'pointer-events:auto;min-width:240px;max-width:400px;padding:12px 16px;border-radius:10px;font-size:13px;font-weight:500;line-height:1.4;box-shadow:0 8px 24px rgba(0,0,0,0.15);transform:translateX(20px);opacity:0;transition:all .25s ease;';
        if (type === 'error') el.style.background = '#991b1b', el.style.color = '#fff';
        else if (type === 'warning') el.style.background = '#92400e', el.style.color = '#fff';
        else if (type === 'success') el.style.background = '#065f46', el.style.color = '#fff';
        else el.style.background = 'var(--bg-card)', el.style.color = 'var(--text-primary)', el.style.border = '1px solid var(--border)';
        el.textContent = msg;
        wrap.appendChild(el);
        requestAnimationFrame(function() { el.style.transform = 'translateX(0)'; el.style.opacity = '1'; });
        setTimeout(function() { el.style.transform = 'translateX(20px)'; el.style.opacity = '0'; setTimeout(function() { if (el.parentNode) el.parentNode.removeChild(el); }, 300); }, 3000);
    }
    function showConfirm(msg, onOk, title) {
        var overlay = document.getElementById('confirmOverlay');
        document.getElementById('confirmTitle').textContent = title || 'Confirm';
        document.getElementById('confirmMsg').textContent = msg;
        overlay.style.display = 'flex';
        var okBtn = document.getElementById('confirmOk');
        var cancelBtn = document.getElementById('confirmCancel');
        function cleanup() { overlay.style.display = 'none'; okBtn.onclick = null; cancelBtn.onclick = null; overlay.onclick = null; }
        okBtn.onclick = function() { cleanup(); if (onOk) onOk(); };
        cancelBtn.onclick = cleanup;
        overlay.onclick = function(e) { if (e.target === overlay) cleanup(); };
    }

    var NotificationManager = {
        permission: typeof Notification !== 'undefined' ? Notification.permission : 'denied',
        soundEl: null,
        _muted: JSON.parse(localStorage.getItem('notif_muted') || 'false'),
        _lastNotif: {},

        init: function() {
            // Create notification sound
            this.soundEl = document.createElement('audio');
            this.soundEl.preload = 'auto';
            this.soundEl.volume = 0.5;
            this.soundEl.innerHTML = '<source src="data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdH2Onp6YkYOAe3l9hY6Xnp6XjYN9eHl+h5CZnp6WjIN8d3h9hpCZnp6WjYR9eHl+hpCZnZyUi4J7d3h+hZCYnZyUjIN8eHl+hpCYnZyVjIR9eHp/h5GZnp6WjYR9eHl+hpCZnp6WjYR9eHl+h5GZnp6WjYR9eHl+hpCYnZyUjIN8eHl+hpGZnp6WjYR9eHl+hpCYnp6WjYR9eHl+h5GZnp6WjYR9eHp/h5GZnp6WjYR9eHl+hpCZnp6WjYR8eHl+hpGZnp6WjYR9eHl+h5GZnZyVjIR9eHl+hpCZnp6WjYR9eHl+h5GZnp6WjYR9eHl+h5GZnp6WjYR9eHl+h5GZnZyVjIR9eHl+hpGZnp6WjYR9eHl+h5GZnZyVjIN8eHl+hpGZnp6WjYR9" type="audio/wav">';
            document.body.appendChild(this.soundEl);

            // Auto-request browser notification permission
            if (typeof Notification !== 'undefined' && this.permission === 'default') {
                var self = this;
                Notification.requestPermission().then(function(p) { self.permission = p; });
            }
        },

        notify: function(opts) {
            var category = opts.category || 'general';
            var title = opts.title || 'Notification';
            var body = opts.body || '';
            var severity = opts.severity || 'info';
            var url = opts.url || '';
            var sound = opts.sound !== false;

            // Rate limit: max 1 notification per category per 10 seconds
            var now = Date.now();
            if (this._lastNotif[category] && (now - this._lastNotif[category]) < 10000) return;
            this._lastNotif[category] = now;

            // 1. Toast notification
            var toastType = severity === 'error' ? 'error' : severity === 'warning' ? 'warning' : 'success';
            showToast(title + ' - ' + body, toastType);

            // 2. Browser desktop notification (when tab not focused)
            if (!document.hasFocus() && this.permission === 'granted') {
                var n = new Notification(title, {
                    body: body,
                    icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect fill="%237c3aed" rx="20" width="100" height="100"/><text x="50" y="68" font-size="50" text-anchor="middle" fill="white">L</text></svg>',
                    tag: 'lvh-' + category,
                    requireInteraction: severity === 'error',
                });
                if (url) {
                    n.onclick = function() { window.focus(); window.location.href = url; n.close(); };
                } else {
                    n.onclick = function() { window.focus(); n.close(); };
                }
                if (severity !== 'error') setTimeout(function() { n.close(); }, 8000);
            }

            // 3. Sound alert
            if (sound && !this._muted && this.soundEl) {
                try { this.soundEl.currentTime = 0; this.soundEl.play(); } catch(e) {}
            }

            // 4. Tab title flash when unfocused
            if (!document.hasFocus() && !window._titleFlash) {
                var baseTitle = window._baseTitle || document.title;
                window._baseTitle = baseTitle;
                var flashOn = true;
                window._titleFlash = setInterval(function() {
                    document.title = flashOn ? '** ' + title + ' **' : baseTitle;
                    flashOn = !flashOn;
                }, 1500);
                window.addEventListener('focus', function clearFlash() {
                    clearInterval(window._titleFlash);
                    window._titleFlash = null;
                    document.title = baseTitle;
                    window.removeEventListener('focus', clearFlash);
                }, {once: true});
            }
        },

        mute: function() { this._muted = true; localStorage.setItem('notif_muted', 'true'); },
        unmute: function() { this._muted = false; localStorage.setItem('notif_muted', 'false'); },
    };

    document.addEventListener('DOMContentLoaded', function() { NotificationManager.init(); });
