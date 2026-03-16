// ─── State ─────────────────────────────────────────────────────────────────
const SID = 'demo';
let ws  = null;
let sse = null;
let chatState = 'idle';
let trackerItems = {};
let trackerEl    = null;
let platProgressEl = null;
let platformSelectorShown = false;  // guard: show platform picker only once per execution
let executing = false;
let connectedPlatforms = { blinkit: false, zepto: false };

// Forward demo token (if present in URL) to all backend connections
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const _qs = (base) => base + (TOKEN ? '&token=' + encodeURIComponent(TOKEN) : '');


// ─── WebSocket ─────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + _qs('/chat?sid=' + SID));
  ws.onopen  = () => setStatus('on', 'Connected');
  ws.onclose = () => { setStatus('', 'Reconnecting…'); setTimeout(connectWS, 2500); };
  ws.onerror = () => setStatus('', 'Connection error');
  ws.onmessage = (e) => {
    try { const d = JSON.parse(e.data); if (!d.ok && d.error) console.warn('WS:', d.error); } catch {}
  };
}

// ─── SSE ───────────────────────────────────────────────────────────────────
// Track which cart-summary platforms have already been rendered to avoid
// duplicates when the SSE stream replays the replay-buffer on reconnect.
var renderedCartSummaries = {};
var renderedComparison    = false;  // only render comparison card once per session

function connectSSE() {
  if (sse) { try { sse.close(); } catch(e) {} }
  sse = new EventSource(_qs('/events?sid=' + SID));
  sse.onmessage = (e) => { try { handleEv(JSON.parse(e.data)); } catch(err) { console.error('SSE handleEv error:', err); } };
  sse.onerror   = () => setTimeout(connectSSE, 3000);
}

function handleEv(ev) {
  if (ev.type === 'ping')              return;
  if (ev.type === 'screenshot')        { showShot(ev.data, ev.label); return; }
  if (ev.type === 'chat')              { onChat(ev); return; }
  if (ev.type === 'item_status')       { updateTracker(ev.item, ev.status, ev.detail || ''); return; }
  if (ev.type === 'platform_progress') { onPlatProgress(ev.platform, ev.message); return; }
  if (ev.type === 'comparison') {
    if (!renderedComparison) {
      renderedComparison = true;
      try { renderComparison(ev); } catch(e) { console.error('renderComparison error:', e); }
    }
    return;
  }
  if (ev.type === 'cart_summary') {
    // Prefer backend-provided summary_id for stable dedupe across reconnects.
    // If summary_id is missing, do NOT dedupe to avoid hiding valid summaries.
    var fp = ev.summary_id ? ('sid|' + String(ev.summary_id)) : '';
    if (fp && renderedCartSummaries[fp]) return;
    if (fp) renderedCartSummaries[fp] = true;
    try { renderCartSummary(ev); } catch(e) { console.error('renderCartSummary error:', e); }
    return;
  }
  if (ev.type === 'platform_status')   { onPlatformStatus(ev.platform, ev.connected); return; }
}

// ─── Chat event handler ────────────────────────────────────────────────────
function onChat(ev) {
  const prevState = chatState;
  chatState = ev.chat_state || chatState;
  updateSub(chatState);

  // Skip user messages already rendered optimistically
  if (ev.role === 'user') return;

  addBubble('assistant', ev.message);

  // Shopping-list confirmation card
  if (ev.items && ev.items.length > 0) {
    addShoppingList(ev.items);
    addConfirmButtons();
    return;
  }

  // Platform picker — show only once per execution cycle
  if (chatState === 'executing' && !platformSelectorShown) {
    platformSelectorShown = true;
    addPlatformPicker();
    return;
  }

  // Preference review shortcuts
  if (chatState === 'pref_review') {
    addActionRow([
      { label: 'Continue \u2192',    cls: 'btn-yes', fn: () => sendText('no')     },
      { label: '\u270e Update Prefs', cls: 'btn-sec', fn: () => sendText('update') },
    ]);
    return;
  }

  // Login flow: update input placeholder/hint
  if (chatState === 'login_waiting') {
    setInputHint('login', 'Type "done" once you\'re logged in\u2026');
    return;
  }
  if (chatState === 'login_phone') {
    setInputHint('login', 'Enter your phone number (e.g. 9876543210)\u2026');
    return;
  }
  if (chatState === 'login_otp') {
    setInputHint('login', 'Enter the OTP you received\u2026');
    return;
  }

  // Done state
  if (chatState === 'done') {
    executing = false;
    platformSelectorShown = false;
    setStatus('on', 'Done');
    removeCancelBtn();
    document.getElementById('ihint').textContent = '';   // clear stale shopping hint
    addActionRow([
      { label: '➕ Add more items', cls: 'btn-yes', fn: () => doAddMore() },
      { label: '↺ Start over',     cls: 'btn-sec', fn: () => doRestart() },
    ]);
  }

  if (chatState === 'error') {
    executing = false;
    platformSelectorShown = false;
    setStatus('on', 'Ready');
    removeCancelBtn();
    setInputHint('normal', '');
  }
  if (chatState === 'idle') {
    setInputHint('normal', '');
  }
}

// ─── DOM helpers ───────────────────────────────────────────────────────────
function mk(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls)  e.className = cls;
  if (html) e.innerHTML = html;
  return e;
}
function msgs()      { return document.getElementById('msgs'); }
function scrollEnd() { const m = msgs(); requestAnimationFrame(() => { m.scrollTop = m.scrollHeight; }); }
function addWidget(node) { msgs().appendChild(node); scrollEnd(); }

function addBubble(role, text) {
  const wrap = mk('div', 'msg ' + role);
  const av   = mk('div', 'av');
  av.textContent = role === 'user' ? '👤' : '🤖';
  const b = mk('div', 'bubble');
  b.innerHTML = fmt(text);
  wrap.appendChild(av);
  wrap.appendChild(b);
  msgs().appendChild(wrap);
  scrollEnd();
}

function fmt(t) {
  return t
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g,       '<em>$1</em>')
    .replace(/\n/g,               '<br>');
}

function addActionRow(actions) {
  const row = mk('div', 'btn-row');
  actions.forEach(a => {
    const b = mk('button', 'btn ' + a.cls);
    b.innerHTML = a.label;
    b.onclick = () => { row.remove(); a.fn(); };
    row.appendChild(b);
  });
  addWidget(row);
}

function addConfirmButtons() {
  addActionRow([
    { label: '✅ Yes, add to cart', cls: 'btn-yes', fn: () => sendText('yes') },
    { label: '✗ Cancel',           cls: 'btn-no',  fn: () => sendText('no')  },
    { label: '✎ Edit list',        cls: 'btn-sec', fn: () => {
      const b = document.getElementById('ibox');
      b.focus();
      b.placeholder = 'e.g. change tomatoes to 1kg, remove garlic…';
    }},
  ]);
}

// ─── Platform connection UI ────────────────────────────────────────────────
function onPlatformStatus(platform, connected) {
  connectedPlatforms[platform] = connected;
  var dot = document.getElementById('pi-' + platform);
  if (dot) dot.className = 'pdot' + (connected ? ' conn' : '');
  // Refresh modal if it's currently open
  var modal = document.getElementById('acctModal');
  if (modal && !modal.classList.contains('hidden')) {
    showConnectionCard();
  }
}

function setInputHint(mode, text) {
  var hint = document.getElementById('ihint');
  var ibox = document.getElementById('ibox');
  if (mode === 'login') {
    hint.innerHTML = '<span class="login-hint">\uD83D\uDD11 ' + text + '</span>';
    ibox.placeholder = text;
  } else {
    hint.innerHTML = 'Enter to send \u00b7 Shift+Enter for new line';
    ibox.placeholder = 'e.g. Make butter chicken for 4 people\u2026';
  }
}

function closeAcctModal() {
  document.getElementById('acctModal').classList.add('hidden');
}

function showConnectionCard() {
  var inner = document.getElementById('acctModalInner');
  inner.innerHTML = '';

  var hdr = mk('div', 'conn-card-hdr');
  hdr.innerHTML = '\uD83D\uDD17 Connected Accounts';
  var closeBtn = mk('button', 'conn-close');
  closeBtn.innerHTML = '&times;';
  closeBtn.onclick = closeAcctModal;
  hdr.appendChild(closeBtn);
  inner.appendChild(hdr);

  var PLAT_META = [
    { id: 'blinkit',   label: 'Blinkit',         ico: '\uD83D\uDFE2' },
    { id: 'zepto',     label: 'Zepto',           ico: '\uD83D\uDFE3' },
  ];
  PLAT_META.forEach(function(p) {
    var conn = connectedPlatforms[p.id];
    var row  = mk('div', 'conn-row');
    var ico  = mk('span', 'conn-ico'); ico.textContent = p.ico;
    var nam  = mk('span', 'conn-name'); nam.textContent = p.label;
    row.appendChild(ico);
    row.appendChild(nam);
    if (conn) {
      var badge = mk('span', 'conn-badge ok');
      badge.textContent = '\u2705 Connected';
      row.appendChild(badge);
      var rebtn = mk('button', 'btn-connect');
      rebtn.style.cssText = 'font-size:11px;padding:3px 9px;margin-left:6px;opacity:.7';
      rebtn.textContent = '\u21BA Re-login';
      rebtn.onclick = (function(pid) {
        return function() { closeAcctModal(); doConnectPlatform(pid); };
      })(p.id);
      row.appendChild(rebtn);
    } else {
      var btn = mk('button', 'btn-connect');
      btn.textContent = 'Connect \u2192';
      btn.onclick = (function(pid) {
        return function() { closeAcctModal(); doConnectPlatform(pid); };
      })(p.id);
      row.appendChild(btn);
    }
    inner.appendChild(row);
  });

  var note = mk('div', 'conn-note');
  note.textContent =
    'Your credentials are never stored \u2014 only browser session cookies are saved locally on the server.';
  inner.appendChild(note);

  document.getElementById('acctModal').classList.remove('hidden');
}

function doConnectPlatform(platform) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  setStatus('busy', 'Connecting ' + pname(platform) + '\u2026');
  var dot = document.getElementById('pi-' + platform);
  if (dot) dot.className = 'pdot busy';
  ws.send(JSON.stringify({ type: 'connect_platform', platform: platform }));
}

// ─── Shopping list card ────────────────────────────────────────────────────
function addShoppingList(items) {
  const card = mk('div', 'slist');
  const hdr  = mk('div', 'slist-hdr');
  hdr.innerHTML = '🛒 Shopping List — <strong>' + items.length + ' item(s)</strong>';
  card.appendChild(hdr);

  const groups = {};
  items.forEach(it => {
    const g = it.source_recipe || 'Items';
    if (!groups[g]) groups[g] = [];
    groups[g].push(it);
  });
  const gkeys = Object.keys(groups);
  gkeys.forEach(g => {
    if (gkeys.length > 1) {
      const gl = mk('div', 'slist-grp');
      gl.textContent = g;
      card.appendChild(gl);
    }
    groups[g].forEach(it => {
      const r = mk('div', 'slist-row');
      r.innerHTML = '<span class="slist-name">' + it.name + '</span>' +
                    '<span class="slist-qty">'  + (it.quantity || '1 unit') + '</span>';
      card.appendChild(r);
    });
  });
  addWidget(card);
  initTracker(items);
}

// ─── Platform picker ───────────────────────────────────────────────────────
const PLATS = [
  { id: 'blinkit',   label: '🟢 Blinkit',   cls: 'blinkit'   },
  { id: 'zepto',     label: '🟣 Zepto',     cls: 'zepto'     },
];
const COMBOS = [
  { ids: ['blinkit', 'zepto'],              label: '🟢🟣 Blinkit + Zepto'         },
];

function addPlatformPicker() {
  const wrap = mk('div', 'widget');
  const lbl  = mk('div', 'plat-label');
  lbl.textContent = 'Where to shop:';
  wrap.appendChild(lbl);

  const r1 = mk('div', 'btn-row');
  PLATS.forEach(p => {
    const b = mk('button', 'btn btn-plat ' + p.cls);
    b.innerHTML = p.label;
    b.onclick = () => { wrap.remove(); startExec([p.id]); };
    r1.appendChild(b);
  });
  wrap.appendChild(r1);

  const r2 = mk('div', 'btn-row');
  COMBOS.forEach(c => {
    const b = mk('button', 'btn btn-plat all');
    b.style.fontSize = '11px';
    b.innerHTML = c.label;
    b.onclick = () => { wrap.remove(); startExec(c.ids); };
    r2.appendChild(b);
  });
  wrap.appendChild(r2);
  addWidget(wrap);
}

function startExec(platforms) {
  executing = true;
  // New shopping run: clear cart summary dedupe so this run's summary always renders.
  renderedCartSummaries = {};
  setStatus('busy', 'Shopping on ' + platforms.map(pname).join(' + '));
  document.getElementById('ihint').textContent = 'Agent is shopping — watch the browser →';

  const cb = mk('button', 'btn btn-cancel');
  cb.id = 'cancelBtn';
  cb.innerHTML = '✕ Cancel';
  cb.onclick = () => { cb.remove(); doCancel(); };
  addWidget(cb);

  waitForWS(() => ws.send(JSON.stringify({ type: 'platform', platforms: platforms })));
}

function removeCancelBtn() {
  const cb = document.getElementById('cancelBtn');
  if (cb) cb.remove();
}

// ─── Tracker ───────────────────────────────────────────────────────────────
function initTracker(items) {
  trackerItems = {};
  items.forEach(it => { trackerItems[it.name] = { status: 'pending', detail: '' }; });
}

function updateTracker(name, status, detail) {
  if (trackerItems[name] !== undefined) trackerItems[name] = { status: status, detail: detail };
  if (trackerEl) trackerEl.remove();
  const keys = Object.keys(trackerItems);
  if (!keys.length) return;

  const total = keys.length;
  const done  = Object.values(trackerItems).filter(t => t.status === 'added').length;
  const pct   = total ? Math.round(done / total * 100) : 0;

  const node = mk('div', 'tracker');
  trackerEl  = node;
  node.innerHTML =
    '<div class="tracker-hdr"><span>Cart Progress</span><span>' + done + '/' + total + ' added</span></div>' +
    '<div class="pbar"><div class="pfill" style="width:' + pct + '%"></div></div>';

  const ICONS = { pending: '⬜', searching: '⏳', added: '✅', skipped: '⏭', failed: '❌' };
  keys.forEach(n => {
    const s   = trackerItems[n];
    const row = mk('div', 'trow ' + s.status);
    row.innerHTML =
      '<span class="tico">'   + (ICONS[s.status] || '⬜') + '</span>' +
      '<span class="tname">'  + n + '</span>' +
      '<span class="tdetail">' + s.detail + '</span>';
    node.appendChild(row);
  });
  addWidget(node);
}

// ─── Platform progress ─────────────────────────────────────────────────────
function onPlatProgress(platform, message) {
  if (!platProgressEl) { platProgressEl = mk('div', 'pstrip'); addWidget(platProgressEl); }
  let row = platProgressEl.querySelector('[data-p="' + platform + '"]');
  if (!row) {
    row = mk('div', 'pstrip-row');
    row.setAttribute('data-p', platform);
    row.innerHTML = '<div class="spin"></div><span></span>';
    platProgressEl.appendChild(row);
  }
  row.querySelector('span').textContent = message;
  scrollEnd();
}

// ─── Comparison card ───────────────────────────────────────────────────────
function renderComparison(ev) {
  if (platProgressEl) { platProgressEl.remove(); platProgressEl = null; }
  const card = mk('div', 'cmp');
  card.innerHTML = '<div class="cmp-hdr">📊 Platform Comparison — Best Deal</div>';

  // Sort: recommended first, then alphabetical
  const sorted = (ev.platforms || []).slice().sort((a, b) => {
    if (a === ev.recommended) return -1;
    if (b === ev.recommended) return 1;
    return a.localeCompare(b);
  });

  // ── Per-item price comparison table (if multiple platforms) ──────────────
  if (sorted.length > 1) {
    // Collect all unique item names across all platforms
    const allItemNames = [];
    const seenNames = new Set();
    sorted.forEach(p => {
      ((ev.results[p] || {}).items || []).forEach(it => {
        if (!seenNames.has(it.name)) { seenNames.add(it.name); allItemNames.push(it.name); }
      });
    });

    if (allItemNames.length > 0) {
      const tbl = mk('div', '');
      tbl.style.cssText = 'overflow-x:auto;border-bottom:1px solid var(--gray-200)';

      // Build a lookup: platform → item_name → item
      const lookup = {};
      sorted.forEach(p => {
        lookup[p] = {};
        ((ev.results[p] || {}).items || []).forEach(it => { lookup[p][it.name] = it; });
      });

      // Header row
      let tHead = '<table style="width:100%;border-collapse:collapse;font-size:11.5px">'
        + '<thead><tr style="background:var(--gray-50)">'
        + '<th style="text-align:left;padding:6px 13px;font-weight:600;color:var(--gray-600)">Item</th>';
      sorted.forEach(p => {
        const isW = p === ev.recommended;
        tHead += '<th style="padding:6px 8px;font-weight:600;color:' + (isW ? 'var(--green-d)' : 'var(--gray-600)') + '">'
          + pname(p) + (isW ? ' ★' : '') + '</th>';
      });
      tHead += '</tr></thead><tbody>';

      let tBody = '';
      allItemNames.forEach((name, idx) => {
        const rowBg = idx % 2 === 0 ? '#fff' : 'var(--gray-50)';
        tBody += '<tr style="background:' + rowBg + '">'
          + '<td style="padding:5px 13px;color:var(--gray-700)">' + name + '</td>';
        sorted.forEach(p => {
          const it = lookup[p][name];
          let cell = '—';
          if (it) {
            const ico = ({added:'✅',skipped:'⏭',failed:'❌'})[it.status] || '❓';
            const price = it.price || '';
            const sub = it.alt ? '<span style="font-size:10px;color:#92400e"> ⚡sub</span>' : '';
            cell = ico + ' ' + (price || (it.status === 'added' ? '?' : '—')) + sub;
          }
          tBody += '<td style="padding:5px 8px;text-align:center">' + cell + '</td>';
        });
        tBody += '</tr>';
      });

      tbl.innerHTML = tHead + tBody + '</tbody></table>';
      card.appendChild(tbl);
    }
  }

  // ── Per-platform summary rows ─────────────────────────────────────────────
  sorted.forEach(p => {
    const r   = (ev.results && ev.results[p]) || {};
    const isR = p === ev.recommended;
    const row = mk('div', 'cmp-row' + (isR ? ' cmp-winner' : ''));

    const items = r.items || [];
    const failedItems = items.filter(i => i.status === 'failed');

    row.innerHTML =
      '<span class="cbadge cb-' + p + '">' + pname(p) + (isR ? ' ★' : '') + '</span>' +
      '<div style="flex:1">' +
        '<div class="cmp-total">' + (r.grand_total || (r.error ? '⚠ Error' : '—')) + '</div>' +
        '<div class="cmp-sub">'   + (r.coverage || '—') + ' items added' +
          (r.delivery ? ' · ' + r.delivery + ' delivery' : ' · FREE delivery') +
          (r.savings ? ' · 💚 ' + r.savings + ' saved' : '') +
          (failedItems.length ? ' · ⚠ ' + failedItems.length + ' failed' : '') +
        '</div>' +
      '</div>';
    card.appendChild(row);
  });

  // ── Recommendation banner ─────────────────────────────────────────────────
  if (ev.recommended) {
    const rec = mk('div', 'cmp-rec');
    rec.innerHTML = '★ <strong>Recommendation: ' + pname(ev.recommended) + '</strong> — ' + (ev.reason || '');
    card.appendChild(rec);
  }
  addWidget(card);
}

// ─── Cart summary ──────────────────────────────────────────────────────────
// function renderCartSummary(ev) {
//   if (platProgressEl) { platProgressEl.remove(); platProgressEl = null; }
//   const card = mk('div', 'cart');

//   // ── Header ────────────────────────────────────────────────────────────────
//   const hdr = mk('div', 'cart-hdr');
//   const addedCount  = (ev.items || []).filter(i => i.status === 'added').length;
//   const totalCount  = (ev.items || []).length;
//   const failedItems = (ev.items || []).filter(i => i.status === 'failed');
//   const subItems    = (ev.items || []).filter(i => i.alt);
//   const qtyWarnItems= (ev.items || []).filter(i => i.qty_note);
//   let hdrText = '🛒 Cart — ' + pname(ev.platform) + '  ·  ' + addedCount + '/' + totalCount + ' added';
//   if (ev.grand_total) hdrText += '  ·  ' + ev.grand_total;
//   hdr.innerHTML = hdrText;
//   card.appendChild(hdr);

//   // ── Item rows ─────────────────────────────────────────────────────────────
//   (ev.items || []).forEach(it => {
//     const ico  = ({ added: '✅', skipped: '⏭', failed: '❌' })[it.status] || '❓';
//     let tags = '';
//     if (it.alt)       tags += '<span class="tag tag-sub">⚡ Substituted</span>';
//     if (it.qty_note)  tags += '<span class="tag tag-warn">⚠ Qty mismatch</span>';
//     if (it.from_prev) tags += '<span class="tag tag-prev">↩ Was in cart</span>';
//     if (it.status === 'failed') tags += '<span class="tag tag-na">✗ Unavailable</span>';

//     const row = mk('div', 'cart-row ' + (it.status || ''));

//     // Quantity requested vs qty ordered (only show when they differ)
//     const qtyRequested = it.qty_requested ? 'Requested: ' + it.qty_requested : '';
//     const qtyOrdered   = it.qty ? 'Ordered: ' + it.qty : '';
//     const qtyDiff      = (qtyRequested && qtyOrdered && it.qty_note)
//       ? '<div style="font-size:11px;color:#b45309;margin-top:2px">' +
//           qtyRequested + ' → ' + qtyOrdered + '</div>' : '';

//     // Savings per line
//     const savingsHtml  = it.savings ? '<div style="font-size:11px;color:var(--green-d)">💚 Saved ' + it.savings + '</div>' : '';

//     // Reasoning for substitution / unavailability
//     const reasonHtml   = it.alt_reason
//       ? '<div style="font-size:11px;color:var(--orange);margin-top:2px">↳ ' + it.alt_reason + '</div>' : '';

//     // Quantity mismatch note
//     const qtyNoteHtml  = it.qty_note
//       ? '<div style="font-size:11px;color:#b91c1c;margin-top:2px">⚠ ' + it.qty_note + '</div>' : '';

//     row.innerHTML =
//       '<span class="cart-ico">' + ico + '</span>' +
//       '<div class="cart-info">' +
//         '<div class="cart-name">' + it.name + '</div>' +
//         '<div class="cart-product">' + (it.product || '') + (it.qty ? ' · ' + it.qty : '') + '</div>' +
//         tags +
//         qtyDiff +
//         qtyNoteHtml +
//         reasonHtml +
//         savingsHtml +
//       '</div>' +
//       '<div class="cart-right">' +
//         '<div class="cart-price">' + (it.price || (it.status === 'failed' ? '—' : '')) + '</div>' +
//       '</div>';
//     card.appendChild(row);
//   });

//   // ── Bill breakdown ────────────────────────────────────────────────────────
//   const hasBill = ev.grand_total || ev.subtotal || ev.delivery || ev.handling || ev.platform_fee;
//   if (hasBill) {
//     const bill = mk('div', 'bill');
//     const rows = [
//       ['Items subtotal', ev.subtotal],
//       ['Delivery',       ev.delivery || (ev.delivery === '' ? null : 'FREE')],
//       ['Handling fee',   ev.handling],
//       ['Platform fee',   ev.platform_fee],
//     ].filter(r => r[1]);
//     rows.forEach(function(pair) {
//       bill.innerHTML += '<div class="bill-r"><span>' + pair[0] + '</span><span>' + pair[1] + '</span></div>';
//     });
//     if (ev.savings)     bill.innerHTML += '<div class="bill-r sav"><span>💚 Total savings</span><span>' + ev.savings + '</span></div>';
//     if (ev.grand_total) bill.innerHTML += '<div class="bill-r tot"><span>Grand Total</span><span>' + ev.grand_total + '</span></div>';
//     card.appendChild(bill);
//   }

//   // ── Alerts section (unavailable + substitutions summary) ─────────────────
//   const alerts = [];
//   if (failedItems.length) {
//     alerts.push('⚠ ' + failedItems.length + ' item(s) could not be added: ' +
//       failedItems.map(i => i.name).join(', '));
//   }
//   if (subItems.length) {
//     const subList = subItems.map(i =>
//       i.name + (i.alt_reason ? ' (' + i.alt_reason + ')' : '')
//     ).join('; ');
//     alerts.push('⚡ Substituted: ' + subList);
//   }
//   if (qtyWarnItems.length) {
//     alerts.push('📦 Quantity notes: ' + qtyWarnItems.map(i => i.name + ' — ' + i.qty_note).join('; '));
//   }
//   if (alerts.length) {
//     const alertBox = mk('div', '');
//     alertBox.style.cssText = 'padding:8px 13px;background:#fffbeb;border-top:1px solid #fde68a;font-size:11.5px;color:#92400e;line-height:1.7';
//     alertBox.innerHTML = alerts.map(a => '<div>' + a + '</div>').join('');
//     card.appendChild(alertBox);
//   }

//   addWidget(card);
// }

function renderCartSummary(ev) {
  if (platProgressEl) { platProgressEl.remove(); platProgressEl = null; }

  const card = mk('div', 'cart');

  // ── Unserviceable banner ──────────────────────────────────────────────────
  if (ev.is_serviceable === false) {
    const unserv = mk('div', '');
    unserv.style.cssText =
      'padding:8px 13px;background:#fef2f2;border-bottom:2px solid #fca5a5;' +
      'font-size:12px;font-weight:600;color:#b91c1c;display:flex;align-items:center;gap:6px';
    unserv.innerHTML = '🚫 Cart is Unserviceable — delivery not available at your address for this cart';
    card.appendChild(unserv);
  }

  // ── Header ────────────────────────────────────────────────────────────────
  const hdr = mk('div', 'cart-hdr');
  const addedCount   = (ev.items || []).filter(i => i.status === 'added').length;
  const totalCount   = (ev.items || []).length;
  const failedItems  = (ev.items || []).filter(i => i.status === 'failed');
  const subItems     = (ev.items || []).filter(i => i.alt);
  const qtyWarnItems = (ev.items || []).filter(i => i.qty_note);
  const requestedCount = (typeof ev.requested === 'number') ? ev.requested : totalCount;
  let hdrText = '🛒 Cart — ' + pname(ev.platform) + '  ·  ' + addedCount + '/' + requestedCount + ' added';
  if (ev.grand_total) hdrText += '  ·  ' + ev.grand_total;
  hdr.innerHTML = hdrText;
  card.appendChild(hdr);

  // ── Delivery time badge ───────────────────────────────────────────────────
  if (ev.delivery_time) {
    const dtBadge = mk('div', '');
    dtBadge.style.cssText =
      'padding:5px 13px;background:#f0fdf4;border-bottom:1px solid #bbf7d0;' +
      'font-size:12px;color:#166534;font-weight:600';
    dtBadge.innerHTML = '🚀 Estimated delivery: <strong>' + ev.delivery_time + '</strong>';
    card.appendChild(dtBadge);
  }

  // ── Item rows ─────────────────────────────────────────────────────────────
  (ev.items || []).forEach(it => {
    const ico = ({ added: '✅', skipped: '⏭', failed: '❌' })[it.status] || '❓';
    let tags = '';
    if (it.alt)              tags += '<span class="tag tag-sub">⚡ Substituted</span>';
    if (it.qty_note)         tags += '<span class="tag tag-warn">⚠ Qty mismatch</span>';
    if (it.from_prev)        tags += '<span class="tag tag-prev">↩ Was in cart</span>';
    if (it.status==='failed') tags += '<span class="tag tag-na">✗ Unavailable</span>';

    const row = mk('div', 'cart-row ' + (it.status || ''));

    const qtyRequested = it.qty_requested ? 'Requested: ' + it.qty_requested : '';
    const qtyOrdered   = it.qty           ? 'Ordered: '   + it.qty           : '';
    const qtyDiff      = (qtyRequested && qtyOrdered && it.qty_note)
      ? '<div style="font-size:11px;color:#b45309;margin-top:2px">' +
          qtyRequested + ' → ' + qtyOrdered + '</div>' : '';

    const savingsHtml = it.savings
      ? '<div style="font-size:11px;color:var(--green-d)">💚 Saved ' + it.savings + '</div>' : '';
    const mrpHtml = it.mrp
      ? '<div style="font-size:11px;color:var(--gray-500)">MRP <s>' + it.mrp + '</s></div>' : '';
    const unitPriceHtml = it.unit_price
      ? '<div style="font-size:11px;color:var(--gray-500)">Unit: ' + it.unit_price + '</div>' : '';
    const reasonHtml  = it.alt_reason
      ? '<div style="font-size:11px;color:var(--orange);margin-top:2px">↳ ' + it.alt_reason + '</div>' : '';
    const qtyNoteHtml = it.qty_note
      ? '<div style="font-size:11px;color:#b91c1c;margin-top:2px">⚠ ' + it.qty_note + '</div>' : '';

    row.innerHTML =
      '<span class="cart-ico">' + ico + '</span>' +
      '<div class="cart-info">' +
        '<div class="cart-name">'    + it.name + '</div>' +
        '<div class="cart-product">' + (it.product || '') + (it.qty ? ' · ' + it.qty : '') + '</div>' +
        tags + qtyDiff + qtyNoteHtml + reasonHtml + savingsHtml + mrpHtml + unitPriceHtml +
      '</div>' +
      '<div class="cart-right">' +
        '<div class="cart-price">' + (it.price || (it.status === 'failed' ? '—' : '')) + '</div>' +
      '</div>';
    card.appendChild(row);
  });

  // ── Bill breakdown ────────────────────────────────────────────────────────
  // FIX #2: old check — const hasBill = ev.grand_total || ev.subtotal || ...
  // If vision didn't extract bill fields they arrive as empty strings (falsy),
  // so hasBill was false and the entire section was silently skipped even when
  // items were successfully added.
  // New rule: always show the bill section when there is at least one added item,
  // and use "—" as fallback text for any missing field so the user can see the
  // section exists and knows which fields the agent couldn't read.
  if (addedCount > 0) {
    const bill = mk('div', 'bill');

    const billRows = [
      ['Items subtotal', ev.subtotal    || '—'],
      ['Delivery',       ev.delivery    || 'FREE'],
      ['Handling fee',   ev.handling    || '—'],
      ['Platform fee',   ev.platform_fee|| '—'],
    ];
    if (ev.late_night_fee)
      billRows.push(['🌙 Late night fee', ev.late_night_fee]);
    if (ev.surge_charge)
      billRows.push(['⚡ Surge charge',   ev.surge_charge]);
    billRows.forEach(function(pair) {
      bill.innerHTML +=
        '<div class="bill-r"><span>' + pair[0] + '</span><span>' + pair[1] + '</span></div>';
    });
    if (ev.savings)
      bill.innerHTML +=
        '<div class="bill-r sav"><span>💚 Total savings</span><span>' + ev.savings + '</span></div>';

    // Grand total row — fall back to estimated_total, then a placeholder.
    const grandTotal = ev.grand_total || ev.estimated_total || '(check cart)';
    bill.innerHTML +=
      '<div class="bill-r tot"><span>Grand Total</span><span>' + grandTotal + '</span></div>';

    card.appendChild(bill);
  }

  // ── Run footer ───────────────────────────────────────────────────────────
  const footer = mk('div', '');
  footer.style.cssText =
    'padding:7px 13px;background:var(--gray-50);border-top:1px solid var(--gray-200);' +
    'font-size:11px;color:var(--gray-600);display:flex;gap:10px;flex-wrap:wrap';
  const footerBits = [];
  if (typeof ev.items_this_session === 'number') footerBits.push('This run: ' + ev.items_this_session);
  if (typeof ev.skipped === 'number' && ev.skipped > 0) footerBits.push('Skipped: ' + ev.skipped);
  if (typeof ev.failed === 'number' && ev.failed > 0) footerBits.push('Failed: ' + ev.failed);
  if (typeof ev.duration === 'number') footerBits.push('Duration: ' + ev.duration.toFixed(1) + 's');
  if (footerBits.length) {
    footer.innerHTML = footerBits.join(' · ');
    card.appendChild(footer);
  }

  // ── Alerts (unavailable + substitutions + qty warnings) ───────────────────
  const alerts = [];
  if (failedItems.length)
    alerts.push('⚠ ' + failedItems.length + ' item(s) could not be added: ' +
      failedItems.map(i => i.name).join(', '));
  if (subItems.length)
    alerts.push('⚡ Substituted: ' + subItems.map(i =>
      i.name + (i.alt_reason ? ' (' + i.alt_reason + ')' : '')).join('; '));
  if (qtyWarnItems.length)
    alerts.push('📦 Quantity notes: ' +
      qtyWarnItems.map(i => i.name + ' — ' + i.qty_note).join('; '));
  if (alerts.length) {
    const alertBox = mk('div', '');
    alertBox.style.cssText =
      'padding:8px 13px;background:#fffbeb;border-top:1px solid #fde68a;' +
      'font-size:11.5px;color:#92400e;line-height:1.7';
    alertBox.innerHTML = alerts.map(a => '<div>' + a + '</div>').join('');
    card.appendChild(alertBox);
  }

  // ── Scroll fix ─────────────────────────────────────────────────────────────
  // FIX #1: old code — card.scrollIntoView({ block: 'start' })
  // That pinned the card HEADER to the top of the chat panel, pushing every
  // item row and the bill section below the visible area.
  //
  // New logic:
  //   • If the whole card fits in the viewport → scroll so the entire card
  //     is visible (header at top, bill at bottom, no clipping).
  //   • If the card is taller than the viewport → scroll so the BILL section
  //     (bottom of card) is visible. The user can scroll up to read items.
  //     This prioritises the grand total which is what most users look for first.
  msgs().appendChild(card);
  requestAnimationFrame(function() {
    var m        = msgs();
    var cardTop  = card.offsetTop;
    var cardH    = card.offsetHeight;
    var viewH    = m.clientHeight;

    if (cardH <= viewH - 20) {
      // Whole card fits — show it all with a small top margin
      m.scrollTo({ top: cardTop - 8, behavior: 'smooth' });
    } else {
      // Card taller than viewport — scroll to show the bill (bottom of card)
      // so the grand total is immediately visible; items are scrollable above
      m.scrollTo({ top: cardTop + cardH - viewH + 16, behavior: 'smooth' });
    }
  });
}

// ─── Screenshot ────────────────────────────────────────────────────────────
function showShot(b64, label) {
  const img = document.getElementById('browserView');
  img.src = 'data:image/png;base64,' + b64;
  img.classList.remove('hidden');
  document.getElementById('bph').classList.add('hidden');
  document.getElementById('blabel').innerHTML =
    '<div class="blabel-dot"></div>' + (label || 'Live Browser');
}

// ─── Input ─────────────────────────────────────────────────────────────────
function sendMsg() {
  const box  = document.getElementById('ibox');
  const text = box.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  addBubble('user', text);   // optimistic render — don't wait for SSE echo
  ws.send(JSON.stringify({ type: 'message', text: text }));
  box.value = '';
  box.style.height = 'auto';
  scrollEnd();
}

function sendText(text) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  addBubble('user', text);
  ws.send(JSON.stringify({ type: 'message', text: text }));
}

function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}
function resize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 90) + 'px';
}

// ─── Status / Sub ──────────────────────────────────────────────────────────
function setStatus(cls, txt) {
  document.getElementById('dot').className = 'dot ' + cls;
  document.getElementById('statusTxt').textContent = txt;
}
function updateSub(state) {
  var MAP = {
    idle:          'Tell me what you want to cook or buy',
    pref_review:   'Review your saved preferences',
    clarifying:    'Clarifying your request\u2026',
    confirming:    'Confirm your shopping list',
    executing:     'Choose your platform',
    shopping:      'Shopping in progress\u2026',
    login_waiting: 'Log in to the browser on the left \u2192',
    login_phone:   'Enter your phone number \u2192',
    login_otp:     'Enter the OTP you received \u2192',
    done:          'All done! \uD83C\uDF89',
    error:         'Something went wrong',
  };
  var sub = document.getElementById('chatSub');
  if (MAP[state]) sub.textContent = MAP[state];
}

// ─── Session control ───────────────────────────────────────────────────────
function doRestart() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'restart' }));
  document.getElementById('msgs').innerHTML = '';
  trackerItems = {}; trackerEl = null; platProgressEl = null;
  executing = false; platformSelectorShown = false;
  renderedCartSummaries = {};
  renderedComparison    = false;
  setStatus('on', 'Ready');
  setInputHint('normal', '');
  showGreeting();
}

function doAddMore() {
  // Tell server to reset to IDLE (keeps cart context) then focus input
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'add_more' }));
  chatState = 'idle';
  platformSelectorShown = false;
  // Reset dedup guards so the next run's cart summary always renders
  renderedCartSummaries = {};
  renderedComparison    = false;
  setInputHint('normal', '');
  document.getElementById('ibox').focus();
}

function doCancel() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'cancel' }));
}

// ─── API Key handling (clean, no override pattern) ─────────────────────────
function doSubmitKey() {
  var key = document.getElementById('apiKeyInput').value.trim();
  if (key) {
    localStorage.setItem('cwm_key', key);
    waitForWS(function() { ws.send(JSON.stringify({ type: 'key', key: key })); });
  }
  document.getElementById('keyModal').classList.add('hidden');
  showGreeting();
}

function doSkipKey() {
  localStorage.removeItem('cwm_key');
  document.getElementById('keyModal').classList.add('hidden');
  showGreeting();
}

function waitForWS(fn, tries) {
  tries = tries || 0;
  if (ws && ws.readyState === WebSocket.OPEN) { fn(); return; }
  if (tries < 30) setTimeout(function() { waitForWS(fn, tries + 1); }, 300);
}

// ─── Greeting ──────────────────────────────────────────────────────────────
function showGreeting() {
  addBubble('assistant',
    "Hi! I'm your **CookWithMe** assistant \uD83C\uDF73\n\n" +
    "Tell me what you'd like to cook or buy and I'll:\n" +
    "\u2022 Expand recipes into a full ingredient list\n" +
    "\u2022 Find the best products & prices on Blinkit & Zepto\n" +
    "\u2022 Add everything to your cart automatically\n\n" +
    '**Try:** "Make butter chicken for 4" or "Buy 2 dozen eggs and 1L milk"'
  );
  // If no platform is connected yet, prompt the user to set up their accounts
  var anyConnected = Object.values(connectedPlatforms).some(function(v) { return v; });
  if (!anyConnected) {
    setTimeout(function() {
      addBubble('assistant',
        "\u26a0\ufe0f **One thing first** \u2014 I need to be signed in to your grocery accounts.\n\n" +
        "Click **\uD83D\uDD17 Accounts** in the top bar to connect **Blinkit** or **Zepto** \u2014 " +
        "I'll open the app in the browser on the left so you can log in normally."
      );
    }, 600);
  }
}

// ─── Helpers ───────────────────────────────────────────────────────────────
function pname(id) {
  return ({ blinkit: 'Blinkit', zepto: 'Zepto' })[id] || id;
}

// ─── Init ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', function() {
  connectWS();
  connectSSE();

  // Fetch platform status on load (server also pushes it on WS connect)
  fetch('/platform-status?sid=' + SID)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      Object.keys(data).forEach(function(p) {
        onPlatformStatus(p, data[p].connected);
      });
    })
    .catch(function() {});

  var saved = localStorage.getItem('cwm_key');
  if (saved) {
    document.getElementById('keyModal').classList.add('hidden');
    showGreeting();
    waitForWS(function() { ws.send(JSON.stringify({ type: 'key', key: saved })); });
  }
  // else: key modal stays visible

  // Escape key closes the accounts modal
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeAcctModal();
  });
});
