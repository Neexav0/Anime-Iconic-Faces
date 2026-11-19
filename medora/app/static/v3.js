/* Medora v3: hero sizing, Namaste (once per session), pill tilt, header search. */
(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // 1) Hero band wraps the hero exactly (straight edge under the greeting + pills).
  const hb = document.querySelector('.hero-bg'), hero = document.querySelector('.hero');
  const fit = () => { if (hb && hero) hb.style.height = Math.ceil(hero.getBoundingClientRect().bottom + scrollY) + 'px'; };
  if (hero) { fit(); addEventListener('resize', fit); document.fonts && document.fonts.ready.then(fit); }

  // 2) Hand-written Namaste on home: animate the first time per session, then show it still.
  const hello = document.querySelector('.hero .namaste');
  if (hello) {
    let seen = false; try { seen = sessionStorage.getItem('medora-hello') === '1'; } catch (e) {}
    if (seen || reduce) hello.classList.add('static');
    else (document.fonts ? document.fonts.ready : Promise.resolve()).then(() => {
      hello.classList.add('play'); try { sessionStorage.setItem('medora-hello', '1'); } catch (e) {}
    });
  }

  // 3) Pills tilt toward the pointer: they feel like they are popping out of the screen.
  const art = document.querySelector('[data-tilt]');
  if (art && !reduce && matchMedia('(hover: hover)').matches) {
    const zone = art.closest('.hero') || art;
    zone.addEventListener('mousemove', (e) => {
      const r = art.getBoundingClientRect();
      const x = (e.clientX - (r.left + r.width / 2)) / r.width, y = (e.clientY - (r.top + r.height / 2)) / r.height;
      art.style.setProperty('--tx', (x * 14).toFixed(2) + 'deg');
      art.style.setProperty('--ty', (-y * 12).toFixed(2) + 'deg');
    });
    zone.addEventListener('mouseleave', () => { art.style.setProperty('--tx', '0deg'); art.style.setProperty('--ty', '0deg'); });
  }

  // 4) Header search: look up any catalogue brand; picking one starts "Add medicine" pre-filled.
  const q = document.getElementById('q'), box = document.getElementById('q-results');
  if (q && box) {
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
          ${b.uses ? `<small>${esc(b.uses)}</small>` : ''}<span class="add-hint">${hi ? '+ मेरी सूची में जोड़ें' : '+ Add to my list'}</span></a>`).join('')
        : `<div class="empty">${hi ? 'कोई मिलान नहीं। आप इसे खुद भी जोड़ सकते हैं।' : 'No match in the catalogue. You can still add it yourself.'}</div>`;
      box.hidden = false;
    };
    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 180); });
    q.addEventListener('keydown', (e) => { if (e.key === 'Escape') { box.hidden = true; q.blur(); } if (e.key === 'ArrowDown') { const a = box.querySelector('a'); if (a) { e.preventDefault(); a.focus(); } } });
    document.addEventListener('click', (e) => { if (!e.target.closest('.search')) box.hidden = true; });
  }
})();
