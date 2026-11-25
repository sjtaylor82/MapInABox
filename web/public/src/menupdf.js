// PDF menus, read in the browser with pdf.js (loaded only when needed). A
// port of the menu browser prototype's PDF reader (menus.py _pdf_page_text):
// menus are often laid out in columns, so each page is read column by
// column, prices sitting apart on the right are joined back onto their dish,
// and text noticeably larger than the rest is marked '## ' as a heading.

const PDFJS = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs';
const PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs';
const MAX_PAGES = 20;
// One price or several side by side ("$14.9 $59": glass and bottle).
const PRICE_ONLY_LINE = /^(?:\$?\s?\d{1,3}(?:[.,]\d{1,2})?\s*(?:\(?[a-z]{1,4}\)?|pp)?\s*)+$/i;
// Headings over a column of prices: "Glass  Bottle", "Small  Large", "150ml".
const PRICE_HEADING = /^(?:glass|gls|bottle|btl|bot|carafe|half|half bottle|jug|pot|pint|schooner|middy|can|small|sml|medium|med|large|lge|regular|reg|single|double|cup|mug|entree|entrée|main|serve|each|\d{2,4}\s?ml|\d(?:\.\d)?\s?l(?:itre)?)$/i;

let lib = null;
async function pdfjs() {
  if (!lib) {
    lib = await import(PDFJS);
    lib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
  }
  return lib;
}

/** Text pieces into lines: pieces side by side on one baseline, close together, are one line. */
function pageLines(items, height) {
  const bits = items.filter((it) => it.str && it.str.trim()).map((it) => {
    const t = it.transform;
    const size = Math.hypot(t[2], t[3]) || it.height || 10;
    return { x0: t[4], x1: t[4] + (it.width || 0), y: height - t[5] - size, size, text: it.str };
  }).sort((a, b) => a.y - b.y || a.x0 - b.x0);
  const lines = [];
  const seen = new Set();
  for (const b of bits) {
    const line = lines.find((l) => Math.abs(l.y - b.y) < l.size * 0.4 && b.x0 >= l.x1 - 1 && b.x0 - l.x1 < l.size * 1.2);
    if (line) {
      const apart = b.x0 - line.x1 > line.size * 0.15;
      line.text += (apart ? ' ' : '') + b.text;
      // The pieces of a line that stand apart ("$14.9" and "$59"), each with where it is.
      if (apart) line.parts.push({ x0: b.x0, x1: b.x1, text: b.text });
      else Object.assign(line.parts[line.parts.length - 1], { x1: b.x1, text: line.parts[line.parts.length - 1].text + b.text });
      line.x1 = Math.max(line.x1, b.x1);
      line.size = Math.max(line.size, b.size);
    } else {
      lines.push({ ...b, parts: [{ x0: b.x0, x1: b.x1, text: b.text }] });
    }
  }
  return lines.map((l) => ({ ...l, text: l.text.replace(/\s+/g, ' ').trim() })).filter((l) => {
    const key = `${l.text}|${Math.round(l.x0 / 3)}|${Math.round(l.y / 3)}`; // bold text is often drawn twice
    if (!l.text || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function pageText(lines, width) {
  if (!lines.length) return '';
  // Columns: a big jump in where lines start begins a new column.
  const gap = Math.max(40, width * 0.05);
  const starts = [...new Set(lines.map((l) => Math.round(l.x0)))].sort((a, b) => a - b);
  const edges = [starts[0], ...starts.filter((s, i) => i && s - starts[i - 1] > gap)];
  for (const l of lines) l.col = edges.reduce((c, e, i) => (Math.round(l.x0) >= e ? i : c), 0);

  // A "column" whose every line starts inside the width of text to its left isn't one:
  // a heading centred over the text, or a note beside a dish ("[organic]"). A note on
  // the same line joins that line; anything else goes in with the text it sits over.
  const sizes = new Map();
  for (const l of lines) sizes.set(Math.round(l.size), (sizes.get(Math.round(l.size)) || 0) + 1);
  const usual = [...sizes].reduce((a, b) => (b[1] > a[1] ? b : a))[0];
  for (const c of [...new Set(lines.map((l) => l.col))].sort((a, b) => a - b)) {
    const members = lines.filter((l) => l.col === c && !PRICE_ONLY_LINE.test(l.text));
    const over = (l) => lines.filter((o) => o.col < c && o.x0 < l.x0 && l.x0 < o.x1);
    if (!c || !members.length || !members.every((l) => over(l).length)) continue;
    for (const l of members) {
      // A note joins the line it is just after; a heading never does.
      const beside = l.size >= usual * 1.3 ? [] : lines.filter((o) => o.col < c && !o.gone
        && Math.abs(o.y - l.y) < 4 && o.x1 <= l.x0 + 5 && l.x0 - o.x1 < 3 * l.size);
      if (beside.length) {
        beside.reduce((a, b) => (b.x1 > a.x1 ? b : a)).text += ` ${l.text}`;
        l.gone = true;
      } else {
        l.col = Math.max(...over(l).map((o) => o.col));
      }
    }
  }
  lines = lines.filter((l) => !l.gone);

  // A column made mostly of bare prices belongs to the text on its left.
  // Headings over it ("Glass  Bottle") name each price: "glass $14.9, bottle $59".
  const isPriceHeading = (l) => l.parts.every((p) => PRICE_HEADING.test(p.text.trim()));
  const cols = [...new Set(lines.map((l) => l.col))].sort((a, b) => a - b);
  for (const c of cols) {
    const members = lines.filter((l) => l.col === c).sort((a, b) => a.y - b.y);
    const prices = members.filter((l) => PRICE_ONLY_LINE.test(l.text)).length;
    const headings = members.filter(isPriceHeading).length;
    if (c && prices && prices + headings > 0.6 * members.length) {
      let heads = [];
      let headsY = -Infinity;
      for (const l of members) {
        if (isPriceHeading(l)) {
          // "Glass" and "Bottle" far apart are two lines at one height: together they are the headings.
          const these = l.parts.map((p) => ({ mid: (p.x0 + p.x1) / 2, name: p.text.trim().toLowerCase() }));
          heads = Math.abs(l.y - headsY) < 4 ? heads.concat(these) : these;
          headsY = l.y;
          l.gone = true;
          continue;
        }
        if (!PRICE_ONLY_LINE.test(l.text)) continue;
        const left = lines.filter((o) => o.col < c && Math.abs(o.y - l.y) < 4 && !o.gone && o.x1 <= l.x0 + 5);
        if (!left.length) continue;
        // Each price under its heading, when it is clearly under one.
        const named = l.parts.map((p) => {
          const mid = (p.x0 + p.x1) / 2;
          const h = heads.length ? heads.reduce((a, b) => (Math.abs(b.mid - mid) < Math.abs(a.mid - mid) ? b : a)) : null;
          return h && Math.abs(h.mid - mid) < 30 ? `${h.name} ${p.text.trim()}` : p.text.trim();
        });
        // Named prices are listed: "House White, glass $12, bottle $42". Others join with a
        // space, as they always did: "VEGGIE STICKS 5.5", "½doz 31.50 1 doz 48.50".
        const plain = named.every((t, i) => t === l.parts[i].text.trim());
        left.reduce((a, b) => (b.x1 > a.x1 ? b : a)).text += plain ? ` ${l.text}` : `, ${named.join(', ')}`;
        l.gone = true;
      }
    }
  }
  lines = lines.filter((l) => !l.gone);

  // Headings: text larger than the usual size, used by only a few lines.
  const bySize = new Map();
  for (const l of lines) {
    const s = Math.round(l.size * 10) / 10;
    bySize.set(s, (bySize.get(s) || 0) + 1);
  }
  const normal = [...bySize].reduce((a, b) => (b[1] > a[1] ? b : a))[0];
  const isHeading = (l) => {
    const s = Math.round(l.size * 10) / 10;
    return s >= normal * 1.015 && bySize.get(s) <= 0.2 * lines.length && l.text.length < 40
      && !/\d{1,3}(?:\.\d{1,2})?\s*$/.test(l.text);
  };

  const out = [];
  for (const c of [...new Set(lines.map((l) => l.col))].sort((a, b) => a - b)) {
    const col = lines.filter((l) => l.col === c).sort((a, b) => a.y - b.y);
    const merged = [];
    for (const l of col) {
      const prev = merged[merged.length - 1];
      // A dish wrapped onto the next line.
      if (prev && !prev.heading && !isHeading(l) && l.y - prev.y < 1.8 * l.size && !/\d\s*$/.test(prev.text)
        && (/[,&/+]$|\b(w|with|and|or|of)$/.test(prev.text) || /^[a-z(]/.test(l.text))) {
        prev.text += ` ${l.text}`;
        prev.y = l.y;
        continue;
      }
      merged.push({ text: l.text, y: l.y, heading: isHeading(l) });
    }
    for (const l of merged) out.push((l.heading ? '## ' : '') + l.text);
    out.push('');
  }
  return out.join('\n').trim();
}

/** The text of a PDF (an ArrayBuffer), page by page; '' when it is only pictures. */
export async function pdfText(bytes) {
  const { getDocument } = await pdfjs();
  const doc = await getDocument({ data: new Uint8Array(bytes), isEvalSupported: false }).promise;
  const pages = [];
  try {
    for (let i = 1; i <= Math.min(doc.numPages, MAX_PAGES); i++) {
      const page = await doc.getPage(i);
      const view = page.getViewport({ scale: 1 });
      const content = await page.getTextContent();
      const text = pageText(pageLines(content.items, view.height), view.width);
      if (text) pages.push(`--- Page ${i} ---\n${text}`);
    }
  } finally {
    doc.destroy();
  }
  return pages.join('\n\n');
}
