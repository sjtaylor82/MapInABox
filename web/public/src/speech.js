// Speech output.
//
// Screen reader: text goes into a live region, cleared a second later so it
// isn't found as text when swiping. Most messages are polite: they wait for
// VoiceOver to finish what it's reading (a button's name, say). Moving on the
// map is assertive ({ now: true }), so the newest place is heard at once.
// Each kind has two regions used in turn, so a repeated message speaks again.
// Built-in voice: the browser's speechSynthesis, for people without a
// screen reader (or both at once, for a sighted helper).

import { settings } from './settings.js';

let assertive = [];
let polite = [];
let turn = 0;
let last = '';
const listeners = new Set();

export function initSpeech() {
  assertive = [document.getElementById('live-a'), document.getElementById('live-b')];
  polite = [document.getElementById('live-c'), document.getElementById('live-d')];
}

/** Speak text. { now: true } interrupts whatever VoiceOver is saying. */
export function say(text, { now = false } = {}) {
  text = String(text || '').trim();
  if (!text) return;
  last = text;
  const mode = settings.get('speech');
  if (mode !== 'voice') {
    const regions = now ? assertive : polite;
    const region = regions[turn];
    for (const r of [...assertive, ...polite]) if (r !== region) r.textContent = '';
    region.textContent = text;
    turn = 1 - turn;
    setTimeout(() => { if (region.textContent === text) region.textContent = ''; }, 1000);
  }
  if (mode === 'voice' || mode === 'both') speakVoice(text);
  for (const fn of listeners) fn(text);
}

export function lastSpoken() {
  return last;
}

export function onSay(fn) {
  listeners.add(fn);
}

function speakVoice(text) {
  if (!('speechSynthesis' in window)) return;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = settings.get('voiceRate') || 1;
    const lang = (navigator.language || 'en').slice(0, 2);
    const voice = speechSynthesis.getVoices().find((v) => v.lang.startsWith(lang) && v.localService);
    if (voice) u.voice = voice;
    speechSynthesis.speak(u);
  } catch { /* speech unavailable */ }
}

export function spell(text) {
  return String(text).replace(/[,.]/g, '').split('').map((c) => (c === ' ' ? 'space' : c)).join(' ');
}
