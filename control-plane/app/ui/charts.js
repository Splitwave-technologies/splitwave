// Небольшие SVG/HTML-графики без библиотек. Цвета берутся из статусной палитры (зелёный/красный/серый),
// подсказка на наведение и фокус, легенда для >= 2 серий. Значения — только через textContent.
import { h } from "./dom.js";

const NS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}, ...kids) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, String(v));
  for (const c of kids.flat()) if (c) el.append(c);
  return el;
};
const niceMax = (v) => {
  if (v <= 0) return 1;
  const e = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
};
const roundedTop = (x, y, w, hh, r) => {
  r = Math.max(0, Math.min(r, w / 2, hh));
  return `M${x},${y + hh} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + hh} Z`;
};

/** Столбцы (стопкой). data: [{label, short, values:[..]}], series: [{name, color}] */
export function columnChart({ data, series, height = 170, ariaLabel = "chart" }) {
  const wrap = h("div", { class: "chart" });
  const legend = series.length > 1
    ? h("div", { class: "legend" }, series.map((s) => { const sw = h("i", { class: "sw" }); sw.style.background = s.color; return h("span", {}, sw, s.name); }))
    : null;
  const host = h("div", { class: "chart-host" });
  const tip = h("div", { class: "tip", hidden: true, role: "status" });
  wrap.append(...[legend, host, tip].filter(Boolean));

  function draw() {
    const w = Math.max(host.clientWidth || 320, 160);
    const left = 30, right = 4, top = 14, bottom = 22;
    const plotW = w - left - right, plotH = height - top - bottom, y0 = top + plotH;
    const totals = data.map((d) => d.values.reduce((a, b) => a + b, 0));
    const max = niceMax(Math.max(0, ...totals));
    const band = plotW / Math.max(data.length, 1);
    const barW = Math.min(20, band * 0.62);
    const labelW = Math.max(...data.map((d) => String(d.short).length)) * 6.4 + 10;     // ширина самой длинной подписи: подписи не должны налезать друг на друга
    const skip = band < labelW ? Math.ceil(labelW / band) : 1;
    const ticks = [0, ...(max % 2 === 0 && max > 2 ? [max / 2] : []), max];
    const svg = svgEl("svg", { width: w, height, role: "img", "aria-label": ariaLabel });
    ticks.forEach((t) => {
      const y = top + plotH - (t / max) * plotH;
      svg.append(svgEl("line", { x1: left, x2: w - right, y1: y, y2: y, class: t === 0 ? "ax-base" : "ax-grid", "stroke-width": 1 }));
      const label = svgEl("text", { x: left - 6, y: y + 3, "text-anchor": "end", "font-size": 10, class: "ax-text" });
      label.textContent = String(t);
      svg.append(label);
    });
    data.forEach((d, i) => {
      const cx = left + band * (i + 0.5);
      let yy = y0;
      const nonZero = d.values.map((v, k) => (v > 0 ? k : -1)).filter((k) => k >= 0);
      const topK = nonZero[nonZero.length - 1];
      d.values.forEach((v, k) => {
        if (v <= 0) return;
        const hh = (v / max) * plotH;
        const y = yy - hh;
        const drawH = k === nonZero[0] ? hh : Math.max(hh - 2, 1);   // зазор 2px между сегментами
        yy -= hh;
        svg.append(k === topK
          ? svgEl("path", { d: roundedTop(cx - barW / 2, y, barW, drawH, 3), fill: series[k].color })
          : svgEl("rect", { x: cx - barW / 2, y, width: barW, height: drawH, fill: series[k].color }));
      });
      if (i % skip === 0) {
        const tl = svgEl("text", { x: cx, y: height - 6, "text-anchor": "middle", "font-size": 10, class: "ax-text" });
        tl.textContent = d.short;
        svg.append(tl);
      }
      const hit = svgEl("rect", { x: left + band * i, y: top, width: band, height: plotH + bottom, fill: "transparent", tabindex: 0 });
      const show = (ev) => {
        tip.hidden = false;
        tip.replaceChildren(h("div", { class: "muted" }, d.label),
          ...series.map((s, k) => { const sw = h("i", { class: "sw" }); sw.style.background = s.color; return h("div", {}, sw, h("b", {}, String(d.values[k])), ` ${s.name}`); }));
        const box = wrap.getBoundingClientRect();
        const r = hit.getBoundingClientRect();
        const cxp = ev.clientX !== undefined && ev.clientX !== 0 ? ev.clientX : r.left + r.width / 2;
        tip.style.left = `${Math.min(Math.max(cxp - box.left, 70), box.width - 70)}px`;
        tip.style.top = `${r.top - box.top + 4}px`;
      };
      hit.addEventListener("pointermove", show); hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", () => { tip.hidden = true; }); hit.addEventListener("blur", () => { tip.hidden = true; });
      svg.append(hit);
    });
    host.replaceChildren(svg);
  }
  const ro = new ResizeObserver(() => draw());
  ro.observe(host);
  requestAnimationFrame(draw);
  return wrap;
}

/** Горизонтальные полосы. rows: [{label, href?, segments:[{name,value,color}], suffix}] */
export function hbars({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.segments.reduce((s, x) => s + x.value, 0)));
  return h("div", { class: "hbars" }, rows.map((r) => {
    const label = r.href ? h("a", { href: r.href, title: r.label }, r.label) : h("span", { title: r.label }, r.label);
    const track = h("div", { class: "hbar-track" });
    const segs = r.segments.filter((x) => x.value > 0);
    segs.forEach((x, i) => {
      const seg = h("div", { class: "hbar-seg", title: `${x.name}: ${x.value}`, tabindex: 0 });
      seg.style.width = `${(x.value / max) * 100}%`;
      seg.style.background = x.color;
      if (i === segs.length - 1) seg.style.borderRadius = "0 4px 4px 0";
      else seg.style.marginRight = "2px";
      track.append(seg);
    });
    return h("div", { class: "hbar-row" }, h("div", { class: "hbar-label" }, label), track, h("div", { class: "hbar-suffix mono" }, r.suffix || ""));
  }));
}
