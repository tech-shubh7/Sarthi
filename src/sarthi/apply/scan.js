// Runs in the page: finds every question, tags its control(s) with data-sarthi-key and returns the fields.
(cfg) => {
  const clean = (s) => (s || "").replace(/[*✱]/g, "").replace(/\s+/g, " ").trim();
  const roots = Array.from(document.querySelectorAll(cfg.root));
  const count = (e) => e.querySelectorAll("input, select, textarea").length;
  const root = roots.sort((a, b) => count(b) - count(a))[0] || document.body;
  const out = [];
  const at = [];
  document.querySelectorAll("[data-sarthi-key]").forEach((e) => e.removeAttribute("data-sarthi-key"));
  let n = 0;
  const nextKey = () => `s-${++n}`;
  const skipped = (el) => !!cfg.skip && !!el.closest(cfg.skip);
  const rawLabel = (el) => {
    const c = el.closest(cfg.container);
    const own = c && cfg.labelSel ? c.querySelector(cfg.labelSel) : null;
    if (own && clean(own.textContent)) return own.textContent || "";
    if (el.id) {
      const lf = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lf && clean(lf.textContent)) return lf.textContent || "";
    }
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)?.textContent || "").join(" ");
      if (clean(t)) return t;
    }
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label") || "";
    const wrap = el.closest("label");
    if (wrap && clean(wrap.textContent)) return wrap.textContent || "";
    const any = c?.querySelector("label, legend");
    if (any && clean(any.textContent)) return any.textContent || "";
    return el.getAttribute("placeholder") || el.getAttribute("name") || el.id || "";
  };
  const isRequired = (el, raw) => {
    const c = el.closest(cfg.container);
    return el.required || el.getAttribute("aria-required") === "true" || /[*✱]/.test(raw) ||
      !!(c && cfg.requiredSel && c.matches(cfg.requiredSel)) || !!(c && cfg.requiredSel && c.querySelector(cfg.requiredSel));
  };
  const optionText = (inp) => {
    if (inp.id) {
      const l = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
      if (l && clean(l.textContent)) return clean(l.textContent);
    }
    const w = inp.closest("label");
    if (w && clean(w.textContent)) return clean(w.textContent);
    return clean(inp.value) || clean(inp.getAttribute("name"));
  };
  if (cfg.yesno) {
    for (const g of Array.from(root.querySelectorAll(cfg.yesno))) {
      if (g.hasAttribute("data-sarthi-key")) continue;
      const key = nextKey();
      g.setAttribute("data-sarthi-key", key);
      const raw = rawLabel(g);
      at.push(g);
      out.push({ key, label: clean(raw), type: "radio", required: isRequired(g, raw),
        options: Array.from(g.querySelectorAll("button")).map((b) => clean(b.textContent)) });
    }
  }
  const groups = new Map();
  for (const el of Array.from(root.querySelectorAll("input, select, textarea"))) {
    if (el.hasAttribute("data-sarthi-key") || skipped(el) || (cfg.yesno && el.closest(cfg.yesno))) continue;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "input" && ["hidden", "submit", "button", "reset", "image", "search"].includes(t)) continue;
    if (t === "radio" || t === "checkbox") {
      const g = el.closest(cfg.container) || el.name || el.id;
      groups.set(g, [...(groups.get(g) || []), el]);
      continue;
    }
    if (t !== "file" && (el.getAttribute("aria-hidden") === "true" || !el.getClientRects().length)) continue;
    const raw = rawLabel(el);
    const key = nextKey();
    el.setAttribute("data-sarthi-key", key);
    const f = { key, label: clean(raw), type: "text", required: isRequired(el, raw), options: [] };
    if (tag === "textarea") f.type = "textarea";
    else if (tag === "select") {
      f.type = "select";
      f.options = Array.from(el.options).filter((o) => o.value !== "" && !/^(select|choose|--|please select)/i.test(clean(o.text)))
        .map((o) => clean(o.text));
    } else if (t === "file") f.type = "file";
    else if (el.getAttribute("role") === "combobox") f.type = "combobox";
    else if (el.closest(".react-datepicker__input-container")) f.type = "date";
    else if (["email", "tel", "url", "number", "date"].includes(t)) f.type = t;
    at.push(el);
    out.push(f);
  }
  for (const inputs of groups.values()) {
    const key = nextKey();
    inputs.forEach((i) => i.setAttribute("data-sarthi-key", key));
    const first = inputs[0];
    const c = first.closest(cfg.container);
    const legend = c?.querySelector(cfg.labelSel || "legend") || c?.querySelector("legend");
    const raw = legend && clean(legend.textContent) ? legend.textContent || "" : c || inputs.length > 1 ? rawLabel(first) : optionText(first);
    at.push(first);
    out.push({ key, label: clean(raw) || optionText(first), type: first.type === "radio" ? "radio" : "checkbox",
      required: inputs.some((i) => isRequired(i, raw)), options: inputs.map(optionText) });
  }
  return out.map((f, i) => [f, at[i]])
    .sort((a, b) => (a[1].compareDocumentPosition(b[1]) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
    .map(([f]) => f);
}
