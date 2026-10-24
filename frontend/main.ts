import './style.css';

type Stop = () => void;
const loaders = new Map<HTMLElement, Stop>();
const positions = new Map<string, number>();
let navigation: AbortController | null = null;
let currentUrl = location.href;
let avatarRefresh: number | undefined;
let fetchingAvatars = false;
let fetchingTranscripts = false;

async function refreshTranscripts(): Promise<void> {
  if (fetchingTranscripts || document.hidden) return;
  const nodes = Array.from(document.querySelectorAll<HTMLElement>('[data-transcription-id]:not([data-transcribe-off])'));
  const ids = [...new Set(nodes.filter(node => {
    const bounds = node.getBoundingClientRect();
    return !['complete', 'unavailable'].includes(node.dataset.transcriptionState ?? '') && bounds.height > 0 && bounds.bottom > 0 && bounds.top < innerHeight;
  }).map(node => node.dataset.transcriptionId!))].slice(0, 100);
  if (!ids.length) return;
  fetchingTranscripts = true;
  try {
    const response = await fetch(`/transcriptions?ids=${ids.join(',')}`, { credentials: 'same-origin' });
    if (!response.ok) return;
    const results: Record<string, { body: string; state: string; error: string }> = await response.json();
    nodes.forEach(node => {
      const result = results[node.dataset.transcriptionId!];
      if (!result || typeof result.body !== 'string' || typeof result.state !== 'string' || typeof result.error !== 'string') return;
      node.dataset.transcriptionState = result.state;
      node.querySelector('[data-transcript-status]')!.textContent = result.state;
      const body = node.querySelector<HTMLElement>('[data-transcript-body]')!;
      body.textContent = result.body;
      body.hidden = !result.body;
      node.querySelector('[data-transcript-error]')!.textContent = result.error;
    });
  } catch { /* Cached transcripts remain readable while offline. */ }
  finally { fetchingTranscripts = false; }
}
const photoViewer = document.querySelector<HTMLDialogElement>('.photo-viewer');
let photoLinks: HTMLAnchorElement[] = [];
let photoIndex = 0;

function showPhoto(index: number): void {
  if (!photoViewer || !photoLinks[index]) return;
  photoIndex = index;
  const link = photoLinks[index];
  const image = photoViewer.querySelector('img')!;
  image.src = link.href;
  image.alt = link.querySelector('img')?.alt ?? 'Profile photo';
  photoViewer.querySelector('figcaption')!.textContent = link.textContent?.trim() ?? '';
  photoViewer.querySelector<HTMLButtonElement>('[data-photo-prev]')!.disabled = index === 0;
  photoViewer.querySelector<HTMLButtonElement>('[data-photo-next]')!.disabled = index === photoLinks.length - 1;
  if (!photoViewer.open) photoViewer.showModal();
}

document.addEventListener('click', event => {
  const target = event.target as Element;
  const link = target.closest<HTMLAnchorElement>('a[data-profile-photo], a[data-profile-photo-trigger]');
  if (link && photoViewer && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    photoLinks = Array.from(document.querySelectorAll<HTMLAnchorElement>('a[data-profile-photo]'));
    showPhoto(photoLinks.findIndex(photo => photo.href === link.href));
  } else if (target.closest('[data-photo-close]') || target === photoViewer) photoViewer?.close();
  else if (target.closest('[data-photo-prev]')) showPhoto(photoIndex - 1);
  else if (target.closest('[data-photo-next]')) showPhoto(photoIndex + 1);
});
photoViewer?.addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft') { event.preventDefault(); showPhoto(photoIndex - 1); }
  if (event.key === 'ArrowRight') { event.preventDefault(); showPhoto(photoIndex + 1); }
});

function localizeTimes(scope: ParentNode = document): void {
  const full = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  const day = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
  const today = new Date().toDateString();
  scope.querySelectorAll<HTMLTimeElement>('time[data-local-time]').forEach(node => {
    const date = new Date(node.dateTime);
    if (!Number.isFinite(date.getTime())) return;
    node.textContent = node.dataset.localTime === 'short'
      ? new Intl.DateTimeFormat(undefined, date.toDateString() === today
        ? { hour: '2-digit', minute: '2-digit' } : { day: '2-digit', month: 'short' }).format(date)
      : full.format(date);
    node.title = full.format(date);
    if (node.dataset.capturedAt && node.dataset.sourceAt) {
      node.title = `Sent: ${full.format(date)} · Captured: ${full.format(new Date(node.dataset.capturedAt))} · Source: ${full.format(new Date(node.dataset.sourceAt))}`;
    }
  });
  scope.querySelectorAll<HTMLElement>('[data-browser-zone]').forEach(node => {
    node.textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
  });
  scope.querySelectorAll<HTMLElement>('.message-list').forEach(list => {
    list.querySelectorAll('[data-message-date]').forEach(node => node.remove());
    let previous = '';
    list.querySelectorAll<HTMLElement>('.message[data-sent-at]').forEach(message => {
      const date = new Date(message.dataset.sentAt!);
      if (!Number.isFinite(date.getTime())) return;
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
      if (key !== previous) {
        const divider = document.createElement('div');
        divider.className = 'date-divider';
        divider.dataset.messageDate = key;
        const label = document.createElement('span');
        label.textContent = day.format(date);
        divider.append(label);
        message.before(divider);
        previous = key;
      }
    });
  });
}

function failedAvatar(image: HTMLImageElement): void {
  const parent = image.closest<HTMLElement>('[data-avatar-chat]');
  if (parent) parent.dataset.avatarVersion = '';
  image.remove();
}

document.addEventListener('error', event => {
  if (event.target instanceof HTMLImageElement && event.target.matches('[data-avatar-image]')) failedAvatar(event.target);
}, true);

async function refreshAvatarPhotos(): Promise<void> {
  if (fetchingAvatars || document.hidden) return;
  const visible = Array.from(document.querySelectorAll<HTMLElement>('[data-avatar-chat]')).filter(node => {
    const bounds = node.getBoundingClientRect();
    return bounds.width > 0 && bounds.bottom > 0 && bounds.top < innerHeight;
  });
  const ids = [...new Set(visible.map(node => node.dataset.avatarChat!))].slice(0, 100);
  if (!ids.length) return;
  fetchingAvatars = true;
  try {
    const response = await fetch(`/avatars?ids=${ids.join(',')}`, { credentials: 'same-origin' });
    if (!response.ok) return;
    const versions: Record<string, unknown> = await response.json();
    document.querySelectorAll<HTMLElement>('[data-avatar-chat]').forEach(node => {
      const id = node.dataset.avatarChat!;
      const version = versions[id];
      if (typeof version !== 'string' || version === node.dataset.avatarVersion) return;
      node.dataset.avatarVersion = version;
      node.querySelector('[data-avatar-image]')?.remove();
      if (!version) return;
      const image = document.createElement('img');
      image.alt = '';
      image.loading = 'lazy';
      image.decoding = 'async';
      image.dataset.avatarImage = '';
      image.src = `/avatar/${encodeURIComponent(id)}?v=${encodeURIComponent(version)}`;
      node.append(image);
    });
  } catch { /* Keep initials or cached photos while the server is unreachable. */ }
  finally { fetchingAvatars = false; }
}

function scheduleAvatarRefresh(): void {
  if (avatarRefresh !== undefined) return;
  avatarRefresh = window.setTimeout(() => {
    avatarRefresh = undefined;
    void refreshAvatarPhotos();
  }, 250);
}

function messageTarget(): HTMLElement | null {
  let id = document.querySelector<HTMLElement>('#workspace [data-scroll-target]')?.dataset.scrollTarget;
  if (location.hash) {
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return null; }
  }
  const target = id ? document.getElementById(id) : null;
  const selected = target?.querySelector<HTMLElement>('.selected-version');
  return selected?.closest<HTMLDetailsElement>('.message-history')?.open ? selected : target;
}

function revealTarget(): void {
  const target = messageTarget();
  // pushState does not consistently update the CSS :target pseudo-class.
  target?.classList.add('context-target');
  target?.scrollIntoView({ block: 'center' });
  target?.closest('[data-scroll-region]')?.dispatchEvent(new Event('scroll'));
}

function initializeScroll(scope: ParentNode = document): void {
  scope.querySelectorAll<HTMLElement>('[data-scroll-region]').forEach(region => {
    if (loaders.has(region)) return;
    const list = region.querySelector<HTMLElement>('[data-scroll-list]');
    const next = region.querySelector<HTMLAnchorElement>('[data-scroll-next]');
    const status = region.querySelector<HTMLElement>('[data-scroll-status]');
    if (!list) return;
    const older = region.dataset.scrollDirection === 'older';
    const controller = new AbortController();
    let busy = false;
    let failed = false;
    let pinned = false;
    let anchor: { node: HTMLElement; top: number } | undefined;
    let observer: IntersectionObserver | undefined;
    let resize: ResizeObserver | undefined;

    function remember(): void {
      if (!older) return;
      pinned = region.scrollHeight - region.clientHeight - region.scrollTop < 4;
      const bounds = region.getBoundingClientRect();
      const selected = list!.querySelector<HTMLElement>('.selected-version');
      const selectedBounds = selected?.getBoundingClientRect();
      const node = selected && selectedBounds && selectedBounds.bottom > bounds.top && selectedBounds.top < bounds.bottom
        ? selected : Array.from(list!.querySelectorAll<HTMLElement>('[data-scroll-key]')).find(node => {
        const item = node.getBoundingClientRect();
        return item.bottom > bounds.top && item.top < bounds.bottom;
      });
      anchor = node ? { node, top: node.getBoundingClientRect().top } : undefined;
    }
    function restore(): void {
      if (!older) return;
      if (pinned) region.scrollTop = region.scrollHeight;
      else if (anchor?.node.isConnected) region.scrollTop += anchor.node.getBoundingClientRect().top - anchor.top;
      remember();
    }
    function change(update: () => void): void {
      remember();
      update();
      restore();
    }
    if (older) {
      const target = messageTarget();
      if (target && region.contains(target)) target.scrollIntoView({ block: 'center' });
      else if (region.dataset.scrollStart === 'latest') region.scrollTop = region.scrollHeight;
      remember();
      region.addEventListener('scroll', remember, { signal: controller.signal, passive: true });
      region.addEventListener('click', event => {
        const summary = (event.target as Element).closest<HTMLElement>('summary');
        if (!summary || !region.contains(summary)) return;
        // Opening history is an explicit reading action. Preserve its control
        // instead of following the growing message to the bottom of the list.
        pinned = false;
        anchor = { node: summary, top: summary.getBoundingClientRect().top };
      }, { signal: controller.signal, capture: true });
      // Keep the latest message in view as media loads, or preserve the reader's
      // visible message when older images and responsive layout change heights.
      if (window.ResizeObserver) {
        resize = new ResizeObserver(restore);
        resize.observe(list);
        resize.observe(region);
      }
    }
    loaders.set(region, () => { controller.abort(); observer?.disconnect(); resize?.disconnect(); });
    if (!next || !status || !window.IntersectionObserver) return;
    const keys = new Set(Array.from(list.querySelectorAll<HTMLElement>('[data-scroll-key]'), node => node.dataset.scrollKey));
    function nearEdge(): boolean {
      const bounds = next!.getBoundingClientRect();
      const viewport = region.getBoundingClientRect();
      return older ? bounds.bottom >= viewport.top - 300 && bounds.top <= viewport.bottom
        : bounds.top <= viewport.bottom + 300 && bounds.bottom >= viewport.top;
    }
    async function load(): Promise<void> {
      if (busy || controller.signal.aborted || !next!.hasAttribute('href')) return;
      busy = true;
      failed = false;
      change(() => {
        next!.setAttribute('aria-disabled', 'true');
        list!.setAttribute('aria-busy', 'true');
        status!.textContent = older ? 'Loading older messages…' : 'Loading more…';
      });
      try {
        const response = await fetch(next!.href, { signal: controller.signal, credentials: 'same-origin' });
        if (!response.ok) throw new Error('Request failed');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const incomingRegion = page.querySelector<HTMLElement>(`[data-scroll-region="${region.dataset.scrollRegion}"]`);
        const incoming = incomingRegion?.querySelector<HTMLElement>('[data-scroll-list]');
        if (!incoming || controller.signal.aborted) throw new Error('Invalid response');
        incoming.querySelectorAll<HTMLElement>('[data-scroll-key]').forEach(node => {
          if (keys.has(node.dataset.scrollKey)) node.remove();
          else keys.add(node.dataset.scrollKey);
        });
        change(() => {
          if (older) {
            list!.prepend(...incoming.childNodes);
          } else list!.append(...incoming.childNodes);
          localizeTimes(region);
          const following = incomingRegion!.querySelector<HTMLAnchorElement>('[data-scroll-next]');
          if (following) {
            next!.href = following.getAttribute('href')!;
            status!.textContent = '';
          } else {
            next!.removeAttribute('href');
            next!.hidden = true;
            status!.textContent = older ? 'Beginning of conversation.' : 'All items loaded.';
            observer!.disconnect();
          }
        });
        updateSelection();
        scheduleAvatarRefresh();
      } catch {
        if (controller.signal.aborted) return;
        failed = true;
        change(() => { status!.textContent = older ? 'Could not load history. Select Load older messages to retry.' : 'Could not load more. Select Load more to retry.'; });
      } finally {
        busy = false;
        next!.removeAttribute('aria-disabled');
        list!.removeAttribute('aria-busy');
      }
      if (!failed && !controller.signal.aborted && next!.hasAttribute('href') && nearEdge()) {
        window.setTimeout(() => void load(), 0);
      }
    }
    observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting) && !failed) void load();
    }, { root: region, rootMargin: '300px' });
    observer.observe(next);
    next.addEventListener('click', event => { event.preventDefault(); void load(); }, { signal: controller.signal });
  });
}

function updateSelection(): void {
  const workspace = document.querySelector<HTMLElement>('#workspace')!;
  document.body.dataset.view = workspace.dataset.view;
  document.body.dataset.chat = workspace.dataset.chat;
  document.querySelectorAll<HTMLElement>('[data-chat-link]').forEach(link => {
    const active = link.dataset.chatLink === workspace.dataset.chat;
    link.classList.toggle('selected', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  const view = workspace.dataset.view;
  document.querySelectorAll<HTMLElement>('[data-section]').forEach(link => {
    const section = link.dataset.section;
    link.classList.toggle('active', section === view || (section === 'chats' && ['home', 'chat', 'message', 'profile'].includes(view ?? '')));
  });
}

async function navigate(url: string, push = true): Promise<void> {
  const destination = new URL(url, location.href);
  if (destination.origin !== location.origin) { location.assign(url); return; }
  navigation?.abort();
  const controller = new AbortController();
  navigation = controller;
  const workspace = document.querySelector<HTMLElement>('#workspace')!;
  const error = document.querySelector<HTMLElement>('.navigation-error')!;
  const scroll = workspace.querySelector<HTMLElement>('[data-scroll-viewport]');
  positions.set(currentUrl, scroll?.scrollTop ?? 0);
  if (workspace.dataset.view === 'profile' && destination.pathname === new URL(currentUrl).pathname) {
    positions.set(destination.href, scroll?.scrollTop ?? 0);
  }
  workspace.setAttribute('aria-busy', 'true');
  error.hidden = true;
  try {
    const response = await fetch(destination.href, { signal: controller.signal, credentials: 'same-origin' });
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const incoming = page.querySelector<HTMLElement>('#workspace');
    // Render validation errors from the search form in their normal application layout.
    if (!incoming || (!response.ok && response.status !== 400)) throw new Error('Navigation failed');
    if (controller.signal.aborted) return;
    for (const [region, stop] of loaders) {
      if (workspace.contains(region)) { stop(); loaders.delete(region); }
    }
    workspace.replaceChildren(...incoming.childNodes);
    workspace.dataset.view = incoming.dataset.view;
    workspace.dataset.chat = incoming.dataset.chat;
    document.title = page.title;
    if (push) history.pushState(null, '', destination.href);
    currentUrl = destination.href;
    localizeTimes(workspace);
    updateSelection();
    initializeScroll(workspace);
    scheduleAvatarRefresh();
    const nextScroll = workspace.querySelector<HTMLElement>('[data-scroll-viewport]');
    if (nextScroll && nextScroll.dataset.scrollDirection !== 'older') nextScroll.scrollTop = positions.get(currentUrl) ?? 0;
    revealTarget();
    workspace.focus({ preventScroll: true });
  } catch {
    if (!controller.signal.aborted) {
      error.textContent = 'Could not open this page. Please try again.';
      error.hidden = false;
      // A failed back/forward request should not leave the URL describing a different view.
      if (!push) history.replaceState(null, '', currentUrl);
    }
  } finally {
    if (navigation === controller) {
      workspace.removeAttribute('aria-busy');
      navigation = null;
    }
  }
}

document.addEventListener('click', event => {
  const link = (event.target as Element).closest<HTMLAnchorElement>('a[data-nav]');
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target || link.hasAttribute('download')) return;
  event.preventDefault();
  void navigate(link.href);
});
document.addEventListener('submit', event => {
  const form = event.target as HTMLFormElement;
  if (!form.matches('[data-nav-form]')) return;
  event.preventDefault();
  const url = new URL(form.action, location.href);
  const params = new URLSearchParams();
  new FormData(form).forEach((value, key) => { if (typeof value === 'string') params.append(key, value); });
  url.search = params.toString();
  void navigate(url.href);
});
window.addEventListener('popstate', () => void navigate(location.href, false));
// Native fragment scrolling happens after deferred scripts and would otherwise
// replace our selected-version position with the top of the whole message.
window.addEventListener('pageshow', () => requestAnimationFrame(() => requestAnimationFrame(revealTarget)));
history.scrollRestoration = 'manual';

const themeButton = document.querySelector<HTMLButtonElement>('[data-theme-toggle]')!;
function setTheme(theme: string): void {
  document.documentElement.dataset.theme = theme;
  themeButton.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
  themeButton.setAttribute('aria-pressed', String(theme === 'dark'));
}
try { setTheme(localStorage.getItem('archive-theme') ?? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')); }
catch { setTheme('light'); }
themeButton.addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  setTheme(theme);
  try { localStorage.setItem('archive-theme', theme); } catch { /* Theme still works without storage. */ }
});
localizeTimes();
initializeScroll();
revealTarget();
document.querySelectorAll<HTMLImageElement>('[data-avatar-image]').forEach(image => {
  if (image.complete && image.naturalWidth === 0) failedAvatar(image);
});
document.addEventListener('scroll', scheduleAvatarRefresh, true);
document.addEventListener('visibilitychange', scheduleAvatarRefresh);
window.setInterval(() => void refreshAvatarPhotos(), 30_000);
window.setInterval(() => void refreshTranscripts(), 10_000);
window.setInterval(() => {
  if (!document.hidden && !navigation && document.querySelector('[data-setup-busy]')) void navigate('/settings', false);
}, 1500);
void refreshAvatarPhotos();
void refreshTranscripts();
