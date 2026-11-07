#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

# Default model per provider (override with --model or the QA_MODEL environment variable)
GEMINI_FALLBACKS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.5-flash"]   # tried if the main model is overloaded
DEFAULT_MODELS = {"gemini": "gemini-flash-latest", "ollama": "qwen2.5:7b", "anthropic": "claude-sonnet-5-5"}

# ---------------------------------------------------------------- page snapshot (runs in the page)
OBSERVE_JS = r"""
() => {
  document.querySelectorAll('[data-qa-id]').forEach(e => e.removeAttribute('data-qa-id'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
              '[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=option],' +
              '[onclick],[tabindex]:not([tabindex="-1"])';
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const items = []; let id = 0;
  for (const e of document.querySelectorAll(sel)) {
    if (items.length >= 150) break;
    if (!visible(e)) continue;
    const tag = e.tagName.toLowerCase();
    let text = clean(e.getAttribute('aria-label')) || clean(e.innerText).slice(0, 70);
    if (e.labels && e.labels.length) text = clean(e.labels[0].innerText) || text;
    const d = { id: ++id, tag };
    if (e.getAttribute('type')) d.type = e.getAttribute('type');
    if (text) d.text = text;
    if (e.getAttribute('placeholder')) d.placeholder = e.getAttribute('placeholder');
    if (e.getAttribute('name')) d.name = e.getAttribute('name');
    if (tag === 'a') d.href = (e.getAttribute('href') || '').slice(0, 80);
    if (e.disabled) d.disabled = true;
    if (e.type === 'checkbox' || e.type === 'radio') d.checked = e.checked;
    if (tag === 'input' && e.type !== 'password' && !['checkbox', 'radio'].includes(e.type) && e.value) d.value = e.value.slice(0, 40);
    if (tag === 'select') {
      d.options = [...e.options].slice(0, 40).map(o => clean(o.text));
      d.selected = e.options[e.selectedIndex] ? clean(e.options[e.selectedIndex].text) : '';
    }
    e.setAttribute('data-qa-id', id);
    items.push(d);
  }
  return { url: location.href, title: document.title,
           text: clean(document.body ? document.body.innerText : '').slice(0, 20000), elements: items };
}
"""

# ---------------------------------------------------------------- LLM tools
def _tool(name, desc, props, required):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}

TOOLS = [
    _tool("click", "Click the element with this id from the ELEMENTS list.",
          {"id": {"type": "integer"}}, ["id"]),
    _tool("type", "Replace the content of a text field. Set submit=true to press Enter afterwards.",
          {"id": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["id", "text"]),
    _tool("select", "Choose an option (by visible text) in a <select> element.",
          {"id": {"type": "integer"}, "option": {"type": "string"}}, ["id", "option"]),
    _tool("press", "Press a keyboard key, e.g. Enter, Tab, Escape.", {"key": {"type": "string"}}, ["key"]),
    _tool("goto", "Navigate the current tab to a URL.", {"url": {"type": "string"}}, ["url"]),
    _tool("scroll", "Scroll the page.", {"direction": {"type": "string", "enum": ["down", "up"]}}, ["direction"]),
    _tool("wait", "Wait for the page to update (max 5 seconds).", {"seconds": {"type": "number"}}, ["seconds"]),
    _tool("finish",
          "End the test with a verdict. Use 'pass' only if EVERY step was done and the expected result is "
          "visible RIGHT NOW. 'evidence' must be text copied verbatim from the current PAGE TEXT, URL or "
          "TITLE that proves the verdict (for 'fail', the text that shows the problem, or '' if none).",
          {"verdict": {"type": "string", "enum": ["pass", "fail"]},
           "reason": {"type": "string"}, "evidence": {"type": "string"}},
          ["verdict", "reason", "evidence"]),
]

SYSTEM = """You are an automated QA engineer executing ONE test case in a real web browser.
Rules:
- Carry out the test case literally, one action at a time, then check the expected result.
- Never assume success. A test PASSES only if each step worked AND the expected outcome is visibly
  present on the current page (use PAGE TEXT / URL / TITLE as proof). If the expected outcome is absent,
  wrong, or an error is shown, finish with 'fail' and explain.
- Use only information from the test case, the EXTRA CONTEXT, or the website itself (many demo sites
  display demo credentials on the page - use those). If a form needs data nobody gave you (e.g. a
  sign-up), invent plausible unique values.
- If an action failed, try a sensible alternative (another element, scroll, wait). Do not repeat the same
  failing action more than twice. If the goal is unreachable, finish with 'fail'.
- Elements are referenced by the numeric id in the ELEMENTS list of the CURRENT snapshot only.
- Cookie banners / popups that block the page may be dismissed.
- Be efficient: do not do steps the test case does not need."""

# ---------------------------------------------------------------- helpers
def norm(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", s).strip()


def observe(page):
    last = None
    for _ in range(4):
        try:
            return page.evaluate(OBSERVE_JS)
        except Exception as e:  # navigation in flight
            last = e
            page.wait_for_timeout(700)
    raise RuntimeError(f"could not read page: {last}")


def settle(page):
    for state, t in (("domcontentloaded", 5000), ("networkidle", 3000)):
        try:
            page.wait_for_load_state(state, timeout=t)
        except Exception:
            pass
    page.wait_for_timeout(300)


def build_prompt(case, extra, history, obs, max_elems=150, text_chars=4000):
    # keep form controls and buttons first, so a small context window never drops the login form
    ranked = sorted(obs["elements"], key=lambda e: 0 if e["tag"] in ("input", "select", "textarea", "button") else 1)
    els = "\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in ranked[:max_elems])
    hist = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-25:])) or "(none yet)"
    return (f"TEST CASE: {case}\n"
            f"EXTRA CONTEXT: {extra or '(none)'}\n\n"
            f"ACTIONS SO FAR:\n{hist}\n\n"
            f"CURRENT URL: {obs['url']}\nTITLE: {obs['title']}\n\n"
            f"PAGE TEXT (truncated):\n{obs['text'][:text_chars]}\n\n"
            f"ELEMENTS:\n{els}\n\nChoose the next action.")


ACTIONS = [t["name"] for t in TOOLS]
REQUIRED = {t["name"]: t["input_schema"]["required"] for t in TOOLS}

# JSON schema for Ollama's structured output. "thought" comes first so small models can plan briefly.
OLLAMA_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ACTIONS},
        "id": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"},
        "option": {"type": "string"}, "key": {"type": "string"}, "url": {"type": "string"},
        "seconds": {"type": "number"}, "direction": {"type": "string", "enum": ["down", "up"]},
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reason": {"type": "string"}, "evidence": {"type": "string"},
    },
    "required": ["thought", "action"],
}

JSON_SYSTEM = SYSTEM + """

Reply with ONE JSON object only: {"thought": "<one short sentence>", "action": "<name>", ...fields}.
Actions and the fields each needs:
  click: id | type: id, text, submit (optional) | select: id, option | press: key
  goto: url | scroll: direction | wait: seconds | finish: verdict, reason, evidence"""


def check_inputs(name, a):
    missing = [k for k in REQUIRED.get(name, []) if k not in a]
    if name not in REQUIRED or missing:
        raise ValueError(f"model reply invalid for action '{name}' (missing {missing})")


def http_json(url, body, headers=None, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_action(text):
    out = json.loads(text)
    if isinstance(out, list) and out:
        out = out[0]
    name = out.pop("action", "")
    out.pop("thought", None)
    return name, out


def make_asker(args):
    """Return ask(prompt) -> (action_name, inputs) for the chosen provider."""
    on_overload = None
    if args.provider == "gemini":
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            sys.exit("Set GEMINI_API_KEY first. Get a free key at https://aistudio.google.com/apikey\n"
                     "PowerShell:  $env:GEMINI_API_KEY=\"your-key\"")
        models = [args.model] + [m for m in GEMINI_FALLBACKS if m != args.model]
        cur = {"i": 0}

        def on_overload():
            cur["i"] = (cur["i"] + 1) % len(models)
            return models[cur["i"]]

        def ask(prompt):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{models[cur['i']]}:generateContent"
            body = {"systemInstruction": {"parts": [{"text": JSON_SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
            data = http_json(url, body, {"x-goog-api-key": key}, timeout=120)
            parts = data["candidates"][0]["content"]["parts"]
            return parse_action("".join(p.get("text", "") for p in parts))

    elif args.provider == "ollama":
        url = args.ollama_url.rstrip("/") + "/api/chat"

        def ask(prompt):
            body = {"model": args.model, "stream": False, "format": OLLAMA_SCHEMA,
                    "options": {"temperature": 0, "num_ctx": args.num_ctx},
                    "messages": [{"role": "system", "content": JSON_SYSTEM},
                                 {"role": "user", "content": prompt}]}
            return parse_action(http_json(url, body)["message"]["content"])

    else:
        try:
            from anthropic import Anthropic
        except ImportError:
            sys.exit("Install the SDK first: pip install anthropic")
        if not os.getenv("ANTHROPIC_API_KEY"):
            sys.exit("Set ANTHROPIC_API_KEY first.")
        client = Anthropic()

        def ask(prompt):
            resp = client.messages.create(model=args.model, max_tokens=700, system=SYSTEM, tools=TOOLS,
                                          tool_choice={"type": "any"},
                                          messages=[{"role": "user", "content": prompt}])
            block = next((b for b in resp.content if b.type == "tool_use"), None)
            if not block:
                raise ValueError("model returned no tool call")
            return block.name, dict(block.input)

    def ask_with_retry(prompt):
        err = None
        for attempt in range(6):
            try:
                name, a = ask(prompt)
                check_inputs(name, a)
                return name, a
            except urllib.error.HTTPError as e:          # must come before URLError (it is a subclass)
                detail = e.read().decode(errors="replace")[:300]
                if e.code == 429 or e.code >= 500:        # rate limit (free tier) or overloaded: wait, retry
                    err = f"HTTP {e.code} {detail[:120]}"
                    wait = min(5 * 2 ** attempt, 40)
                    note = ""
                    if args.provider == "gemini":
                        note = f" - switching to model {on_overload()}"
                    print(f"    {args.provider} busy (HTTP {e.code}){note}; retrying in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{args.provider} API error {e.code}: {detail}")
            except urllib.error.URLError as e:
                hint = (f" Start it with 'ollama serve' and run 'ollama pull {args.model}'."
                        if args.provider == "ollama" else " Check your internet connection.")
                raise RuntimeError(f"Cannot reach the {args.provider} API ({e.reason}).{hint}")
            except Exception as e:
                err = str(e)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"LLM call failed: {err}")

    return ask_with_retry


def loc(page, el_id):
    return page.locator(f'[data-qa-id="{int(el_id)}"]').first


def act(page, name, a):
    """Execute one action. Returns a short description for the history; raises on failure."""
    if name == "click":
        try:
            loc(page, a["id"]).click(timeout=5000)
        except Exception:
            loc(page, a["id"]).evaluate("e => e.click()")  # covered / animated elements
        return f"clicked element {a['id']}"
    if name == "type":
        loc(page, a["id"]).fill(a["text"], timeout=5000)
        if a.get("submit"):
            page.keyboard.press("Enter")
        return f"typed '{a['text']}' into element {a['id']}" + (" and pressed Enter" if a.get("submit") else "")
    if name == "select":
        l = loc(page, a["id"])
        try:
            l.select_option(label=a["option"], timeout=5000)
        except Exception:
            l.select_option(value=a["option"], timeout=5000)
        return f"selected '{a['option']}' in element {a['id']}"
    if name == "press":
        page.keyboard.press(a["key"])
        return f"pressed {a['key']}"
    if name == "goto":
        page.goto(a["url"], wait_until="domcontentloaded", timeout=30000)
        return f"navigated to {a['url']}"
    if name == "scroll":
        page.mouse.wheel(0, 700 if a["direction"] == "down" else -700)
        return f"scrolled {a['direction']}"
    if name == "wait":
        page.wait_for_timeout(int(min(float(a.get("seconds", 2)), 5) * 1000))
        return "waited"
    raise ValueError(f"unknown action {name}")


# ---------------------------------------------------------------- one test case
def run_case(case, idx, args, asker, outdir):
    started = time.time()
    result = {"test": case, "status": "ERROR", "reason": "", "evidence": "", "steps": [], "screenshot": None}
    history, rejected, repeats = [], 0, {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        state = {"page": ctx.new_page()}

        def adopt(p):  # links that open a new tab: follow them
            p.on("dialog", lambda d: d.accept())
            state["page"] = p

        state["page"].on("dialog", lambda d: d.accept())
        ctx.on("page", adopt)

        try:
            if args.url:
                state["page"].goto(args.url, wait_until="domcontentloaded", timeout=45000)
                history.append(f"opened start URL {args.url}")
            settle(state["page"])

            for step in range(args.max_steps):
                page = state["page"]
                obs = observe(page)
                name, a = asker(build_prompt(case, args.context, history, obs, args.max_elements, args.text_chars))
                print(f"  step {step + 1:>2}: {name} {json.dumps(a, ensure_ascii=False)[:110]}")

                if name == "finish":
                    verdict, evidence = a["verdict"], a.get("evidence", "")
                    haystack = norm(" ".join([obs["text"], obs["url"], obs["title"]]))
                    if verdict == "pass" and (not evidence.strip() or norm(evidence) not in haystack):
                        rejected += 1
                        history.append("REJECTED finish(pass): evidence was not found verbatim on the page; "
                                       "verify again or finish with fail")
                        if rejected >= 2:
                            result.update(status="FAIL", reason="Could not substantiate a pass with on-page evidence: "
                                          + a["reason"])
                            break
                        continue
                    result.update(status=verdict.upper(), reason=a["reason"], evidence=evidence)
                    break

                key = f"{name}:{json.dumps(a, sort_keys=True)}:{obs['url']}"
                repeats[key] = repeats.get(key, 0) + 1
                try:
                    done = act(page, name, a)
                    history.append(done + (" (this exact action was already repeated - try something else)"
                                           if repeats[key] > 2 else ""))
                except Exception as e:
                    history.append(f"FAILED {name} {json.dumps(a)}: {str(e).splitlines()[0][:120]}")
                result["steps"].append(history[-1])
                settle(state["page"])
            else:
                result.update(status="FAIL", reason=f"Gave up after {args.max_steps} steps without a verdict")

        except Exception as e:
            result.update(status="ERROR", reason=str(e))
        finally:
            try:
                shot = outdir / f"test_{idx + 1}.png"
                state["page"].screenshot(path=str(shot))
                result["screenshot"] = str(shot)
            except Exception:
                pass
            browser.close()

    result["seconds"] = round(time.time() - started, 1)
    return result


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Run plain-English test cases in a browser.")
    ap.add_argument("tests", nargs="*", help="test case(s) in plain English (quote each one)")
    ap.add_argument("--file", help="text file with one test case per line (# for comments)")
    ap.add_argument("--url", help="start URL (optional if the test case itself says where to go)")
    ap.add_argument("--context", default="", help="extra info for the agent, e.g. 'email=a@b.com password=xyz'")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--provider", choices=["gemini", "ollama", "anthropic"], default=os.getenv("QA_PROVIDER", "gemini"))
    ap.add_argument("--model", default=None, help="default: gemini-flash-latest / qwen2.5:7b (ollama) / claude-sonnet-5-5 (anthropic)")
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--num-ctx", type=int, default=8192, help="Ollama context window in tokens")
    ap.add_argument("--max-elements", type=int, default=None)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    cases = list(args.tests)
    if args.file:
        cases += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    if not cases:
        ap.error("give at least one test case (argument or --file)")
    args.model = args.model or os.getenv("QA_MODEL") or DEFAULT_MODELS[args.provider]
    local = args.provider == "ollama"
    args.max_elements = args.max_elements or (70 if local else 150)
    args.text_chars = 2500 if local else 4000
    asker = make_asker(args)
    print(f"Provider: {args.provider}   Model: {args.model}")

    outdir = Path(args.out) / datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, case in enumerate(cases):
        print(f"\n[{i + 1}/{len(cases)}] {case}")
        r = run_case(case, i, args, asker, outdir)
        results.append(r)
        print(f"  => {r['status']}: {r['reason']}")

    (outdir / "report.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n================ SUMMARY ================")
    for i, r in enumerate(results, 1):
        print(f"{i}. [{r['status']}] {r['test']}  ({r['seconds']}s)")
    print(f"\nReport + screenshots: {outdir}")
    sys.exit(0 if all(r["status"] == "PASS" for r in results) else 1)


if __name__ == "__main__":
    main()