// Full screen for whatever you're doing, with Back at the top.
//
// A workflow (a route's steps, the places nearby, Settings...) is shown on
// its own: everything else on the page is hidden, so the page isn't busy and
// a screen reader only finds what belongs to it. Back (or Escape) goes back a
// step: a workflow can handle Back itself (a list of public transport legs
// back to the options), otherwise the screen closes and focus returns to the
// button that opened it.
//
//   openScreen(el, { from, back, close })
//     el:    the element to show full screen (it stays where it is in the page)
//     from:  where focus goes when it closes (default: what had focus)
//     back:  () => true when the workflow went back a step itself (stay open)
//     close: () => tidy up when it closes (hide its results, and so on)
//   closeScreen(el)   close it without Back (the workflow finished)
//   closeAllScreens() close everything (going to the map)

const stack = [];
const hiddenNow = new Set();

const backButton = document.createElement('button');
backButton.type = 'button';
backButton.className = 'back screen-back';
backButton.textContent = 'Back';
backButton.addEventListener('click', () => screenBack());

/** Is el open as a screen (anywhere in the stack)? */
export const isScreen = (el) => stack.some((s) => s.el === el);

/** The screen on top, or null. */
export const topScreen = () => (stack.length ? stack[stack.length - 1].el : null);

export function openScreen(el, { from = document.activeElement, back = null, close = null } = {}) {
  if (!el) return;
  const i = stack.findIndex((s) => s.el === el);
  if (i >= 0) stack.splice(i, 1);
  stack.push({ el, from, back, close });
  apply();
}

/** Back: the workflow's own step back, else close the top screen and return focus. */
export function screenBack() {
  const top = stack[stack.length - 1];
  if (!top) return false;
  if (top.back && top.back() === true) return true;
  stack.pop();
  if (top.close) top.close();
  apply();
  const to = top.from && top.from.isConnected && top.from.offsetParent !== null ? top.from : null;
  if (to) to.focus();
  return true;
}

export function closeScreen(el) {
  const i = stack.findIndex((s) => s.el === el);
  if (i < 0) return;
  const [s] = stack.splice(i, 1);
  if (s.close) s.close();
  apply();
}

export function closeAllScreens() {
  while (stack.length) {
    const s = stack.pop();
    if (s.close) s.close();
  }
  apply();
}

/** Hide everything but the top screen (and what the page always needs: live regions, dialogs). */
function apply() {
  for (const n of hiddenNow) n.classList.remove('off-screen');
  hiddenNow.clear();
  const top = stack[stack.length - 1];
  document.body.classList.toggle('screen-on', !!top);
  if (!top) {
    backButton.remove();
    return;
  }
  for (let node = top.el; node && node !== document.body; node = node.parentElement) {
    const parent = node.parentElement;
    if (!parent) break;
    for (const sib of parent.children) {
      if (sib === node || sib === backButton || sib.matches('dialog, script, style, [aria-live], .sr-only')) continue;
      if (!sib.classList.contains('off-screen')) {
        sib.classList.add('off-screen');
        hiddenNow.add(sib);
      }
    }
  }
  top.el.hidden = false;
  top.el.prepend(backButton);
  window.scrollTo(0, 0);
}

// Escape is Back on a computer (dialogs close themselves).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !stack.length || document.querySelector('dialog[open]')) return;
  e.preventDefault();
  e.stopPropagation();
  screenBack();
}, true);
