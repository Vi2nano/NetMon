const notesInput = document.getElementById('notes');
const generateBtn = document.getElementById('generate-btn');
const result = document.getElementById('result');
const paragraph = document.getElementById('paragraph');
const errorBox = document.getElementById('error');

function setError(message) {
  if (!message) {
    errorBox.classList.remove('show');
    errorBox.textContent = '';
    return;
  }
  errorBox.textContent = message;
  errorBox.classList.add('show');
}

async function parseApiResponse(response) {
  const raw = await response.text();
  try {
    return raw ? JSON.parse(raw) : {};
  } catch (_) {
    return {detail: raw || 'Unexpected response from server'};
  }
}

generateBtn.addEventListener('click', async () => {
  setError('');
  result.classList.remove('show');

  const notes = notesInput.value.trim();
  if (!notes) {
    setError('Please enter at least one technical action.');
    return;
  }

  generateBtn.disabled = true;
  generateBtn.textContent = 'Generating...';
  try {
    const response = await fetch('/api/widgets/ticket-closure/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({notes}),
    });
    const data = await parseApiResponse(response);
    if (!response.ok) {
      throw new Error(data.detail || 'Failed to generate closure text');
    }

    paragraph.textContent = data.paragraph;
    result.classList.add('show');
  } catch (error) {
    setError(error.message || 'Failed to generate closure text.');
  } finally {
    generateBtn.disabled = false;
    generateBtn.textContent = 'Generate Closure';
  }
});
