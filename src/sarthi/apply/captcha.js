// Ported from OpenApply (MIT), src/apply/captcha.ts @ db7796e. Detection only: Sarthi never solves or evades
// a CAPTCHA or bot check; an interactive challenge is handed to the user.
() => {
  const kinds = new Set();
  let interactive = false, invisible = false;
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 30 && r.height > 30 && el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  };
  const rules = [
    ["recaptcha", /recaptcha\/(api2|enterprise)\/(anchor|bframe)|google\.com\/recaptcha|recaptcha\.net\/recaptcha/],
    ["hcaptcha", /hcaptcha\.com/],
    ["turnstile", /challenges\.cloudflare\.com/],
  ];
  for (const f of Array.from(document.querySelectorAll("iframe"))) {
    const src = f.getAttribute("src") || "";
    for (const [kind, re] of rules) {
      if (!re.test(src)) continue;
      if (/[?&]size=invisible/.test(src) || !shown(f)) { invisible = true; kinds.add(`${kind}:invisible`); }
      else { interactive = true; kinds.add(kind); }
    }
  }
  for (const w of Array.from(document.querySelectorAll(".g-recaptcha, .h-captcha, .cf-turnstile"))) {
    if (w.getAttribute("data-size") === "invisible" || !shown(w)) invisible = true;
  }
  if (document.querySelector(".grecaptcha-badge")) { invisible = true; kinds.add("recaptcha:badge"); }
  if (/^(just a moment|attention required)/i.test(document.title) || document.querySelector("#challenge-running, #cf-challenge-running, #challenge-form")) {
    interactive = true; kinds.add("cloudflare");
  }
  return { interactive, invisible, kinds: Array.from(kinds) };
}
