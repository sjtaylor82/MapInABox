// Choosing one item from a list, without sliders.
//
// These used to be <input type="range"> "adjustable" sliders. Sliders are
// meant for numbers on a scale; for a list of choices iPhone VoiceOver read
// the changes as edited text (higher or lower voice, half words) and lost its
// place when the page changed. Two plain controls replace them:
//
//   - Radio buttons (the default), for a short set of choices such as a
//     setting: "Large, radio button, 2 of 3". Arrow keys move on a computer.
//   - Previous and Next buttons around the current item ({ browse: true }),
//     for stepping through results (journey options, legs, flights, dates,
//     places, search results). Focus stays on the button you press, and the
//     new item is read out.
//
// The original range input stays in the page, hidden, as the anchor the rest
// of the app talks to: its value, hidden attribute, focus() and Enter key
// still work, so callers did not need to change.

/**
 * Turn <input type="range"> into a picker.
 *   items(): [{ value, label }]
 *   get(): the chosen value
 *   set(value): called when the choice changes
 *   shown: the element that used to show the chosen label (now unused)
 *   browse: true for Previous / Next instead of radio buttons
 * Returns { refresh(), sync(), index(), item() }.
 */
let pickerCount = 0;

export function makePicker(input, { items, get, set, shown, browse = false }) {
  let list = [];
  let idx = 0;
  const n = ++pickerCount;
  const label = document.querySelector(`label[for="${input.id}"]`);
  if (label && !label.id) label.id = `${input.id}-label`;
  const name = label ? label.textContent.trim() : '';

  input.hidden = true;
  input.tabIndex = -1;
  if (shown) shown.hidden = true;

  const box = document.createElement('div');
  box.className = browse ? 'browse' : 'radios-list';
  input.after(box);

  // "Bus. About 2 hours. 3 of 5." -> radio buttons say the position themselves.
  const plain = (text) => String(text).replace(/[,.]?\s*\d+ of \d+\.?\s*$/, '').trim();

  let current = null;
  if (browse) {
    const prev = document.createElement('button');
    prev.type = 'button';
    prev.textContent = 'Previous';
    prev.setAttribute('aria-label', `Previous ${name.toLowerCase()}`.trim());
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Next';
    next.setAttribute('aria-label', `Next ${name.toLowerCase()}`.trim());
    current = document.createElement('p');
    current.className = 'browse-current';
    current.tabIndex = -1;
    current.setAttribute('aria-live', 'polite');
    box.append(current, prev, next);
    const move = (d) => {
      if (!list.length) return;
      const i = Math.max(0, Math.min(list.length - 1, idx + d));
      if (i === idx) {
        current.textContent = '';
        current.textContent = d > 0 ? `Last one. ${list[idx].label}` : `First one. ${list[idx].label}`;
        return;
      }
      choose(i);
    };
    prev.addEventListener('click', () => move(-1));
    next.addEventListener('click', () => move(1));
  } else {
    box.setAttribute('role', 'radiogroup');
    if (label) box.setAttribute('aria-labelledby', label.id);
  }

  function show() {
    if (browse) {
      current.textContent = list[idx] ? list[idx].label : '';
      return;
    }
    for (const r of box.querySelectorAll('input')) r.checked = Number(r.value) === idx;
  }

  function choose(i) {
    idx = i;
    input.value = String(i);
    show();
    if (list[i]) set(list[i].value);
  }

  function refresh() {
    list = items();
    idx = Math.max(0, list.findIndex((it) => it.value === get()));
    input.min = '0';
    input.max = String(Math.max(0, list.length - 1));
    input.value = String(idx);
    if (!browse) {
      box.replaceChildren(...list.map((it, i) => {
        const l = document.createElement('label');
        l.className = 'radio';
        const r = document.createElement('input');
        r.type = 'radio';
        r.name = `picker-${n}`;
        r.value = String(i);
        r.addEventListener('change', () => { if (r.checked) choose(i); });
        l.append(r, ` ${plain(it.label)}`);
        return l;
      }));
    }
    show();
  }

  // Callers focus the old input: send focus to the chosen radio, or to the current item.
  input.focus = () => {
    if (browse) current.focus();
    else (box.querySelector('input:checked') || box.querySelector('input'))?.focus();
  };
  // Enter on a choice works as Enter on the old input did (for example, Go there).
  box.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.target.tagName === 'BUTTON') return;
    e.preventDefault();
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  });
  // Callers hide the old input when there's nothing to choose: hide the new controls with it.
  const setHidden = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'hidden');
  Object.defineProperty(input, 'hidden', {
    get: () => box.hidden,
    set: (v) => { box.hidden = !!v; setHidden.set.call(input, true); },
    configurable: true,
  });
  box.hidden = false;

  refresh();
  return {
    refresh,
    // Show the current choice without rebuilding the list (focus stays put).
    sync: () => {
      idx = Math.max(0, list.findIndex((it) => it.value === get()));
      input.value = String(idx);
      show();
    },
    index: () => idx,
    item: () => list[idx],
  };
}
