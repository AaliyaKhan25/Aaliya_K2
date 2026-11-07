# Plain-English Test Runner (Python + Playwright + LLM)

A small tool that takes a test case written in **plain English**, for example

> "Log in as a customer and verify the dashboard is shown"

runs it automatically in a real browser, and reports **PASS**, **FAIL** or **ERROR**. It is not hard-coded to any website: you give it a start URL (or put the URL in the test itself) and the AI model works out what to click and type by looking at the page.

> Author: Aaliya Khan
> Demo site: https://phptravels.com/demo

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [How it works](#2-how-it-works)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
5. [Choosing an AI provider](#5-choosing-an-ai-provider)
6. [Running tests](#6-running-tests)
7. [Command-line options](#7-command-line-options)
8. [Environment variables](#8-environment-variables)
9. [Writing good test cases](#9-writing-good-test-cases)
10. [Output and reports](#10-output-and-reports)
11. [Design decisions](#11-design-decisions)
12. [Safety guards against wrong results](#12-safety-guards-against-wrong-results)
13. [Project structure](#13-project-structure)
14. [Known limitations](#14-known-limitations)
15. [Troubleshooting](#15-troubleshooting)
16. [Security notes](#16-security-notes)

---

## 1. What it does

- Accepts one or more test cases in plain English, from the command line or a text file.
- Opens a fresh Chromium browser for every test.
- Lets an AI model drive the browser step by step (click, type, select, press keys, navigate, scroll, wait).
- Decides PASS or FAIL by checking that the expected result is really visible on the page.
- Prints a summary, saves a screenshot per test and a `report.json` with every step.
- Returns exit code `0` if all tests passed, otherwise `1` (usable in CI).

## 2. How it works

The tool runs a simple **observe, decide, act** loop.

```
        ┌───────────────────────────────────────────────┐
        │ 1. OBSERVE  (Playwright)                      │
        │    URL, title, visible text, and a numbered   │
        │    list of interactive elements on screen     │
        └──────────────────────┬────────────────────────┘
                               ▼
        ┌───────────────────────────────────────────────┐
        │ 2. DECIDE  (LLM)                              │
        │    Input: test case + actions so far +        │
        │    current snapshot                           │
        │    Output: ONE action, or a final verdict     │
        └──────────────────────┬────────────────────────┘
                               ▼
        ┌───────────────────────────────────────────────┐
        │ 3. ACT  (Playwright)                          │
        │    click / type / select / press / goto /     │
        │    scroll / wait, then wait for page to settle│
        └──────────────────────┬────────────────────────┘
                               ▼
                    repeat until finish (or step limit)
```

Details:

1. **Snapshot.** A JavaScript function tags every visible interactive element (links, buttons, inputs, selects, elements with a role such as `button`/`tab`/`checkbox`, clickable elements) with a number and describes it: tag, type, visible text, placeholder, name, link target, checked state, select options. Passwords typed in fields are never included. The page text is included too, so the model can read demo credentials or success messages.
2. **Decision.** The model gets the test case, any extra context, the list of actions already taken and the current snapshot. It must reply with exactly one action. The snapshot is rebuilt every step, so element numbers always refer to the current page.
3. **Action.** The action is executed with Playwright. If a normal click fails (for example the element is covered), it falls back to a script click. If an action fails, the failure is recorded in the history so the model can try something else.
4. **Verdict.** When the model calls `finish`, the tool checks the evidence (see section 12) before accepting a PASS.

Other behaviour:

- **New tabs.** If a click opens a new tab (the phptravels demo does this), the tool follows it automatically.
- **Dialogs** (alert/confirm) are accepted automatically.
- **Settling.** After each action it waits for the page to load and for network activity to calm down.
- **Loop protection.** If the same action is repeated several times, the history tells the model to try something different. There is also a maximum step count (default 25).

## 3. Requirements

| Item | Notes |
|---|---|
| Python 3.9 or newer | `python --version` |
| Playwright for Python | Installed from `requirements.txt` |
| Chromium for Playwright | Installed with `python -m playwright install chromium` |
| One AI provider | Gemini (default, free key), Ollama (free, local), or Anthropic Claude (paid API) |
| Internet access | For the websites under test and for Gemini/Anthropic |

No other Python package is needed for Gemini or Ollama: those providers are called over plain HTTP with the standard library.

## 4. Installation

```bash
git clone <this-repository-url>
cd <repository-folder>

# optional but recommended: a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
python -m playwright install chromium
```

On Windows, if the command `playwright` is "not recognised", always use `python -m playwright ...` as shown above.

## 5. Choosing an AI provider

Select the provider with `--provider` (or the `QA_PROVIDER` environment variable). Default: `gemini`.

### Option A: Google Gemini (default, free tier)

1. Create a free key at https://aistudio.google.com/apikey
2. Set it in your terminal:

   ```powershell
   # Windows PowerShell
   $env:GEMINI_API_KEY="your-key"
   ```
   ```bash
   # macOS / Linux
   export GEMINI_API_KEY="your-key"
   ```
3. Run the tool. Default model: `gemini-flash-latest`.

The tool automatically retries when the service is busy (HTTP 429/5xx), waits with increasing delays and **falls back to other models** (`gemini-2.5-flash`, then `gemini-2.5-flash-lite`). Retry messages are printed so you can see what is happening.

### Option B: Ollama (free, runs on your own computer)

1. Install Ollama and pull a model once: `ollama pull qwen2.5:7b`
2. Make sure Ollama is running (the desktop app does this, or run `ollama serve`).
3. Run with `--provider ollama`.

Default model: `qwen2.5:7b`. You can choose another with `--model`, for example `--model qwen3:8b`. Small local models are less reliable on long multi-step tasks, so consider `--max-steps 35`. In this mode the tool sends a smaller snapshot (70 elements, 2,500 characters of text) so it fits the context window (`--num-ctx`, default 8192).

### Option C: Anthropic Claude (paid API)

1. `pip install anthropic`
2. Set `ANTHROPIC_API_KEY` (an API account with credits is required; this is separate from a chat subscription).
3. Run with `--provider anthropic`. Default model: `claude-sonnet-5-5`.

## 6. Running tests

**One test case, with the start URL:**

```bash
python nl_test_runner.py --url https://phptravels.com/demo "Log in as a customer and verify the dashboard is shown"
```

**Watch the browser while it works:**

```bash
python nl_test_runner.py --url https://phptravels.com/demo "Log in as a customer and verify the dashboard is shown" --headed
```

**Several test cases from a file** (one per line, lines starting with `#` are comments):

```bash
python nl_test_runner.py --file sample_tests.txt --url https://phptravels.com/demo
```

**Without `--url`:** put the address in the test itself:

```bash
python nl_test_runner.py "Go to https://example.com and verify the page heading says Example Domain"
```

**Give extra information** such as credentials the page does not show:

```bash
python nl_test_runner.py --url https://my-site.test --context "email=qa@test.com password=Secret123" "Log in and verify the account page is shown"
```

**Use another provider:**

```bash
python nl_test_runner.py --provider ollama --model qwen2.5:7b --url https://phptravels.com/demo "Log in as a customer and verify the dashboard is shown"
```

## 7. Command-line options

| Option | Default | Meaning |
|---|---|---|
| `tests` (positional) | none | One or more test cases in quotes |
| `--file <path>` | none | Text file with one test case per line |
| `--url <address>` | none | Page to open before the test starts |
| `--context "<text>"` | empty | Extra information for the model (credentials, data to use) |
| `--headed` | off | Show the browser window |
| `--max-steps <n>` | `25` | Maximum actions per test before it gives up |
| `--provider gemini\|ollama\|anthropic` | `gemini` | Which AI service to use |
| `--model <name>` | provider default | Model name |
| `--ollama-url <url>` | `http://localhost:11434` | Ollama server address |
| `--num-ctx <n>` | `8192` | Ollama context window in tokens |
| `--max-elements <n>` | `150` (`70` for Ollama) | How many page elements to show the model |
| `--out <folder>` | `results` | Folder for reports and screenshots |

## 8. Environment variables

| Variable | Used for |
|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Gemini provider |
| `ANTHROPIC_API_KEY` | Anthropic provider |
| `QA_PROVIDER` | Default provider |
| `QA_MODEL` | Default model name |
| `OLLAMA_URL` | Ollama server address |

## 9. Writing good test cases

The tool understands natural language, but clearer tests give more reliable results.

- Say **what to do** and **what must be true at the end**: "Log in as a customer **and verify the dashboard is shown**".
- Put data into the test or `--context` when the page does not show it.
- One goal per test case. Several tests are better than one very long one.
- Sites that show demo credentials on the page (like the phptravels demo) need no extra data; the model reads them from the page.

Examples (also in `sample_tests.txt`):

```
Log in as a customer and verify the dashboard is shown
Log in as an admin and verify the admin dashboard is shown
Open the demo page and verify that both a front-end and a back-end demo link are offered
Try to log in as a customer with a wrong password and verify an error message is shown
```

Negative tests work too. A test such as the last example passes when the error message really appears.

## 10. Output and reports

Console output while running:

```
Provider: gemini   Model: gemini-flash-latest

[1/1] Log in as a customer and verify the dashboard is shown
  step  1: click {"id": 14}
  step  2: type {"id": 3, "text": "user@phptravels.com"}
  ...
  => PASS: The dashboard page is displayed after login.

================ SUMMARY ================
1. [PASS] Log in as a customer and verify the dashboard is shown  (48.2s)

Report + screenshots: results\20261106_181852
```

(The step lines above only illustrate the format.)

Each run creates a time-stamped folder inside `results/`:

```
results/20261106_181852/
├── report.json      # full results for every test
└── test_1.png       # screenshot of the final page state
```

Fields in `report.json` per test:

| Field | Meaning |
|---|---|
| `test` | The test case text |
| `status` | `PASS`, `FAIL` or `ERROR` |
| `reason` | The model's explanation, or the error message |
| `evidence` | Text copied from the page that proves the verdict |
| `steps` | List of actions taken (including failed ones) |
| `screenshot` | Path of the final screenshot |
| `seconds` | Duration |

Status meanings:

| Status | Meaning |
|---|---|
| **PASS** | Every step worked and the expected outcome was visible on the page |
| **FAIL** | The expected outcome was not reached, an error was shown, or the step limit was hit |
| **ERROR** | The tool itself could not run the test (for example the AI service was unreachable or credits ran out) |

**Exit code:** `0` if every test passed, otherwise `1`.

## 11. Design decisions

| Decision | Reason |
|---|---|
| An AI model decides each action from a live snapshot | The tool must work on websites and test cases it has never seen, so nothing is hard-coded: no selectors, URLs or site names |
| Numbered elements instead of CSS selectors | The model refers to "element 14", which is robust against changing ids and classes and keeps replies short |
| The model sees only the current snapshot plus a text history | Keeps each request small, fast and cheap; old pages are not resent |
| Forced structured replies (JSON schema / tool calling) | Avoids free-text answers that cannot be executed |
| Several providers behind one small interface | Works with a free cloud key, a local model, or a paid API, so a reviewer can run it with whatever they have |
| Plain HTTP for Gemini and Ollama | No extra dependencies to install |
| Fresh browser per test | Tests cannot affect each other |

## 12. Safety guards against wrong results

AI models can claim success without proof. To prevent false passes:

1. A `finish(pass)` must include **evidence**, text copied from the page.
2. The tool checks that the evidence appears **word for word** in the current page text, URL or title.
3. If it does not, the pass is **rejected** and the model is told to verify again. After two rejections the test is marked FAIL.

Further guards:

- Failed actions are recorded and shown to the model.
- Repeating the same action more than twice triggers a hint to try something else.
- A step limit stops endless loops.

## 13. Project structure

```
.
├── nl_test_runner.py     # the whole tool
├── requirements.txt      # Python dependencies
├── sample_tests.txt      # example test cases
├── README.md
├── .gitignore
└── results/              # created at run time (reports and screenshots)
```

Inside `nl_test_runner.py`:

| Part | Purpose |
|---|---|
| `OBSERVE_JS` | In-page script that builds the snapshot |
| `TOOLS`, `SYSTEM`, `JSON_SYSTEM`, `OLLAMA_SCHEMA` | The actions the model may choose and the instructions it follows |
| `make_asker` | Provider layer: Gemini, Ollama, Anthropic, with retries and fallbacks |
| `build_prompt` | Builds the per-step prompt |
| `act` | Executes one action with Playwright |
| `run_case` | The observe, decide, act loop for one test |
| `main` | Command-line interface, summary and exit code |

## 14. Known limitations

- **Model quality matters.** Cloud models are noticeably more reliable than small local ones. Results can vary from run to run.
- **Iframes** are not read: the snapshot covers the main page of the current tab only.
- **CAPTCHAs**, multi-factor codes and similar human checks cannot be solved.
- **Very large pages:** only the first elements (form controls and buttons first) and the first part of the page text are shown to the model.
- **Visual checks** (colours, layout, images) are not supported; verification is based on text, URL and title.
- **Free-tier limits:** Gemini's free tier has request-rate limits, so long runs can pause while the tool retries.
- **Tests are not deterministic.** An AI-driven test can take a different path each time. The evidence check keeps the verdict honest, but speed and the exact steps can vary.
- **Privacy:** page text and element lists are sent to the chosen AI service. Do not run the tool against pages that contain real personal data or real credentials unless you use the local Ollama provider.

## 15. Troubleshooting

| Symptom | Fix |
|---|---|
| `playwright` is not recognised | Run `python -m playwright install chromium` |
| `export` is not recognised | You are in PowerShell. Use `$env:GEMINI_API_KEY="your-key"` |
| `can't open file ... nl_test_runner.py` | Wrong folder or file name. Run `dir` and check the file is listed; use `cd <folder>` first |
| Browser executable not found | Run `python -m playwright install chromium` |
| `ERROR: ... HTTP 503` or `429` (Gemini) | The model is busy or you hit the free-tier rate limit. The tool retries and switches models; wait a few minutes or use `--model gemini-2.5-flash` |
| `ERROR: ... credit balance is too low` (Anthropic) | Add credits to the API account or use `--provider gemini` / `ollama` |
| `Set GEMINI_API_KEY first` | Set the variable in the same terminal window before running; it is lost when the window closes |
| `Cannot reach the ollama API` | Start Ollama (`ollama serve`) and run `ollama pull <model>` |
| Test ends with `FAIL: Gave up after 25 steps` | Increase `--max-steps`, give more detail in the test, or use a stronger model |
| Test ends with `Could not substantiate a pass` | The model claimed success but quoted text that is not on the page; rerun or use a stronger model |

## 16. Security notes

- **Never commit API keys.** Keep them in environment variables only. `.gitignore` excludes `results/`, `.env` and `venv/`.
- If a key is ever pasted into a chat, screenshot or repository by mistake, delete it in the provider's console and create a new one.
- Run the tool only against sites you are allowed to test.

---

*Built for the CloudQA internship screening task.*