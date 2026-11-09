export {};
type SelectedMessage = { id: string; author: string; timestamp: string; body: string; files: { name: string; url: string }[] };
const selected = new Map<string, SelectedMessage>();
const toolbar = document.querySelector<HTMLElement>('.export-toolbar')!;
const status = toolbar.querySelector<HTMLElement>('[data-export-status]')!;
const escapeHTML = (value: string): string => value.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
const escapeMarkdown = (value: string): string => value.replace(/([\\`*_{}\[\]()<>#+.!|~-])/g, '\\$1');

function refresh(): void {
  toolbar.hidden = selected.size === 0;
  toolbar.querySelector('[data-selection-count]')!.textContent = `${selected.size} selected`;
  document.querySelectorAll<HTMLElement>('.message[data-message-id]').forEach(message => {
    const active = selected.has(message.dataset.messageId!);
    message.classList.toggle('export-selected', active);
    message.setAttribute('aria-label', `${active ? 'Selected message' : 'Message'} from ${message.dataset.author}. Click empty space or press Space to toggle selection.`);
  });
}

function toggleMessage(message: HTMLElement): void {
  const id = message.dataset.messageId!;
  const content = message.querySelector<HTMLElement>('[data-export-content]');
  if (!selected.has(id)) selected.set(id, {
    id, author: message.dataset.author!, timestamp: message.dataset.sentAt!,
    body: content?.dataset.exportBody ?? '',
    files: Array.from(content?.querySelectorAll<HTMLElement>('.attachment') ?? []).map(file => {
      const link = file.querySelector<HTMLAnchorElement>('a.file-link');
      return { name: file.querySelector('strong')?.textContent ?? 'Attachment', url: link?.href ?? '' };
    }),
  });
  else selected.delete(id);
  status.textContent = '';
  refresh();
}

const interactive = 'a, button, input, select, textarea, label, summary, audio, video, [contenteditable], [role="button"]';
document.addEventListener('click', event => {
  if (event.defaultPrevented || event.button !== 0 || event.detail > 1 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
  const target = event.target;
  if (!(target instanceof Element) || target.closest(interactive)) return;
  const message = target.closest<HTMLElement>('.message[data-message-id]');
  if (!message || !window.getSelection()?.isCollapsed) return;
  // Text stays available for normal selection. Only the bubble's free space toggles it.
  const walker = document.createTreeWalker(message, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  while (walker.nextNode()) {
    if (!walker.currentNode.textContent?.trim()) continue;
    range.selectNodeContents(walker.currentNode);
    if (Array.from(range.getClientRects()).some(rect => event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom)) return;
  }
  toggleMessage(message);
});
document.addEventListener('keydown', event => {
  const target = event.target;
  if (event.defaultPrevented || event.repeat || event.key !== ' ' || !(target instanceof HTMLElement) || !target.matches('.message[data-message-id]')) return;
  event.preventDefault();
  toggleMessage(target);
});

toolbar.addEventListener('click', async event => {
  const button = (event.target as Element).closest<HTMLButtonElement>('button');
  if (!button) return;
  if (button.matches('[data-clear-selection]')) { selected.clear(); refresh(); return; }
  const action = button.dataset.export;
  if (!action || !selected.size) return;
  const metadata = toolbar.querySelector<HTMLInputElement>('[data-export-metadata]')!.checked;
  const messages = [...selected.values()].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp) || Number(a.id) - Number(b.id));
  const heading = (message: SelectedMessage): string => `${message.author} · ${message.timestamp}`;
  const markdown = messages.map(message => [
    ...(metadata ? [`**${escapeMarkdown(heading(message))}**`] : []),
    message.body,
    ...message.files.map(file => file.url ? `[${escapeMarkdown(file.name)}](<${file.url}>)` : `${escapeMarkdown(file.name)} (unavailable)`),
  ].filter(Boolean).join('\n\n')).join('\n\n---\n\n');
  const html = messages.map(message => `<section>${metadata ? `<p><strong>${escapeHTML(heading(message))}</strong></p>` : ''}<p>${escapeHTML(message.body).replace(/\n/g, '<br>')}</p>${message.files.map(file => `<p>${file.url ? `<a href="${escapeHTML(file.url)}">${escapeHTML(file.name)}</a>` : `${escapeHTML(file.name)} (unavailable)`}</p>`).join('')}</section>`).join('<hr>');
  const plain = messages.map(message => [...(metadata ? [heading(message)] : []), message.body, ...message.files.map(file => `${file.name}${file.url ? `: ${file.url}` : ' (unavailable)'}`)].filter(Boolean).join('\n\n')).join('\n\n———\n\n');
  if (action === 'download') {
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = 'archived-messages.md'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = 'Markdown downloaded.';
    return;
  }
  try {
    if (action === 'rich') {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([plain], { type: 'text/plain' }),
      })]);
    } else await navigator.clipboard.writeText(markdown);
    status.textContent = 'Copied.';
  } catch {
    status.textContent = 'Clipboard unavailable or denied. Use Download .md to save your selection.';
  }
});
// Navigation and infinite scrolling replace message nodes; selection belongs to the archive IDs.
new MutationObserver(refresh).observe(document.querySelector('#workspace')!, { childList: true, subtree: true });

refresh();
