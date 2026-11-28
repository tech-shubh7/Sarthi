// Runs in the page (the extension's own isolated world): fills the fields scan.js tagged with
// data-sarthi-key. A field that already has a value is left as it is. Nothing is ever submitted.

(() => {
  const clean = (s) => (s || "").replace(/[*✱]/g, "").replace(/\s+/g, " ").trim();
  const norm = (s) => clean(s).toLowerCase();
  const YES = /^(yes|y|true|1|checked|agree|i agree)$/i;
  const SECRET = /pass(word|code)|one[- ]time|otp|captcha|card number|cvv/i;

  const optionText = (input) => {
    if (input.id) {
      const label = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
      if (label && clean(label.textContent)) return clean(label.textContent);
    }
    const wrap = input.closest("label");
    if (wrap && clean(wrap.textContent)) return clean(wrap.textContent);
    return clean(input.value) || clean(input.getAttribute("name"));
  };

  // React, Vue and Angular forms keep their own copy of the value: the native setter plus input/change
  // events is what makes them see it (as if typed).
  const setValue = (el, value) => {
    const proto =
      el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : el.tagName === "SELECT" ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    el.focus();
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
  };

  const mark = (el) => {
    el.style.outline = "2px solid #20B486";
    el.style.outlineOffset = "1px";
  };

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const attach = (el, file) => {
    const bytes = Uint8Array.from(atob(file.data), (c) => c.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], file.name, { type: "application/pdf" }));
    el.files = transfer.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };

  async function pickFromList(el, value) {
    // A searchable dropdown (role=combobox): type the answer, then choose the option that reads the same.
    setValue(el, value);
    el.focus();
    for (let i = 0; i < 6; i += 1) {
      await wait(250);
      const options = Array.from(document.querySelectorAll('[role="option"]')).filter((o) => o.getClientRects().length);
      const hit = options.find((o) => norm(o.textContent) === norm(value)) ||
        (options.length === 1 && norm(options[0].textContent).startsWith(norm(value)) ? options[0] : null);
      if (hit) {
        hit.click();
        return true;
      }
    }
    return false;
  }

  self.__sarthiFill = async (answers, files) => {
    const done = [];
    const kept = [];
    const failed = [];
    const attached = [];
    for (const [key, answer] of Object.entries(answers)) {
      const els = Array.from(document.querySelectorAll(`[data-sarthi-key="${CSS.escape(key)}"]`));
      const el = els[0];
      if (!el) {
        failed.push(key);
        continue;
      }
      const type = (el.getAttribute("type") || "").toLowerCase();
      try {
        if (answer.source === "file") {
          const file = files[answer.value];
          if (!file || type !== "file") failed.push(key);
          else {
            attach(el, file);
            mark(el.closest("label, div") || el);
            attached.push(file.name);
            done.push(key);
          }
        } else if (type === "password" || SECRET.test(el.getAttribute("name") || "")) {
          failed.push(key); // never filled from Sarthi
        } else if (el.tagName === "SELECT") {
          const options = Array.from(el.options);
          const hit = options.find((o) => norm(o.text) === norm(answer.value)) ||
            options.find((o) => norm(o.value) === norm(answer.value));
          if (!hit) failed.push(key);
          else if (el.value && el.selectedIndex > 0 && el.value !== hit.value) kept.push(key);
          else {
            setValue(el, hit.value);
            mark(el);
            done.push(key);
          }
        } else if (type === "radio" || type === "checkbox") {
          if (els.length === 1 && type === "checkbox") {
            if (el.checked !== YES.test(answer.value.trim())) el.click();
            mark(el);
            done.push(key);
          } else {
            const wanted = type === "checkbox" ? answer.value.split(/\s*,\s*/).map(norm) : [norm(answer.value)];
            let hit = false;
            for (const input of els) {
              if (wanted.includes(norm(optionText(input)))) {
                if (!input.checked) input.click();
                mark(input);
                hit = true;
                if (type === "radio") break;
              }
            }
            (hit ? done : failed).push(key);
          }
        } else if (!["INPUT", "TEXTAREA"].includes(el.tagName)) {
          // a group of Yes/No buttons
          const button = Array.from(el.querySelectorAll("button")).find((b) => norm(b.textContent) === norm(answer.value));
          if (button) {
            button.click();
            done.push(key);
          } else failed.push(key);
        } else if (el.readOnly || el.disabled) {
          failed.push(key);
        } else if (clean(el.value) && norm(el.value) !== norm(answer.value)) {
          kept.push(key); // the site (or the user) already put something here: left as it is
        } else if (el.getAttribute("role") === "combobox") {
          if (await pickFromList(el, answer.value)) {
            mark(el);
            done.push(key);
          } else failed.push(key);
        } else {
          setValue(el, answer.value);
          mark(el);
          done.push(key);
        }
      } catch (error) {
        failed.push(key);
      }
    }
    return { done, kept, failed, attached };
  };

  // After Submit: what the user typed into the questions Sarthi could not answer (kept for next time).
  self.__sarthiRead = (wanted) => {
    const out = [];
    for (const { key, label } of wanted) {
      const els = Array.from(document.querySelectorAll(`[data-sarthi-key="${CSS.escape(key)}"]`));
      const el = els[0];
      if (!el) continue;
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (type === "password" || type === "file" || SECRET.test(label)) continue;
      let value = "";
      if (el.tagName === "SELECT") value = el.selectedIndex > 0 ? clean(el.options[el.selectedIndex].text) : "";
      else if (type === "radio" || type === "checkbox") value = els.filter((i) => i.checked).map(optionText).join(", ");
      else if ("value" in el) value = clean(el.value);
      if (value) out.push({ label, value });
    }
    return out;
  };
})();
