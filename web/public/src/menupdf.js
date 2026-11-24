// PDF menus, read in the browser with pdf.js (loaded only when needed). A
// port of the menu browser prototype's PDF reader (menus.py _pdf_page_text):
// menus are often laid out in columns, so each page is read column by
// column, prices sitting apart on the right are joined back onto their dish,
// and text noticeably larger than the rest is marked '## ' as a heading.

const PDFJS = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs';
const PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs';
const MAX_PAGES = 20;
const PRICE_ONLY_LINE = /^\$?\s?\d{1,3}(?:[.,]\d{1,2})?\s*(?:\(?[a-z]{1,4}\)?|pp)?$/i;

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
      line.text += (b.x0 - line.x1 > line.size * 0.15 ? ' ' : '') + b.text;
      line.x1 = Math.max(line.x1, b.x1);
      line.size = Math.max(line.size, b.size);
    } else {
      lines.push({ ...b });
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

  // A column made mostly of bare prices belongs to the text on its left.
  const cols = [...new Set(lines.map((l) => l.col))].sort((a, b) => a - b);
  for (const c of cols) {
    const members = lines.filter((l) => l.col === c);
    if (c && members.filter((l) => PRICE_ONLY_LINE.test(l.text)).length > 0.6 * members.length) {
      for (const l of members) {
        const left = lines.filter((o) => o.col < c && Math.abs(o.y - l.y) < 4 && !o.gone && o.x1 <= l.x0 + 5);
        if (left.length) {
          left.reduce((a, b) => (b.x1 > a.x1 ? b : a)).text += ` ${l.text}`;
          l.gone = true;
        }
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
