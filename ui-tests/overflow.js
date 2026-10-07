// Находит текст, который выходит за границы своей кнопки, плашки, карточки или ячейки (или обрезается без многоточия).
module.exports = async function findOverflow(page) {
  return page.evaluate(() => {
    const out = [];
    const isBox = (el) => {
      if (el === document.body || el === document.documentElement) return false;
      const cs = getComputedStyle(el);
      const border = ["Top", "Right", "Bottom", "Left"].some((s) => parseFloat(cs["border" + s + "Width"]) > 0 && cs["border" + s + "Style"] !== "none");
      const bg = cs.backgroundColor !== "rgba(0, 0, 0, 0)" && cs.backgroundColor !== "transparent";
      return border || bg;
    };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!n.nodeValue.trim()) continue;
      const p = n.parentElement;
      if (!p || ["SCRIPT", "STYLE", "OPTION", "NOSCRIPT"].includes(p.tagName)) continue;
      const cs = getComputedStyle(p);
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      const range = document.createRange(); range.selectNodeContents(n);
      const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
      if (!rects.length) continue;
      const left = Math.min(...rects.map((r) => r.left)), right = Math.max(...rects.map((r) => r.right));
      let box = p; while (box && !isBox(box) && box.parentElement) box = box.parentElement;
      // элемент, у которого содержимое прокручивается или обрезается намеренно, пропускаем
      let scroller = p; let skip = false;
      while (scroller && scroller !== document.body) { const o = getComputedStyle(scroller); if (/(auto|scroll)/.test(o.overflowX)) { skip = true; break; } scroller = scroller.parentElement; }
      if (skip) continue;
      // намеренное обрезание с многоточием не считается ошибкой
      let clipper = p; let clipped = false;
      while (clipper && clipper !== document.body) { const o = getComputedStyle(clipper); if (/(hidden|clip)/.test(o.overflowX) && o.textOverflow === "ellipsis") { clipped = true; break; } clipper = clipper.parentElement; }
      if (clipped) continue;
      const vw = document.documentElement.clientWidth;
      if (right > vw + 1 || left < -1) { out.push({ kind: "page-edge", text: n.nodeValue.trim().slice(0, 40), tag: p.tagName + "." + p.className }); continue; }
      if (box && isBox(box)) {
        const b = box.getBoundingClientRect();
        const bcs = getComputedStyle(box);
        if (right > b.right + 1 || left < b.left - 1) out.push({ kind: "outside-box", text: n.nodeValue.trim().slice(0, 40), tag: p.tagName + "." + p.className, box: box.tagName + "." + box.className, over: Math.round(right - b.right) });
      }
      if (p.scrollWidth > p.clientWidth + 1 && /hidden|clip/.test(cs.overflowX) && cs.textOverflow !== "ellipsis" && p.clientWidth > 0) out.push({ kind: "clipped", text: n.nodeValue.trim().slice(0, 40), tag: p.tagName + "." + p.className });
    }
    return out;
  });
};
