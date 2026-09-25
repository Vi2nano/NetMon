(() => {
  const params = new URLSearchParams(window.location.search);
  if (params.get('embedded') !== '1') return;
  document.body.classList.add('embedded-widget');
})();
