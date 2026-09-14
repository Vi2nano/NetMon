// ============================================================
// Import/Export UI for NetMon Settings
// ============================================================

// Export configuration as JSON file
async function handleExport() {
  try {
    const btn = document.getElementById('export-config-btn');
    btn.disabled = true;
    btn.textContent = 'Exporting...';
    
    const config = await api('/api/export/config');
    
    // Generate timestamp for filename
    const now = new Date();
    const timestamp = now.toISOString().split('T')[0];
    const filename = `netmon-backup-${timestamp}.json`;
    
    // Create blob and download
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    toast(`Configuration exported as ${filename}`, 'up');
  } catch (e) {
    toast(`Export failed: ${e.message}`, 'crit');
  } finally {
    const btn = document.getElementById('export-config-btn');
    btn.disabled = false;
    btn.textContent = '📥 Download backup';
  }
}

// Import configuration from JSON file
function handleImportClick() {
  const input = document.getElementById('import-config-file');
  input.click();
}

async function handleImportFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  
  try {
    const content = await file.text();
    const config = JSON.parse(content);
    
    // Ask for mode
    const mode = confirm(
      'Merge configuration (OK) or replace all config (Cancel)?\n\n' +
      'Merge: Add new items, skip existing by IP\n' +
      'Replace: Delete all config and reimport fresh'
    ) ? 'merge' : 'replace';
    
    const btn = document.getElementById('import-config-btn');
    btn.disabled = true;
    btn.textContent = 'Importing...';
    
    const result = await api('/api/import/config', {
      method: 'POST',
      body: JSON.stringify({ config, mode })
    });
    
    const stats = result.stats;
    let msg = `Import complete: ${stats.groups_created} groups, ${stats.devices_created} devices, ${stats.ports_created} ports, ${stats.rules_created} rules`;
    
    if (stats.errors.length > 0) {
      msg += `\n\nErrors: ${stats.errors.slice(0, 3).join('; ')}`;
      if (stats.errors.length > 3) msg += ` ...and ${stats.errors.length - 3} more`;
    }
    
    toast(msg, stats.errors.length > 0 ? 'warn' : 'up');
    
    // Reload page to reflect changes
    setTimeout(() => location.reload(), 1000);
  } catch (e) {
    toast(`Import failed: ${e.message}`, 'crit');
  } finally {
    const btn = document.getElementById('import-config-btn');
    btn.disabled = false;
    btn.textContent = '📤 Upload backup';
    event.target.value = '';  // Reset file input
  }
}

// Initialize import/export UI
function initImportExportUI() {
  const exportBtn = document.getElementById('export-config-btn');
  const importBtn = document.getElementById('import-config-btn');
  const importFile = document.getElementById('import-config-file');
  
  if (exportBtn) exportBtn.addEventListener('click', handleExport);
  if (importBtn) importBtn.addEventListener('click', handleImportClick);
  if (importFile) importFile.addEventListener('change', handleImportFile);
}

// Call on page load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initImportExportUI);
} else {
  initImportExportUI();
}
