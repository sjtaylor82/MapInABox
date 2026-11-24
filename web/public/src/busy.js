// "Working" state for slow searches (stations, routes, menus...), so it's
// clear the app is busy and nothing on the page looks ready when it isn't.
//
//   const done = working(button, 'Finding stations near you', { slow: true });
//   ... await the search ...
//   done();
//
// While working: the button is marked busy (aria-disabled) and ignores
// presses; a line straight after it shows "Finding stations near you…" and
// how long it may take; and every 10 seconds a short "Still finding stations
// near you" is spoken, so a long wait doesn't sound like a hang.

import { say } from './speech.js';
import { workingTick } from './audio.js';

const REMIND_MS = 10000;
const TICK_MS = 2000;

/** Is this button in the middle of its search? */
export const isWorking = (button) => !!button && button.getAttribute('aria-disabled') === 'true';

/**
 * Start working. message: "Finding stations near you" (no full stop).
 * slow: add "This can take up to half a minute." speak: say it now (false
 * when the caller already says something). Returns done().
 */
export function working(button, message, { slow = false, speak = true } = {}) {
  const text = `${message}…${slow ? ' This can take up to half a minute.' : ''}`;
  let line = null;
  if (button) {
    button.setAttribute('aria-disabled', 'true');
    button.classList.add('is-working');
    line = document.createElement('p');
    line.className = 'working-status';
    line.textContent = text;
    button.after(line);
  }
  if (speak) say(text);
  const still = `Still ${message.charAt(0).toLowerCase()}${message.slice(1)}.`;
  const timer = setInterval(() => say(still), REMIND_MS);
  // A short tick every 2 seconds while working.
  const ticks = setInterval(workingTick, TICK_MS);
  let finished = false;
  return function done() {
    if (finished) return;
    finished = true;
    clearInterval(timer);
    clearInterval(ticks);
    if (button) {
      button.removeAttribute('aria-disabled');
      button.classList.remove('is-working');
    }
    line?.remove();
  };
}

/**
 * Run a slow action from a button: marked busy while it runs, and a second
 * press while it's busy does nothing.
 */
export function onSlowClick(button, message, action, opts = {}) {
  button.addEventListener('click', async () => {
    if (isWorking(button)) return;
    const done = working(button, message, { speak: false, ...opts });
    try {
      await action();
    } finally {
      done();
    }
  });
}
