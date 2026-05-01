        // ===== Loader — global UI loading API =====
        // Usage:
        //   Loader.show('Saving...')        // full-page overlay
        //   Loader.hide()
        //   Loader.button(btn, 'Saving...') // button loading state (preserves original label)
        //   Loader.buttonReset(btn)
        //   Loader.inline('#feedContent', 'Loading feed...')   // drop spinner into a container
        //   Loader.skeleton('#tbody', 5, 4)                    // 5 rows × 4 cols of shimmer cells
        //   Loader.fetch(url, opts, btn?)                      // fetch wrapper that handles button + errors
        window.Loader = (function() {
            var overlay = null, msgEl = null;
            var refCount = 0;
            function el() {
                if (!overlay) {
                    overlay = document.getElementById('ltLoaderOverlay');
                    msgEl = document.getElementById('ltLoaderMsg');
                }
                return overlay;
            }
            return {
                show: function(msg) {
                    refCount++;
                    var o = el();
                    if (!o) return;
                    if (msgEl) msgEl.textContent = msg || 'Loading...';
                    o.classList.add('show');
                    o.setAttribute('aria-hidden', 'false');
                },
                hide: function() {
                    refCount = Math.max(0, refCount - 1);
                    if (refCount > 0) return;
                    var o = el();
                    if (!o) return;
                    o.classList.remove('show');
                    o.setAttribute('aria-hidden', 'true');
                },
                hideAll: function() {
                    refCount = 0;
                    var o = el();
                    if (o) { o.classList.remove('show'); o.setAttribute('aria-hidden', 'true'); }
                },
                button: function(btn, label) {
                    if (!btn) return;
                    if (btn.dataset.ltOriginal == null) btn.dataset.ltOriginal = btn.innerHTML;
                    if (label) btn.setAttribute('aria-label', label);
                    btn.classList.add('lt-btn-loading');
                    btn.disabled = true;
                },
                buttonReset: function(btn) {
                    if (!btn) return;
                    if (btn.dataset.ltOriginal != null) {
                        btn.innerHTML = btn.dataset.ltOriginal;
                        delete btn.dataset.ltOriginal;
                    }
                    btn.classList.remove('lt-btn-loading');
                    btn.disabled = false;
                    btn.removeAttribute('aria-label');
                },
                inline: function(target, msg) {
                    var node = (typeof target === 'string') ? document.querySelector(target) : target;
                    if (!node) return;
                    node.innerHTML = '<div class="lt-loader-inline" role="status">'
                        + '<span class="lt-spinner lt-spinner-lg"></span>'
                        + '<span>' + (msg || 'Loading...') + '</span></div>';
                },
                skeleton: function(target, rows, cols) {
                    var node = (typeof target === 'string') ? document.querySelector(target) : target;
                    if (!node) return;
                    rows = rows || 5; cols = cols || 1;
                    var html = '';
                    for (var r = 0; r < rows; r++) {
                        html += '<tr>';
                        for (var c = 0; c < cols; c++) {
                            html += '<td><span class="lt-skeleton" style="height:14px;width:'
                                + (60 + Math.floor(Math.random() * 30)) + '%;"></span></td>';
                        }
                        html += '</tr>';
                    }
                    node.innerHTML = html;
                },
                fetch: function(url, opts, btn) {
                    if (btn) Loader.button(btn);
                    return fetch(url, opts || {})
                        .then(function(r) {
                            if (!r.ok) throw new Error('HTTP ' + r.status);
                            var ct = r.headers.get('content-type') || '';
                            return ct.indexOf('application/json') >= 0 ? r.json() : r.text();
                        })
                        .finally(function() { if (btn) Loader.buttonReset(btn); });
                }
            };
        })();

        // ===== Navigation loader =====
        // Show our centered loader overlay for full-page navigations so users see
        // *our* spinner, not just the browser's tiny tab spinner. Skips: external
        // links, new-tab clicks, anchor-only jumps, downloads, javascript: URLs,
        // and links that opt out via data-no-loader.
        (function() {
            var pending = null;

            function isInternal(href) {
                if (!href) return false;
                if (href.indexOf('javascript:') === 0) return false;
                if (href.indexOf('mailto:') === 0 || href.indexOf('tel:') === 0) return false;
                if (href.charAt(0) === '#') return false;
                try {
                    var u = new URL(href, location.href);
                    if (u.origin !== location.origin) return false;
                    // Same-page anchor only? skip
                    if (u.pathname === location.pathname && u.search === location.search && u.hash) return false;
                    return true;
                } catch (e) { return false; }
            }

            var safetyTimer = null;
            function scheduleShow(msg) {
                if (pending) return;
                // 250ms grace — most cached/lite navigations finish before this fires,
                // so the user only ever sees the overlay for genuinely slow loads.
                // Below ~250ms the human eye perceives a flash as "jank" rather than
                // "progress feedback", so we'd rather show nothing.
                pending = setTimeout(function() {
                    pending = null;
                    Loader.show(msg || 'Loading...');
                    // Safety net: auto-hide after 15s if navigation silently aborts
                    // (e.g. server triggers a file download instead of navigating).
                    safetyTimer = setTimeout(function() { Loader.hideAll(); }, 15000);
                }, 250);
            }

            function cancelShow() {
                if (pending) { clearTimeout(pending); pending = null; }
                if (safetyTimer) { clearTimeout(safetyTimer); safetyTimer = null; }
                Loader.hideAll();
            }

            document.addEventListener('click', function(e) {
                // Ignore modified clicks (new tab / new window / save-as)
                if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                var a = e.target.closest && e.target.closest('a');
                if (!a) return;
                if (a.target && a.target !== '' && a.target !== '_self') return;
                if (a.hasAttribute('download')) return;
                if (a.dataset.noLoader === '1') return;
                if (a.getAttribute('rel') === 'external') return;
                var href = a.getAttribute('href');
                if (!isInternal(href)) return;
                scheduleShow();
            }, true);

            document.addEventListener('submit', function(e) {
                var f = e.target;
                if (!f || f.tagName !== 'FORM') return;
                if (f.dataset.noLoader === '1') return;
                // Skip AJAX-style forms — they handle their own loading state via Loader.button()
                if (f.dataset.ajax === '1') return;
                // Skip form posts opening in a new tab
                if (f.target && f.target !== '' && f.target !== '_self') return;
                scheduleShow(f.dataset.loaderMsg || 'Saving...');
            }, true);

            // Hide overlay on bfcache restore (browser back/forward) — without this,
            // the overlay would stay visible after returning from a previous page.
            window.addEventListener('pageshow', cancelShow);
            // Also hide if the navigation is cancelled (e.g. file download starts).
            window.addEventListener('pagehide', function() { /* keep showing — page is leaving */ });
            window.addEventListener('popstate', cancelShow);

            // Expose so inline handlers can opt in/out programmatically:
            //   Loader.nav.cancel()  — cancel a pending show
            //   Loader.nav.show()    — manually trigger
            Loader.nav = { show: scheduleShow, cancel: cancelShow };
        })();
