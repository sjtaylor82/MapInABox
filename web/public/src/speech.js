// Speech output.
//
// Screen reader: text goes into one of two assertive live regions. Alternating
// between them makes a repeated identical message speak again, and clearing
// the idle one stops a stale message being re-read.
// Built-in voice: the browser's speechSynthesis, for people without a
// screen reader (or both at once, for a sighted helper).

import { settings } from './settings.js';

let regions = [];
let turn = 0;
let last = '';
const listeners = new Set();

export function initSpeech() {
  regions = [document.getElementById('live-a'), document.getElementById('live-b')];
}

/** Speak text, interrupting what was speaking before. */
export function say(text) {
  text = String(text || '').trim();
  if (!text) return;
  last = text;
  const mode = settings.get('speech');
  if (mode !== 'voice') {
    const now = regions[turn];
    const other = regions[1 - turn];
    other.textContent = '';
    now.textContent = text;
    turn = 1 - turn;
    // Empty it once it has been spoken, so VoiceOver doesn't find the old
    // message sitting at the top of the page when you swipe.
    const wait = Math.min(20000, 4000 + text.length * 80);
    setTimeout(() => { if (now.textContent === text) now.textContent = ''; }, wait);
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
