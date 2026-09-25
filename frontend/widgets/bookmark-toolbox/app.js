const USER_BOOKMARKS_KEY = 'netmon:bookmarkToolbox:userLinks';
const SCOPE_LABELS = {
  user: 'User',
  organization: 'Organization',
  deployment: 'Deployment',
};
const state = {
  user: [],
  organization: [],
  deployment: [],
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[char]
  ));
}

function makeId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `user-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isValidHttpUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch (_) {
    return false;
  }
}

function normalizeBookmark(bookmark, scope) {
  if (!bookmark || typeof bookmark !== 'object') return null;
  const title = String(bookmark.title || '').trim();
  const url = String(bookmark.url || '').trim();
  const note = String(bookmark.note || '').trim();
  if (!title || !url || !isValidHttpUrl(url)) return null;
  return {
    id: String(bookmark.id || makeId()),
    scope,
    title,
    url,
    note,
  };
}

function readUserBookmarks() {
  try {
    const raw = JSON.parse(localStorage.getItem(USER_BOOKMARKS_KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.map((bookmark) => normalizeBookmark(bookmark, 'user')).filter(Boolean);
  } catch (_) {
    return [];
  }
}

function saveUserBookmarks() {
  localStorage.setItem(USER_BOOKMARKS_KEY, JSON.stringify(state.user));
}

async function api(path, opts = {}) {
  const response = await fetch(path, {
    headers: {'Content-Type': 'application/json'},
    ...opts,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.status === 204 ? null : response.json();
}

function setError(message = '') {
  const errorEl = document.getElementById('bookmark-error');
  errorEl.textContent = message;
  errorEl.classList.toggle('show', Boolean(message));
}

function setStatus(message = '') {
  const statusEl = document.getElementById('bookmark-status');
  statusEl.textContent = message;
  statusEl.classList.toggle('show', Boolean(message));
}

function clearMessages() {
  setError('');
  setStatus('');
}

function renderScope(scope) {
  const container = document.getElementById(`bookmark-list-${scope}`);
  const bookmarks = state[scope] || [];
  if (!bookmarks.length) {
    container.innerHTML = `<div class="bookmark-empty">No ${escapeHtml(SCOPE_LABELS[scope].toLowerCase())} links saved yet.</div>`;
    return;
  }

  container.innerHTML = bookmarks.map((bookmark) => `
    <div class="bookmark-card">
      <div class="bookmark-card-header">
        <div>
          <a class="bookmark-link" href="${escapeHtml(bookmark.url)}" target="_blank" rel="noreferrer noopener">${escapeHtml(bookmark.title)}</a>
          <div class="bookmark-url">${escapeHtml(bookmark.url)}</div>
        </div>
        <button class="secondary bookmark-delete" type="button" data-delete-scope="${escapeHtml(scope)}" data-delete-id="${escapeHtml(bookmark.id)}">Delete</button>
      </div>
      ${bookmark.note ? `<div class="bookmark-note">${escapeHtml(bookmark.note)}</div>` : ''}
    </div>
  `).join('');
}

function renderAll() {
  renderScope('user');
  renderScope('organization');
  renderScope('deployment');
}

async function loadBookmarks() {
  clearMessages();
  state.user = readUserBookmarks();
  renderScope('user');
  try {
    const shared = await api('/api/widgets/bookmark-toolbox/shared');
    state.organization = Array.isArray(shared.organization)
      ? shared.organization.map((bookmark) => normalizeBookmark(bookmark, 'organization')).filter(Boolean)
      : [];
    state.deployment = Array.isArray(shared.deployment)
      ? shared.deployment.map((bookmark) => normalizeBookmark(bookmark, 'deployment')).filter(Boolean)
      : [];
    renderScope('organization');
    renderScope('deployment');
  } catch (error) {
    state.organization = [];
    state.deployment = [];
    renderScope('organization');
    renderScope('deployment');
    setError(`Shared links are unavailable right now: ${error.message}`);
  }
}

async function addBookmark(event) {
  event.preventDefault();
  clearMessages();
  const title = document.getElementById('bookmark-title').value.trim();
  const url = document.getElementById('bookmark-url').value.trim();
  const scope = document.getElementById('bookmark-scope').value;
  const note = document.getElementById('bookmark-note').value.trim();

  if (!title) {
    setError('Link name is required.');
    return;
  }
  if (!isValidHttpUrl(url)) {
    setError('Enter a valid http or https URL.');
    return;
  }

  try {
    if (scope === 'user') {
      state.user.unshift({id: makeId(), scope, title, url, note});
      saveUserBookmarks();
      renderScope('user');
    } else {
      const saved = await api('/api/widgets/bookmark-toolbox/shared', {
        method: 'POST',
        body: JSON.stringify({scope, title, url, note}),
      });
      state[scope].unshift(normalizeBookmark(saved, scope));
      renderScope(scope);
    }
    document.getElementById('bookmark-form').reset();
    document.getElementById('bookmark-scope').value = scope;
    setStatus(`${SCOPE_LABELS[scope]} link saved.`);
  } catch (error) {
    setError(error.message);
  }
}

async function deleteBookmark(scope, id) {
  const label = SCOPE_LABELS[scope] || 'This';
  if (!confirm(`Delete this ${label.toLowerCase()} link?`)) return;
  clearMessages();
  try {
    if (scope === 'user') {
      state.user = state.user.filter((bookmark) => bookmark.id !== id);
      saveUserBookmarks();
    } else {
      await api(`/api/widgets/bookmark-toolbox/shared/${encodeURIComponent(scope)}/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      });
      state[scope] = state[scope].filter((bookmark) => bookmark.id !== id);
    }
    renderScope(scope);
    setStatus(`${label} link removed.`);
  } catch (error) {
    setError(error.message);
  }
}

document.getElementById('bookmark-form').addEventListener('submit', addBookmark);
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-delete-scope][data-delete-id]');
  if (!button) return;
  deleteBookmark(button.dataset.deleteScope, button.dataset.deleteId);
});

renderAll();
loadBookmarks();
