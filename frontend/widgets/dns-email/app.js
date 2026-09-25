const targetInput = document.getElementById('target');
const lookupBtn = document.getElementById('lookup-btn');
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

function setText(id, value) {
  document.getElementById(id).textContent = value || 'Unavailable';
}

function renderLinks(links) {
  const container = document.getElementById('links');
  container.innerHTML = '';
  const entries = [
    ['WHOIS', links.whois],
    ['BGP HE', links.bgp],
    ['Geo Lookup', links.geo],
    ['Reverse DNS', links.reverse_dns],
    ['Blacklist Lookup', links.blacklist],
  ];
  for (const [label, href] of entries) {
    const a = document.createElement('a');
    a.href = href;
    a.textContent = label;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    container.appendChild(a);
  }
}

function renderBlacklist(rows) {
  if (!rows || rows.length === 0) return 'No blacklist data returned';
  return rows.map((row) => `${row.zone}: ${row.reason}`).join(' | ');
}

lookupBtn.addEventListener('click', async () => {
  setError('');
  resultBox.classList.remove('show');

  const target = targetInput.value.trim();
  if (!target) {
    setError('Please enter a hostname or public IP address.');
    return;
  }

  lookupBtn.disabled = true;
  lookupBtn.textContent = 'Looking up...';
  try {
    const response = await fetch('/api/widgets/dns-email/lookup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target}),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || 'Lookup failed');
    }

    const geo = data.geolocation;
    const geoText = geo.error
      ? geo.error
      : [geo.city, geo.region, geo.country].filter(Boolean).join(', ') +
        (geo.lat != null && geo.lon != null ? ` (${geo.lat}, ${geo.lon})` : '');

    setText('target-out', `${data.target} (${data.ip})`);
    setText('isp-out', data.isp || data.org || 'Unavailable');
    setText('asn-out', data.asn || 'Unavailable');
    setText('geo-out', geoText || 'Unavailable');
    setText('ptr-out', data.ptr || 'No PTR record found');
    setText('blacklist-out', renderBlacklist(data.blacklists));
    renderLinks(data.links || {});

    resultBox.classList.add('show');
  } catch (error) {
    setError(error.message || 'Lookup failed.');
  } finally {
    lookupBtn.disabled = false;
    lookupBtn.textContent = 'Run Lookup';
  }
});
