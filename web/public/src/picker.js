// Adjustable pickers.
//
// A choice of several options as a slider, so VoiceOver and TalkBack treat
// it as "adjustable": swipe up or down on it and the next choice is spoken
// straight away, with no pop-up to open and close. On a computer the arrow
// keys do the same. A native range input is used because iPhone VoiceOver
// adjusts those reliably, where custom ARIA sliders often do nothing.

/**
 * Turn <input type="range"> into a picker.
 *   items(): [{ value, label }]
 *   get(): the chosen value
 *   set(value): called when the choice changes
 *   shown: optional element that shows the chosen label on screen
 * Returns { refresh(), index(), item() }.
 */
export function makePicker(input, { items, get, set, shown }) {
  let list = [];

  function show(i) {
    const label = list[i] ? list[i].label : '';
    // VoiceOver reads the value, then where it is in the list ("Large, 2 of 3"),
    // unless the label already says so.
    const spoken = !label || /\d+ of \d+\.?$/.test(label) ? label : `${label}, ${i + 1} of ${list.length}`;
    input.setAttribute('aria-valuetext', spoken);
    if (shown) shown.textContent = label;
  }

  function refresh() {
    list = items();
    input.min = '0';
    input.max = String(Math.max(0, list.length - 1));
    input.step = '1';
    const i = Math.max(0, list.findIndex((it) => it.value === get()));
    input.value = String(i);
    input.disabled = list.length === 0;
    show(i);
  }

  input.addEventListener('input', () => {
    const i = Number(input.value);
    show(i);
    if (list[i]) set(list[i].value);
  });

  refresh();
  return {
    refresh,
    index: () => Number(input.value),
    item: () => list[Number(input.value)],
  };
}
