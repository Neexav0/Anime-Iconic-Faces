/* Medora client helpers: read-aloud and the ingredient map. No data leaves the page. */
(() => {
  // ---------------------------------------------------------------- read aloud
  const synth = 'speechSynthesis' in window ? window.speechSynthesis : null;
  if (synth) synth.getVoices(); // voices load asynchronously in Chrome

  function pickVoice(lang) {
    const vs = synth.getVoices();
    const want = lang === 'hi' ? 'hi' : 'en';
    return vs.find(v => v.lang.toLowerCase().replace('_', '-') === want + '-in')
        || vs.find(v => v.lang.toLowerCase().startsWith(want)) || null;
  }
  function resetButtons() {
    document.querySelectorAll('[data-speak].on').forEach(b => {
      b.classList.remove('on');
      const l = b.querySelector('.lbl'); if (l) l.textContent = b.dataset.label;
    });
  }
  function speak(text, btn) {
    if (!synth) { alert('Read aloud is not available in this browser.'); return; }
    const wasOn = btn && btn.classList.contains('on');
    synth.cancel(); resetButtons();
    if (wasOn) return;
    const lang = document.documentElement.lang;
    const v = pickVoice(lang);
    if (lang === 'hi' && !v) {
      alert('इस ब्राउज़र में हिंदी आवाज़ उपलब्ध नहीं है। कृपया Chrome या Edge में खोलें।\nNo Hindi voice in this browser. Please open Medora in Chrome or Edge.');
      return;
    }
    const u = new SpeechSynthesisUtterance(text.replace(/\s+/g, ' ').trim());
    u.lang = lang === 'hi' ? 'hi-IN' : 'en-IN';
    if (v) u.voice = v;
    u.rate = 0.88;
    if (btn) {
      btn.classList.add('on');
      const l = btn.querySelector('.lbl'); if (l) l.textContent = btn.dataset.stop;
      u.onend = u.onerror = resetButtons;
    }
    synth.speak(u);
  }
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-speak]');
    if (!b) return;
    const src = b.dataset.speak;
    const text = src.startsWith('#') ? [...document.querySelectorAll(src)].map(n => n.innerText).join('. ') : src;
    speak(text, b);
  });
  window.addEventListener('pagehide', () => synth && synth.cancel());

  // ---------------------------------------------------------------- ingredient map
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

  document.querySelectorAll('[data-map]').forEach(host => {
    const data = JSON.parse(document.getElementById(host.dataset.map).textContent);
    if (!data.meds.length) return;
    const W = 480, BW = 176, BH = 42, ROW = 56, PAD = 14;
    const ings = [...data.ings].sort((a, b) => (b.shared - a.shared) || a.label.localeCompare(b.label));
    const rows = Math.max(data.meds.length, ings.length);
    const H = rows * ROW + PAD * 2 - (ROW - BH);
    const yOf = (i, n) => PAD + (rows - n) * ROW / 2 + i * ROW;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': host.dataset.label || 'Ingredient map' }, host);
    const linkG = el('g', {}, svg), nodeG = el('g', {}, svg);
    const medPos = {}, ingPos = {};
    data.meds.forEach((m, i) => { medPos[m.id] = yOf(i, data.meds.length) + BH / 2; });
    ings.forEach((g, i) => { ingPos[g.key] = yOf(i, ings.length) + BH / 2; });
    const shared = new Set(ings.filter(g => g.shared).map(g => g.key));

    const links = data.links.map(([mid, key], i) => {
      const y1 = medPos[mid], y2 = ingPos[key], x1 = BW + 2, x2 = W - BW - 2, mx = (x1 + x2) / 2;
      const p = el('path', { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`,
                            class: 'm-link' + (shared.has(key) ? ' shared' : '') }, linkG);
      p.dataset.med = mid; p.dataset.ing = key;
      p.style.animationDelay = (i * 0.07) + 's';
      return p;
    });
    links.forEach(p => p.style.setProperty('--len', Math.ceil(p.getTotalLength()) + 1));

    const node = (x, y, label, cls, attrs) => {
      const g = el('g', { class: 'm-node ' + cls, tabindex: 0, ...attrs }, nodeG);
      el('rect', { x, y: y - BH / 2, width: BW, height: BH, rx: 14 }, g);
      const t = el('text', { x: x + 14, y: y + 5 }, g);
      t.textContent = trunc(label, 17);
      const title = el('title', {}, g); title.textContent = label;
      return g;
    };
    const medNodes = data.meds.map(m => node(0, medPos[m.id], m.name, 'm-med', { 'data-med': m.id }));
    const ingNodes = ings.map(g => node(W - BW, ingPos[g.key], (g.shared ? '⚠ ' : '') + g.label, 'm-ing' + (g.shared ? ' shared' : ''), { 'data-ing': g.key }));

    const focus = (pred) => {
      links.forEach(p => p.classList.toggle('dim', !pred(p)));
      const keepMed = new Set(links.filter(pred).map(p => p.dataset.med));
      const keepIng = new Set(links.filter(pred).map(p => p.dataset.ing));
      medNodes.forEach(n => n.classList.toggle('dim', !keepMed.has(n.dataset.med)));
      ingNodes.forEach(n => n.classList.toggle('dim', !keepIng.has(n.dataset.ing)));
    };
    const clear = () => svg.querySelectorAll('.dim').forEach(n => n.classList.remove('dim'));
    [...medNodes, ...ingNodes].forEach(n => {
      const on = () => n.dataset.med ? focus(p => p.dataset.med === n.dataset.med) : focus(p => p.dataset.ing === n.dataset.ing);
      n.addEventListener('mouseenter', on); n.addEventListener('focus', on);
      n.addEventListener('mouseleave', clear); n.addEventListener('blur', clear);
      n.addEventListener('click', on);
    });
  });
})();

/* Catalogue search on the Add medicine page: picking a brand starts "Type it in" pre-filled. */
(() => {
  const q = document.getElementById('q'), box = document.getElementById('q-results');
  if (!q || !box) return;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const hi = document.documentElement.lang === 'hi';
  let timer, seq = 0;
  const run = async () => {
    const v = q.value.trim();
    if (v.length < 2) { box.hidden = true; return; }
    const mine = ++seq;
    const res = await fetch('/api/brands?q=' + encodeURIComponent(v)).then(r => r.json()).catch(() => []);
    if (mine !== seq) return;
    box.innerHTML = res.length ? res.map(b => `<a href="/manual?q=${encodeURIComponent(b.name)}" role="option">
        <b>${esc(b.name)}</b><small>${b.ingredients.map(i => esc(i.display + ' ' + i.strength)).join(' + ')}</small>
        ${b.uses ? `<small>${esc(b.uses)}</small>` : ''}</a>`).join('')
      : `<div class="empty">${hi ? 'कैटलॉग में नहीं मिला। आप संरचना खुद लिख सकते हैं।' : 'Not in the catalogue. You can enter the composition yourself.'}</div>`;
    box.hidden = false;
  };
  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 180); });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { box.hidden = true; }
    if (e.key === 'ArrowDown') { const a = box.querySelector('a'); if (a) { e.preventDefault(); a.focus(); } }
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.search')) box.hidden = true; });
})();

/* Show / hide password. */
document.addEventListener('click', (e) => {
  const b = e.target.closest('.pw-toggle');
  if (!b) return;
  const input = b.parentElement.querySelector('input');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  b.textContent = show ? b.dataset.hide : b.dataset.show;
  b.setAttribute('aria-pressed', show ? 'true' : 'false');
});
