// Public demonstration: no runtime API, account, analytics or external scripts.
const dashboard = document.getElementById('dashboard');
const tabs = [...document.querySelectorAll('[data-view]')];
function selectView(name, focus = false) {
  const selected = tabs.find(tab => tab.dataset.view === name);
  if (!selected) return;
  for (const tab of tabs) {
    const active = tab === selected;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    document.getElementById('view-' + tab.dataset.view).hidden = !active;
  }
  document.getElementById('current-view').textContent = selected.textContent.trim();
  if (focus) selected.focus();
}
for (const tab of tabs) {
  tab.addEventListener('click', () => selectView(tab.dataset.view));
  tab.addEventListener('keydown', event => {
    const index = tabs.indexOf(tab);
    const next = ['ArrowRight', 'ArrowDown'].includes(event.key) ? (index + 1) % tabs.length
      : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? (index + tabs.length - 1) % tabs.length
      : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    selectView(tabs[next].dataset.view, true);
  });
}
document.getElementById('theme-toggle').addEventListener('click', event => {
  const dark = dashboard.classList.toggle('demo-dark');
  event.currentTarget.setAttribute('aria-pressed', String(dark));
  event.currentTarget.textContent = dark ? '切换浅色' : '切换深色';
});
document.getElementById('memory-query').addEventListener('input', event => {
  const query = event.target.value.trim().toLocaleLowerCase();
  let matches = 0;
  for (const card of document.querySelectorAll('#memory-results article')) {
    card.hidden = !(card.dataset.search + card.textContent).toLocaleLowerCase().includes(query);
    if (!card.hidden) matches++;
  }
  document.getElementById('memory-empty').hidden = matches > 0;
});
document.querySelector('[data-copy]').addEventListener('click', async event => {
  const status = document.getElementById('copy-result');
  const text = document.getElementById(event.currentTarget.dataset.copy).textContent;
  try { await navigator.clipboard.writeText(text); status.textContent = '已复制'; }
  catch { status.textContent = '未能复制，请手动选择上方命令。'; }
});

// Reproducible public screenshots retain the example-data labels.
const preview = new URLSearchParams(location.search);
if (preview.get('capture') === '1') document.body.classList.add('capture');
selectView(preview.get('view'));
