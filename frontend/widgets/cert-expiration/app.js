const hostnameInput = document.getElementById('hostname');
const portInput = document.getElementById('port');
const checkBtn = document.getElementById('check-btn');
const errorBox = document.getElementById('error');
const resultBox = document.getElementById('result');

function setError(message) {
  if (!message) {
    errorBox.classList.remove('show');
    errorBox.textContent = '';
    return;
  }
  errorBox.textContent = message;
  errorBox.classList.add('show');
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function fmtDate(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value || 'N/A';
  return parsed.toLocaleString();
}

function statusBadge(status, days) {
  let css = 'ok';
  let label = 'OK';
  if (status === 'expired') {
    css = 'crit';
    label = 'Expired';
  } else if (status === 'expiring_soon') {
    css = 'warn';
    label = 'Expiring Soon';
  }
  return `<span class="badge ${css}">${label}${typeof days === 'number' ? ` (${days}d)` : ''}</span>`;
}

checkBtn.addEventListener('click', async () => {
  setError('');
  resultBox.classList.remove('show');

  const hostname = hostnameInput.value.trim();
  const port = Number(portInput.value || 443);
  if (!hostname) {
    setError('Please enter a hostname.');
    return;
  }

  checkBtn.disabled = true;
  checkBtn.textContent = 'Checking...';
  try {
    const response = await fetch('/api/widgets/cert-expiration/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({hostname, port}),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || 'Certificate check failed');
    }

    setText('subject', data.subject_cn || 'N/A');
    setText('issuer', data.issuer_cn || 'N/A');
    setText('valid-from', fmtDate(data.valid_from));
    setText('valid-until', fmtDate(data.valid_until));
    setText('days', String(data.days_remaining));
    document.getElementById('status').innerHTML = statusBadge(data.status, data.days_remaining);
    resultBox.classList.add('show');
  } catch (error) {
    setError(error.message || 'Certificate check failed.');
  } finally {
    checkBtn.disabled = false;
    checkBtn.textContent = 'Check Certificate';
  }
});
