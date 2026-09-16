document.querySelectorAll('[data-refresh]').forEach(button => {
  button.addEventListener('click', () => window.location.reload());
});
