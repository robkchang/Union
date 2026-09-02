(() => {
  const page = document.getElementById('unionPage');
  const unionId = page.dataset.unionId;
  const csrf = document.querySelector('meta[name=csrf-token]').content;
  const api = (path, body) => fetch(`/unions/${unionId}/api/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || j.error || r.statusText); return j; });

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let state = JSON.parse($('initialState').textContent);
  let revealed = false;

  function rel(iso, nowIso) {
    if (!iso) return '—';
    const t = Date.parse(iso.endsWith('Z') || iso.includes('+') ? iso : iso.replace(' ', 'T') + 'Z');
    const now = nowIso ? Date.parse(nowIso) : Date.now();
    const s = Math.max(0, Math.round((now - t) / 1000));
    if (s < 5) return 'just now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function renderKey() {
    const k = state.union.join_key;
    $('joinKey').dataset.key = k;
    $('joinKey').textContent = revealed ? k : 'UNJ-••••••••••••••••••••';
    $('revealBtn').textContent = revealed ? 'Hide' : 'Reveal';
    $('keyMeta').textContent = `Generation ${state.union.join_key_gen}` +
      (state.union.key_cycled_at ? ` · cycled ${rel(state.union.key_cycled_at, state.now)}` : ' · never cycled');
  }

  function render() {
    $('unionName').textContent = state.union.name;
    document.title = `${state.union.name} — Union`;
    const online = state.nodes.filter(n => n.status !== 'offline').length;
    $('summary').textContent = `${online} online · ${state.nodes.length} member${state.nodes.length === 1 ? '' : 's'}`;
    renderKey();

    const tbody = $('nodesTable').querySelector('tbody');
    tbody.innerHTML = state.nodes.map(n => `
      <tr>
        <td><strong>${esc(n.name)}</strong>${n.cwd ? `<br><span class="muted">${esc(n.cwd)}</span>` : ''}</td>
        <td>${esc(n.machine)}</td>
        <td>${esc(n.harness)}</td>
        <td><span class="pill pill--${esc(n.mode)}">${esc(n.mode)}</span></td>
        <td><span class="dot dot--${esc(n.status)}"></span>${esc(n.status)}</td>
        <td class="dim" title="${esc(n.last_seen_at || '')}">${rel(n.last_seen_at, state.now)}</td>
        <td class="dim" title="${esc(n.joined_at)}">${esc((n.joined_at || '').slice(0, 10))} <span class="muted">(key gen ${n.joined_key_gen})</span></td>
        <td class="dim">${n.messages_sent} ↑ ${n.messages_recv} ↓</td>
        <td><code>${esc(n.fingerprint)}</code>${n.key_rotations ? `<br><span class="muted" title="${esc(n.rotated_at || '')}">keys rotated ${n.key_rotations}×</span>` : ''}</td>
        <td><button class="btn btn--danger btn--sm" data-evict="${esc(n.id)}" data-name="${esc(n.name)}">Evict</button></td>
      </tr>`).join('');
    $('nodesEmpty').hidden = state.nodes.length > 0;
    $('nodesTable').hidden = state.nodes.length === 0;

    const labels = { created: 'Union created', joined: 'joined', evicted: 'evicted', left: 'left', key_cycled: 'Join key cycled',
                     renamed: 'Union renamed', deleted: 'Union deleted' };
    $('activity').innerHTML = state.activity.map(a => {
      const who = a.node_name ? `<strong>${esc(a.node_name)}</strong> ${labels[a.kind] || esc(a.kind)}` : (labels[a.kind] || esc(a.kind));
      const by = a.actor && !a.node_name ? '' : (a.actor ? ` by ${esc(a.actor)}` : '');
      return `<li><span>${who}${by}</span><time title="${esc(a.at)}">${rel(a.at, state.now)}</time></li>`;
    }).join('') || '<li class="muted">Nothing yet.</li>';
  }

  async function refresh() {
    try { state = await api('state'); render(); } catch (e) { /* keep last state */ }
  }

  $('revealBtn').addEventListener('click', () => { revealed = !revealed; renderKey(); });
  $('copyBtn').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(state.union.join_key); $('copyBtn').textContent = 'Copied'; setTimeout(() => $('copyBtn').textContent = 'Copy', 1200); }
    catch { alert(state.union.join_key); }
  });
  $('cycleBtn').addEventListener('click', async () => {
    if (!confirm('Cycle the join key? The old key stops working for new joins. Existing members are unaffected.')) return;
    try { const r = await api('cycle-key', {}); state.union.join_key = r.join_key; revealed = true; await refresh(); } catch (e) { alert(e.message); }
  });
  $('renameBtn').addEventListener('click', async () => {
    const name = prompt('New name', state.union.name);
    if (!name || name.trim() === state.union.name) return;
    try { await api('rename', { name: name.trim() }); await refresh(); } catch (e) { alert(e.message); }
  });
  $('deleteBtn').addEventListener('click', async () => {
    if (!confirm(`Delete "${state.union.name}" and evict all ${state.nodes.length} node(s)?`)) return;
    try { await api('delete', {}); location.href = '/unions'; } catch (e) { alert(e.message); }
  });
  $('nodesTable').addEventListener('click', async ev => {
    const btn = ev.target.closest('[data-evict]');
    if (!btn) return;
    if (!confirm(`Evict ${btn.dataset.name}? Its key pair cannot rejoin.`)) return;
    try { await api(`nodes/${btn.dataset.evict}/evict`, {}); await refresh(); } catch (e) { alert(e.message); }
  });

  render();
  setInterval(refresh, 3000);
})();
