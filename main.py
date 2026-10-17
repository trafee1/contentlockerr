import concurrent.futures
from datetime import datetime, timezone
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

# ---------------------------------------------------------
# Environment Parsers & Utilities (Case-Insensitive)
# ---------------------------------------------------------
ENV_MAP = {k.upper(): v for k, v in os.environ.items()}

def get_env(key: str, default: str = "") -> str:
    return ENV_MAP.get(key.upper(), default).strip()

def parse_num(val_str: str, default: float) -> float:
    try:
        return float(val_str.strip())
    except (ValueError, TypeError):
        return default

def parse_range(var_name: str, default_min: float, default_max: float):
    raw_val = get_env(var_name)
    if not raw_val:
        return default_min, default_max
    try:
        parts = [p.strip() for p in raw_val.split(",") if p.strip()]
        if len(parts) >= 2:
            return parse_num(parts[0], default_min), parse_num(parts[1], default_max)
        elif len(parts) == 1:
            val = parse_num(parts[0], default_min)
            return val, val
    except Exception:
        print(f"[WARN] Invalid range in '{var_name}' ('{raw_val}'). Using defaults ({default_min}, {default_max}).")
    return default_min, default_max

def parse_bool(var_name: str, default: bool = False) -> bool:
    val = get_env(var_name)
    if not val:
        return default
    return val.lower() in ("true", "1", "yes", "on", "enable")

def parse_list(var_name: str, defaults: list) -> list:
    raw_val = get_env(var_name)
    if not raw_val and not var_name.endswith("S"):
        raw_val = get_env(f"{var_name}S")
    elif not raw_val and var_name.endswith("S"):
        raw_val = get_env(var_name[:-1])

    if not raw_val:
        return defaults
    items = [item.strip() for item in raw_val.split(",") if item.strip()]
    return items if items else defaults

def truncate_url(url: str, head_len: int = 18, tail_len: int = 14) -> str:
    if len(url) <= (head_len + tail_len + 5):
        return url
    return f"{url[:head_len]}...{url[-tail_len:]}"

def parse_native_ratio(var_name: str, default_pct: float = 80.0) -> float:
    raw = get_env(var_name)
    if not raw:
        return default_pct / 100.0
    try:
        if "," in raw:
            val = float(raw.split(",")[0].strip())
        else:
            val = float(raw.strip())
        if val > 1.0:
            return min(1.0, max(0.0, val / 100.0))
        return min(1.0, max(0.0, val))
    except Exception:
        return default_pct / 100.0

def parse_tier_ratio(var_name="TIER_RATIO", default=(70.0, 25.0, 5.0)):
    raw = get_env(var_name)
    if not raw:
        return default
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    try:
        if len(parts) == 1:
            t1 = float(parts[0])
            rem = max(0.0, 100.0 - t1)
            t2 = rem * (2.0 / 3.0)
            t3 = rem * (1.0 / 3.0)
            return (t1, t2, t3)
        elif len(parts) >= 3:
            return (float(parts[0]), float(parts[1]), float(parts[2]))
        elif len(parts) == 2:
            return (float(parts[0]), float(parts[1]), max(0.0, 100.0 - float(parts[0]) - float(parts[1])))
    except ValueError:
        pass
    return default

T1_RATIO, T2_RATIO, T3_RATIO = parse_tier_ratio("TIER_RATIO", (70.0, 25.0, 5.0))


# =========================================================
# DYNAMIC SCROLL & BUTTON ENGINE
# =========================================================

BUTTON_CLICK_ENABLE = parse_bool("BUTTON_CLICK_ENABLE", default=False)
BUTTON_TARGETS = parse_list("BUTTON_TARGETS", [])
BUTTON_SELECTION_MODE = get_env("BUTTON_SELECTION_MODE", "first").lower()
BUTTON_CLICK_COUNT = get_env("BUTTON_CLICK_COUNT", "1").strip().lower()
BUTTON_PAGE_LOAD_WAIT = get_env("BUTTON_PAGE_LOAD_WAIT", "fully_loaded").strip().lower()

BUTTON_L2_CLICK_ENABLE = parse_bool("BUTTON_L2_CLICK_ENABLE", default=False)
BUTTON_L2_TARGETS = parse_list("BUTTON_L2_TARGETS", [])
BUTTON_L2_SELECTION_MODE = get_env("BUTTON_L2_SELECTION_MODE", "first").lower()
BUTTON_L2_CLICK_COUNT = get_env("BUTTON_L2_CLICK_COUNT", "1").strip().lower()
L2_DELAY_MIN, L2_DELAY_MAX = parse_range("BUTTON_L2_DELAY_RANGE", 2.0, 4.0)

# SCROLL_MODE can be: off, before{20,80}, after{30,90}, random{25,90}
RAW_SCROLL_MODE = get_env("SCROLL_MODE", "off").strip()

def parse_scroll_specification(spec_str: str):
    """
    Parses scroll config:
    Returns (mode, min_pct, max_pct)
    Modes: 'off', 'before', 'after', 'random'
    """
    spec_str = spec_str.strip()
    if not spec_str or spec_str.lower() in ("off", "none", "false", "0"):
        return "off", 0.0, 0.0

    match = re.match(r"^(before|after|random)\{([^}]+)\}$", spec_str, re.IGNORECASE)
    if match:
        mode = match.group(1).lower()
        parts = [p.strip() for p in match.group(2).split(",") if p.strip()]
        try:
            if len(parts) >= 2:
                p_min = min(100.0, max(1.0, float(parts[0])))
                p_max = min(100.0, max(1.0, float(parts[1])))
                if p_min > p_max:
                    p_min, p_max = p_max, p_min
                return mode, p_min, p_max
            elif len(parts) == 1:
                p_val = min(100.0, max(1.0, float(parts[0])))
                return mode, p_val, p_val
        except ValueError:
            return "off", 0.0, 0.0
    return "off", 0.0, 0.0

PARSED_SCROLL_MODE, SCROLL_MIN_PCT, SCROLL_MAX_PCT = parse_scroll_specification(RAW_SCROLL_MODE)

def resolve_bot_scroll():
    """Resolves specific runtime scroll action and percentage for an individual bot run."""
    if PARSED_SCROLL_MODE == "off":
        return "off", 0.0
    
    target_pct = round(random.uniform(SCROLL_MIN_PCT, SCROLL_MAX_PCT), 1)
    if PARSED_SCROLL_MODE == "random":
        chosen_timing = random.choice(["before", "after"])
    else:
        chosen_timing = PARSED_SCROLL_MODE
    return chosen_timing, target_pct

def parse_target_pattern(pat_str: str) -> dict:
    pat_str = pat_str.strip()
    tags = []
    curr = pat_str
    while "{" in curr and curr.endswith("}"):
        tag_part, rest = curr.split("{", 1)
        tag_part = tag_part.strip().lower()
        if tag_part:
            tags.append(tag_part)
        curr = rest[:-1].strip()
    return {"tags": tags, "text": curr.lower()}

def resolve_target_click_count(count_setting: str) -> str:
    if count_setting in ("all", "-1"):
        return '"all"'
    if "," in count_setting:
        parts = [p.strip() for p in count_setting.split(",") if p.strip()]
        try:
            low = int(parts[0])
            high = int(parts[1]) if len(parts) >= 2 else low
            if low > high:
                low, high = high, low
            chosen = random.randint(low, high)
            return str(chosen)
        except ValueError:
            return "1"
    try:
        return str(max(1, int(count_setting)))
    except ValueError:
        return "1"

def resolve_initial_wait_ms(wait_setting: str) -> int:
    if wait_setting in ("fully_loaded", "default", "load", ""):
        return 1500
    if "," in wait_setting:
        parts = [p.strip() for p in wait_setting.split(",") if p.strip()]
        try:
            w_min = float(parts[0])
            w_max = float(parts[1]) if len(parts) >= 2 else w_min
            return int(random.uniform(w_min, w_max) * 1000)
        except ValueError:
            return 1500
    try:
        return int(float(wait_setting) * 1000)
    except ValueError:
        return 1500

def generate_js_scenario_script(scroll_timing: str, scroll_pct: float) -> str:
    l1_patterns = [parse_target_pattern(t) for t in BUTTON_TARGETS if t.strip()] if (BUTTON_CLICK_ENABLE and BUTTON_TARGETS) else []
    l1_count_str = resolve_target_click_count(BUTTON_CLICK_COUNT)
    l1_wait_ms = resolve_initial_wait_ms(BUTTON_PAGE_LOAD_WAIT)

    l2_active = BUTTON_L2_CLICK_ENABLE and bool(BUTTON_L2_TARGETS) and BUTTON_CLICK_ENABLE
    l2_patterns = [parse_target_pattern(t) for t in BUTTON_L2_TARGETS if t.strip()] if l2_active else []
    l2_count_str = resolve_target_click_count(BUTTON_L2_CLICK_COUNT) if l2_active else "1"
    l2_delay_ms = int(random.uniform(L2_DELAY_MIN, L2_DELAY_MAX) * 1000)

    l1_patterns_json = json.dumps(l1_patterns)
    l2_patterns_json = json.dumps(l2_patterns)
    has_buttons = bool(BUTTON_CLICK_ENABLE and BUTTON_TARGETS)

    js_code = f"""(async function() {{
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        await sleep({l1_wait_ms});

        async function performSmoothScroll(pct) {{
            if (!pct || pct <= 0) return 0;
            const scrollHeight = Math.max(
                document.body.scrollHeight, document.documentElement.scrollHeight,
                document.body.offsetHeight, document.documentElement.offsetHeight,
                document.body.clientHeight, document.documentElement.clientHeight
            );
            const viewPortHeight = window.innerHeight || document.documentElement.clientHeight;
            const maxScrollable = Math.max(0, scrollHeight - viewPortHeight);
            const targetY = Math.floor(maxScrollable * (Math.min(100, pct) / 100.0));
            
            let currentY = window.pageYOffset || document.documentElement.scrollTop;
            const step = Math.max(15, Math.floor(Math.abs(targetY - currentY) / 18));
            
            while (Math.abs(targetY - currentY) > step) {{
                currentY += (targetY > currentY) ? step : -step;
                window.scrollTo(0, currentY);
                await sleep(25);
            }}
            window.scrollTo(0, targetY);
            await sleep(350);
            return targetY;
        }}

        function findMatchingElements(patterns) {{
            if (!patterns || patterns.length === 0) return [];
            const interactiveSelectors = 'button, a, input[type="button"], input[type="submit"], [role="button"], span, div, p';
            const allElements = Array.from(document.querySelectorAll(interactiveSelectors));

            for (const pat of patterns) {{
                const targetText = pat.text.trim();
                const matched = [];

                for (const el of allElements) {{
                    const elText = (el.innerText || el.textContent || el.value || '').toLowerCase();
                    if (!elText.includes(targetText)) continue;

                    if (pat.tags && pat.tags.length > 0) {{
                        let curr = el;
                        let matchedTags = true;
                        for (let i = pat.tags.length - 1; i >= 0; i--) {{
                            const expectedTag = pat.tags[i];
                            const ancestor = curr.closest(expectedTag);
                            if (!ancestor) {{
                                matchedTags = false;
                                break;
                            }}
                            curr = ancestor.parentElement;
                            if (!curr && i > 0) {{
                                matchedTags = false;
                                break;
                            }}
                        }}
                        if (matchedTags) matched.push(el);
                    }} else {{
                        matched.push(el);
                    }}
                }}
                if (matched.length > 0) return matched;
            }}
            return [];
        }}

        function selectAndClick(elements, mode, countLimit) {{
            if (!elements || elements.length === 0) return 0;
            let candidates = [...elements];
            if (mode === 'last') candidates.reverse();
            else if (mode === 'random') candidates.sort(() => Math.random() - 0.5);

            let maxClicks = (countLimit === 'all') ? candidates.length : Math.min(candidates.length, parseInt(countLimit, 10) || 1);
            let clicked = 0;

            for (let i = 0; i < maxClicks; i++) {{
                const target = candidates[i];
                try {{
                    target.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                    target.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                    target.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                    target.focus();
                    target.click();
                    target.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                    clicked++;
                }} catch (e) {{}}
            }}
            return clicked;
        }}

        // Execution: BEFORE Scroll
        if ('{scroll_timing}' === 'before') {{
            await performSmoothScroll({scroll_pct});
        }}

        let l1ClickedCount = 0;
        if ({str(has_buttons).lower()}) {{
            const l1Matched = findMatchingElements({l1_patterns_json});
            l1ClickedCount = selectAndClick(l1Matched, '{BUTTON_SELECTION_MODE}', {l1_count_str});

            if ({str(l2_active).lower()} && l1ClickedCount > 0) {{
                await sleep({l2_delay_ms});
                const l2Matched = findMatchingElements({l2_patterns_json});
                selectAndClick(l2Matched, '{BUTTON_L2_SELECTION_MODE}', {l2_count_str});
            }}
        }}

        // Execution: AFTER Scroll
        if ('{scroll_timing}' === 'after') {{
            await sleep(600);
            await performSmoothScroll({scroll_pct});
        }}
    }})();"""
    return js_code


# =========================================================
# MANUAL USER AGENT POOLS
# =========================================================

WIN_CHROME_HIGH = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]
WIN_CHROME_MID = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
]
WIN_CHROME_LOW = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

WIN_EDGE_HIGH = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
]
WIN_EDGE_MID = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
]
WIN_EDGE_LOW = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
]

WIN_FF_HIGH = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
]
WIN_FF_MID = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
]
WIN_FF_LOW = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

WIN_OPERA_HIGH = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 OPR/117.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 OPR/115.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 OPR/112.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 OPR/110.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 OPR/106.0.0.0",
]
WIN_OPERA_MID = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 OPR/104.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 OPR/102.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 OPR/101.0.0.0",
]
WIN_OPERA_LOW = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 OPR/98.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 OPR/94.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 OPR/91.0.0.0",
]

MAC_SAFARI_HIGH = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]
MAC_SAFARI_MID = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]
MAC_SAFARI_LOW = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Safari/605.1.15",
]

MAC_CHROME_HIGH = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]
MAC_CHROME_MID = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
]
MAC_CHROME_LOW = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

MAC_FF_HIGH = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:140.0) Gecko/20100101 Firefox/140.0",
]
MAC_FF_MID = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0",
]
MAC_FF_LOW = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]

MAC_OPERA_ALL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 OPR/116.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 OPR/113.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 OPR/110.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 OPR/106.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 OPR/102.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 OPR/98.0.0.0",
]

LINUX_CHROME_HIGH = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]
LINUX_CHROME_MID = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
]
LINUX_CHROME_LOW = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]
LINUX_FF_HIGH = [
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0",
    "Mozilla/5.0 (X11; Fedora; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
]
LINUX_FF_MID = [
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
]
LINUX_FF_LOW = [
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]
LINUX_OPERA_ALL = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 OPR/114.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 OPR/110.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 OPR/106.0.0.0",
]

IOS_SAFARI_HIGH = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
]
IOS_SAFARI_MID = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]
IOS_SAFARI_LOW = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
]

IOS_CHROME_HIGH = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/152.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/150.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/148.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/145.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/142.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0.0.0 Mobile/15E148 Safari/604.1",
]
IOS_CHROME_MID = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/139.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/137.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/135.0.0.0 Mobile/15E148 Safari/604.1",
]
IOS_CHROME_LOW = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/128.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.0.0 Mobile/15E148 Safari/604.1",
]
IOS_FF_ALL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/150.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/146.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/142.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/138.0 Mobile/15E148 Safari/604.1",
]
IOS_OPERA_ALL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) OPT/5.2.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) OPT/5.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) OPT/4.8.0 Mobile/15E148 Safari/604.1",
]

ANDROID_CHROME_HIGH = [
    "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 15; SM-S938B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; CPH2573) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-A556B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
]
ANDROID_CHROME_MID = [
    "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; 2210132G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36",
]
ANDROID_CHROME_LOW = [
    "Mozilla/5.0 (Linux; Android 12; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
]

ANDROID_SAMSUNG_HIGH = [
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-A556B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36",
]
ANDROID_SAMSUNG_MID = [
    "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/22.0 Chrome/111.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/21.0 Chrome/110.0.0.0 Mobile Safari/537.36",
]
ANDROID_SAMSUNG_LOW = [
    "Mozilla/5.0 (Linux; Android 11; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/20.0 Chrome/106.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-A515F) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/19.0 Chrome/102.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/18.0 Chrome/99.0.0.0 Mobile Safari/537.36",
]

ANDROID_FF_ALL = [
    "Mozilla/5.0 (Android 15; Mobile; rv:152.0) Gecko/152.0 Firefox/152.0",
    "Mozilla/5.0 (Android 15; Mobile; rv:150.0) Gecko/150.0 Firefox/150.0",
    "Mozilla/5.0 (Android 14; Mobile; rv:148.0) Gecko/148.0 Firefox/148.0",
    "Mozilla/5.0 (Android 14; Mobile; rv:146.0) Gecko/146.0 Firefox/146.0",
    "Mozilla/5.0 (Android 14; Mobile; rv:144.0) Gecko/144.0 Firefox/144.0",
    "Mozilla/5.0 (Android 14; Mobile; rv:142.0) Gecko/142.0 Firefox/142.0",
    "Mozilla/5.0 (Android 13; Mobile; rv:140.0) Gecko/140.0 Firefox/140.0",
    "Mozilla/5.0 (Android 13; Mobile; rv:138.0) Gecko/138.0 Firefox/138.0",
    "Mozilla/5.0 (Android 13; Mobile; rv:136.0) Gecko/136.0 Firefox/136.0",
    "Mozilla/5.0 (Android 12; Mobile; rv:134.0) Gecko/134.0 Firefox/134.0",
    "Mozilla/5.0 (Android 12; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0",
    "Mozilla/5.0 (Android 11; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0",
]

ANDROID_OPERA_ALL = [
    "Mozilla/5.0 (Linux; Android 15; SM-S938B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36 OPR/88.0.0.0",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36 OPR/86.0.0.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36 OPR/84.0.0.0",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36 OPR/82.0.0.0",
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36 OPR/79.0.0.0",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 OPR/75.0.0.0",
]

def sample_weighted_browser(high_list, mid_list, low_list):
    roll = random.random()
    if roll < 0.65 and high_list:
        return random.choice(high_list)
    elif roll < 0.90 and mid_list:
        return random.choice(mid_list)
    elif low_list:
        return random.choice(low_list)
    combined = high_list + mid_list + low_list
    return random.choice(combined) if combined else ""

def tokenize_balanced_blocks(s: str) -> list:
    tokens, depth, buf = [], 0, []
    for ch in s:
        if ch == '{':
            depth += 1
            buf.append(ch)
        elif ch == '}':
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == ',' and depth == 0:
            token = "".join(buf).strip()
            if token:
                tokens.append(token)
            buf = []
        else:
            buf.append(ch)
    last = "".join(buf).strip()
    if last:
        tokens.append(last)
    return tokens

def parse_nested_device_spec(spec_str: str) -> dict:
    spec_str = spec_str.strip()
    if not spec_str:
        return {
            "mobile": {"android": ["chrome", "samsung", "firefox", "opera"], "ios": ["safari", "chrome", "firefox", "opera"]},
            "desktop": {"windows": ["chrome", "edge", "firefox", "opera"], "mac": ["safari", "chrome", "firefox", "opera"], "linux": ["chrome", "firefox", "opera"]}
        }

    tree = {}
    cat_blocks = tokenize_balanced_blocks(spec_str)

    for block in cat_blocks:
        match = re.match(r"^([a-zA-Z]+)(?:\{(.*)\})?$", block, re.DOTALL)
        if not match:
            continue
        cat = match.group(1).lower().strip()
        body = match.group(2)

        if cat not in ("mobile", "desktop"):
            continue

        tree[cat] = {}

        if not body:
            if cat == "mobile":
                tree[cat] = {"android": ["chrome", "samsung", "firefox", "opera"], "ios": ["safari", "chrome", "firefox", "opera"]}
            else:
                tree[cat] = {"windows": ["chrome", "edge", "firefox", "opera"], "mac": ["safari", "chrome", "firefox", "opera"], "linux": ["chrome", "firefox", "opera"]}
            continue

        os_blocks = tokenize_balanced_blocks(body)
        for os_blk in os_blocks:
            os_match = re.match(r"^([a-zA-Z]+)(?:\{(.*)\})?$", os_blk, re.DOTALL)
            if not os_match:
                continue
            os_name = os_match.group(1).lower().strip()
            b_body = os_match.group(2)

            if b_body:
                browsers = [b.strip().lower() for b in tokenize_balanced_blocks(b_body) if b.strip()]
            else:
                if os_name == "windows":
                    browsers = ["chrome", "edge", "firefox", "opera"]
                elif os_name in ("mac", "macos"):
                    browsers = ["safari", "chrome", "firefox", "opera"]
                elif os_name == "linux":
                    browsers = ["chrome", "firefox", "opera"]
                elif os_name == "ios":
                    browsers = ["safari", "chrome", "firefox", "opera"]
                elif os_name == "android":
                    browsers = ["chrome", "samsung", "firefox", "opera"]
                else:
                    browsers = ["chrome"]

            tree[cat][os_name] = browsers

    if not tree or not any(tree.values()):
        tree = {"mobile": {"android": ["chrome", "firefox"]}}

    return tree

RAW_DEVICE_SPEC = get_env("DEVICE_TYPE", "desktop,mobile")
DEVICE_TREE = parse_nested_device_spec(RAW_DEVICE_SPEC)
NATIVE_RATIO = parse_native_ratio("BROWSER_NATIVE_RATIO", default_pct=80.0)

def validate_ua_entries(uas: list, expected_scope: str):
    desktop_signatures = ["windows nt", "macintosh", "x11; linux x86_64", "fedora", "ubuntu"]
    mobile_signatures = ["mobile", "android", "iphone", "ipad", "crios", "fxios"]

    for idx, ua in enumerate(uas, start=1):
        ua_clean = ua.lower()
        if expected_scope == "mobile":
            has_desktop = any(d in ua_clean for d in desktop_signatures)
            is_mobile = any(m in ua_clean for m in mobile_signatures)
            if has_desktop and not is_mobile:
                print("\n" + "#" * 70)
                print(" [PINNED CRITICAL ERROR] INVALID USER-AGENT CONFLICT!")
                print(f" Item #{idx} in MOBILE_USER_AGENTS has DESKTOP signatures:")
                print(f" String: {ua}")
                print(" Execution terminated to prevent platform header corruption.")
                print("#" * 70 + "\n")
                sys.exit(1)

        elif expected_scope == "desktop":
            is_mobile = any(m in ua_clean for m in mobile_signatures)
            if is_mobile and "ipad" not in ua_clean:
                print("\n" + "#" * 70)
                print(" [PINNED CRITICAL ERROR] INVALID USER-AGENT CONFLICT!")
                print(f" Item #{idx} in DESKTOP_USER_AGENTS has MOBILE signatures:")
                print(f" String: {ua}")
                print(" Execution terminated to prevent platform header corruption.")
                print("#" * 70 + "\n")
                sys.exit(1)

CUSTOM_DESKTOP_UAS = parse_list("DESKTOP_USER_AGENTS", [])
CUSTOM_MOBILE_UAS = parse_list("MOBILE_USER_AGENTS", [])

if CUSTOM_DESKTOP_UAS:
    validate_ua_entries(CUSTOM_DESKTOP_UAS, "desktop")
if CUSTOM_MOBILE_UAS:
    validate_ua_entries(CUSTOM_MOBILE_UAS, "mobile")

NATIVE_BROWSER_MAP = {
    "ios": "safari",
    "android": "chrome",
    "windows": "chrome",
    "mac": "safari",
    "macos": "safari",
    "linux": "chrome"
}

def pick_browser_for_os(os_name: str, allowed_browsers: list, native_pct: float) -> str:
    if len(allowed_browsers) == 1:
        return allowed_browsers[0]

    native_name = NATIVE_BROWSER_MAP.get(os_name, "")
    if native_name and native_name in allowed_browsers:
        other_browsers = [b for b in allowed_browsers if b != native_name]
        if other_browsers and random.random() < native_pct:
            return native_name
        elif other_browsers:
            return random.choice(other_browsers)
        return native_name

    return random.choice(allowed_browsers)

def get_ua_for_selection(category: str, os_name: str, browser: str) -> tuple:
    if category == "mobile" and CUSTOM_MOBILE_UAS:
        return random.choice(CUSTOM_MOBILE_UAS), "Custom:Mobile"
    if category == "desktop" and CUSTOM_DESKTOP_UAS:
        return random.choice(CUSTOM_DESKTOP_UAS), "Custom:Desktop"

    if category == "mobile":
        if os_name == "android":
            if browser == "firefox":
                return random.choice(ANDROID_FF_ALL), "Android:Firefox"
            elif browser == "samsung":
                return sample_weighted_browser(ANDROID_SAMSUNG_HIGH, ANDROID_SAMSUNG_MID, ANDROID_SAMSUNG_LOW), "Android:Samsung"
            elif browser == "opera":
                return random.choice(ANDROID_OPERA_ALL), "Android:Opera"
            elif browser == "chrome":
                return sample_weighted_browser(ANDROID_CHROME_HIGH, ANDROID_CHROME_MID, ANDROID_CHROME_LOW), "Android:Chrome"
            return random.choice(ANDROID_FF_ALL), "Android:Firefox"

        elif os_name == "ios":
            if browser == "firefox":
                return random.choice(IOS_FF_ALL), "iOS:Firefox"
            elif browser == "chrome":
                return sample_weighted_browser(IOS_CHROME_HIGH, IOS_CHROME_MID, IOS_CHROME_LOW), "iOS:Chrome"
            elif browser == "opera":
                return random.choice(IOS_OPERA_ALL), "iOS:Opera"
            elif browser == "safari":
                return sample_weighted_browser(IOS_SAFARI_HIGH, IOS_SAFARI_MID, IOS_SAFARI_LOW), "iOS:Safari"
            return sample_weighted_browser(IOS_SAFARI_HIGH, IOS_SAFARI_MID, IOS_SAFARI_LOW), "iOS:Safari"

        return random.choice(ANDROID_FF_ALL), "Android:Firefox"

    if category == "desktop":
        if os_name == "windows":
            if browser == "firefox":
                return sample_weighted_browser(WIN_FF_HIGH, WIN_FF_MID, WIN_FF_LOW), "Win:Firefox"
            elif browser == "edge":
                return sample_weighted_browser(WIN_EDGE_HIGH, WIN_EDGE_MID, WIN_EDGE_LOW), "Win:Edge"
            elif browser == "opera":
                return sample_weighted_browser(WIN_OPERA_HIGH, WIN_OPERA_MID, WIN_OPERA_LOW), "Win:Opera"
            elif browser == "chrome":
                return sample_weighted_browser(WIN_CHROME_HIGH, WIN_CHROME_MID, WIN_CHROME_LOW), "Win:Chrome"
            return sample_weighted_browser(WIN_CHROME_HIGH, WIN_CHROME_MID, WIN_CHROME_LOW), "Win:Chrome"

        elif os_name in ("mac", "macos"):
            if browser == "safari":
                return sample_weighted_browser(MAC_SAFARI_HIGH, MAC_SAFARI_MID, MAC_SAFARI_LOW), "Mac:Safari"
            elif browser == "chrome":
                return sample_weighted_browser(MAC_CHROME_HIGH, MAC_CHROME_MID, MAC_CHROME_LOW), "Mac:Chrome"
            elif browser == "firefox":
                return sample_weighted_browser(MAC_FF_HIGH, MAC_FF_MID, MAC_FF_LOW), "Mac:Firefox"
            elif browser == "opera":
                return random.choice(MAC_OPERA_ALL), "Mac:Opera"
            return sample_weighted_browser(MAC_SAFARI_HIGH, MAC_SAFARI_MID, MAC_SAFARI_LOW), "Mac:Safari"

        elif os_name == "linux":
            if browser == "firefox":
                return sample_weighted_browser(LINUX_FF_HIGH, LINUX_FF_MID, LINUX_FF_LOW), "Linux:Firefox"
            elif browser == "opera":
                return random.choice(LINUX_OPERA_ALL), "Linux:Opera"
            return sample_weighted_browser(LINUX_CHROME_HIGH, LINUX_CHROME_MID, LINUX_CHROME_LOW), "Linux:Chrome"

        return sample_weighted_browser(WIN_CHROME_HIGH, WIN_CHROME_MID, WIN_CHROME_LOW), "Win:Chrome"

    return sample_weighted_browser(WIN_CHROME_HIGH, WIN_CHROME_MID, WIN_CHROME_LOW), "Win:Chrome"

def resolve_cycle_ua(used_uas_in_cycle: set) -> tuple:
    cats = list(DEVICE_TREE.keys())
    cat = random.choice(cats) if cats else "mobile"

    sub_os_dict = DEVICE_TREE.get(cat, {})
    if not sub_os_dict:
        sub_os_dict = {"android": ["chrome"]} if cat == "mobile" else {"windows": ["chrome"]}

    chosen_os = random.choice(list(sub_os_dict.keys()))
    allowed_browsers = sub_os_dict.get(chosen_os, ["chrome"])
    
    chosen_browser = pick_browser_for_os(chosen_os, allowed_browsers, NATIVE_RATIO)

    ua, label = "", ""
    for _ in range(15):
        ua, label = get_ua_for_selection(cat, chosen_os, chosen_browser)
        if ua not in used_uas_in_cycle:
            break

    used_uas_in_cycle.add(ua)
    return cat, ua, label

# ---------------------------------------------------------
# Datacenter Country Pool
# ---------------------------------------------------------
ALL_DATACENTER_NON_PREMIUM = [
    ("AE", "United Arab Emirates"), ("AR", "Argentina"), ("AT", "Austria"),
    ("AU", "Australia"), ("BE", "Belgium"), ("BG", "Bulgaria"),
    ("BR", "Brazil"), ("CA", "Canada"), ("CH", "Switzerland"),
    ("CL", "Chile"), ("CO", "Colombia"), ("CZ", "Czech Republic"),
    ("DE", "Germany"), ("DK", "Denmark"), ("ES", "Spain"),
    ("FI", "Finland"), ("FR", "France"), ("GB", "United Kingdom"),
    ("GR", "Greece"), ("HK", "Hong Kong"), ("HR", "Croatia"),
    ("HU", "Hungary"), ("ID", "Indonesia"), ("IE", "Ireland"),
    ("IL", "Israel"), ("IN", "India"), ("IT", "Italy"),
    ("JP", "Japan"), ("KR", "South Korea"), ("MX", "Mexico"),
    ("MY", "Malaysia"), ("NL", "Netherlands"), ("NO", "Norway"),
    ("NZ", "New Zealand"), ("PH", "Philippines"), ("PL", "Poland"),
    ("PT", "Portugal"), ("RO", "Romania"), ("RS", "Serbia"),
    ("SA", "Saudi Arabia"), ("SE", "Sweden"), ("SG", "Singapore"),
    ("SK", "Slovakia"), ("TH", "Thailand"), ("TR", "Turkey"),
    ("TW", "Taiwan"), ("UA", "Ukraine"), ("US", "United States"),
    ("VN", "Vietnam"), ("ZA", "South Africa")
]
ALL_DATACENTER_NON_PREMIUM = sorted(ALL_DATACENTER_NON_PREMIUM, key=lambda x: x[1])

DATACENTER_CODE_TO_NAME = {code.upper(): name for code, name in ALL_DATACENTER_NON_PREMIUM}
DATACENTER_NAME_TO_CODE = {name.lower(): code.upper() for code, name in ALL_DATACENTER_NON_PREMIUM}

TIER_1_CODES = {"US", "GB", "CA", "DE", "FR", "ES", "IT", "NL", "JP", "SG", "AU", "SE", "MX", "BR", "KR"}
TIER_2_CODES = {"AE", "AR", "AT", "BE", "CH", "CL", "CO", "CZ", "DK", "FI", "HK", "IE", "IL", "IN", "NO", "NZ", "PL", "PT", "RO", "SA", "TR", "TW", "ZA"}
TIER_3_CODES = {"BG", "GR", "HR", "HU", "ID", "MY", "PH", "RS", "SK", "TH", "UA", "VN"}

def parse_country_config(raw_cfg: str):
    inc, exc = set(), set()
    warnings = set()
    if not raw_cfg.strip():
        return inc, exc

    tokens = [t.strip() for t in re.split(r",(?![^{]*\})", raw_cfg) if t.strip()]
    for token in tokens:
        match = re.match(r"^(include|exclude)\{([^\}]+)\}$", token, re.IGNORECASE)
        if match:
            mode = match.group(1).lower()
            items = [c.strip().lower() for c in match.group(2).split(",") if c.strip()]
            for item in items:
                resolved_code = item.upper()
                if item.upper() in DATACENTER_CODE_TO_NAME:
                    resolved_code = item.upper()
                elif item in DATACENTER_NAME_TO_CODE:
                    resolved_code = DATACENTER_NAME_TO_CODE[item]
                else:
                    warnings.add(resolved_code)

                if mode == "include":
                    inc.add(resolved_code)
                else:
                    exc.add(resolved_code)
        else:
            items = [c.strip().lower() for c in token.split(",") if c.strip()]
            for item in items:
                resolved_code = item.upper()
                if item.upper() in DATACENTER_CODE_TO_NAME:
                    resolved_code = item.upper()
                elif item in DATACENTER_NAME_TO_CODE:
                    resolved_code = DATACENTER_NAME_TO_CODE[item]
                else:
                    warnings.add(resolved_code)
                inc.add(resolved_code)

    contradictions = inc.intersection(exc)
    if contradictions:
        exc.difference_update(contradictions)
    return inc, exc

RAW_COUNTRY_CONF = get_env("COUNTRY", get_env("COUNTRIES", ""))
INC_COUNTRIES, EXC_COUNTRIES = parse_country_config(RAW_COUNTRY_CONF)
ROUTE_CHANGE_ON_UNAVAILABLE = parse_bool("ROUTE_CHANGE_ON_UNAVAILABLE", parse_bool("COUNTRIES_ROUTE_CHANGE", False))

def build_datacenter_country_pools():
    t1_pool, t2_pool, t3_pool = [], [], []
    for code, name in ALL_DATACENTER_NON_PREMIUM:
        if code in EXC_COUNTRIES:
            continue
        if INC_COUNTRIES and code not in INC_COUNTRIES:
            continue
        
        entry = (code, code.lower(), name)
        if code in TIER_1_CODES:
            t1_pool.append(entry)
        elif code in TIER_2_CODES:
            t2_pool.append(entry)
        else:
            t3_pool.append(entry)
    
    return t1_pool, t2_pool, t3_pool

VALID_T1, VALID_T2, VALID_T3 = build_datacenter_country_pools()
TOTAL_VALID = len(VALID_T1) + len(VALID_T2) + len(VALID_T3)

def pick_datacenter_country(used_countries_in_cycle: set):
    tiers = []
    if VALID_T1: tiers.append(("T1", T1_RATIO, VALID_T1))
    if VALID_T2: tiers.append(("T2", T2_RATIO, VALID_T2))
    if VALID_T3: tiers.append(("T3", T3_RATIO, VALID_T3))

    if tiers:
        total_weight = sum(w for t, w, pool in tiers)
        for _ in range(20):
            roll = random.uniform(0, total_weight)
            current = 0
            for t_name, w, pool in tiers:
                current += w
                if roll <= current:
                    c_up, c_low, name = random.choice(pool)
                    if c_up not in used_countries_in_cycle or TOTAL_VALID <= len(used_countries_in_cycle):
                        used_countries_in_cycle.add(c_up)
                        return f"{t_name}-{c_up}", c_up, c_low
                    break
        t_name, _, pool = tiers[-1]
        c_up, c_low, _ = random.choice(pool)
        used_countries_in_cycle.add(c_up)
        return f"{t_name}-{c_up}", c_up, c_low

    if not ROUTE_CHANGE_ON_UNAVAILABLE:
        if INC_COUNTRIES:
            locked = sorted(list(INC_COUNTRIES))[0]
            used_countries_in_cycle.add(locked)
            return f"LOCKED-{locked}", locked, locked.lower()
        return "UNSET", "", ""

    fallback_pool = [(c, c.lower(), n) for c, n in ALL_DATACENTER_NON_PREMIUM if c not in EXC_COUNTRIES]
    if fallback_pool:
        c_up, c_low, _ = random.choice(fallback_pool)
        used_countries_in_cycle.add(c_up)
        return f"FALLBACK-{c_up}", c_up, c_low

    return "DC", "US", "us"


# ---------------------------------------------------------
# Dynamic Slug & Link Engine
# ---------------------------------------------------------
class CycleLinkDispatcher:
    def __init__(self, raw_links: list, short_template: str):
        self.raw_links = raw_links
        self.short_template = short_template
        self.full_links = []
        self.explicit_short_links = []
        self._categorize()

    def _categorize(self):
        for item in self.raw_links:
            item = item.strip()
            if not item:
                continue

            parsed = urllib.parse.urlparse(item)
            is_url = bool(parsed.scheme and parsed.netloc)
            slug = None

            if is_url:
                query = urllib.parse.parse_qs(parsed.query)
                for param in ("via", "ref", "r", "referral", "aff", "code", "id", "tag"):
                    if param in query and query[param]:
                        slug = query[param][0]
                        break
                if not slug:
                    path_parts = [p for p in parsed.path.split("/") if p]
                    if path_parts:
                        slug = path_parts[-1]

                if not parsed.query and slug and self.short_template and self.short_template in item:
                    self.explicit_short_links.append((item, slug))
                else:
                    self.full_links.append((item, slug if slug else "link"))
            else:
                self.full_links.append((item, item))

    def generate_cycle_batch(self, worker_count: int):
        used_slugs, used_urls, tasks = set(), set(), []
        pool = []
        for url, sl in self.full_links:
            pool.append(("FULL", url, sl))
        for url, sl in self.explicit_short_links:
            pool.append(("SHORT_CUSTOM", url, sl))

        random.shuffle(pool)

        def make_short_url(slug: str, reference: str) -> str:
            if not reference:
                return ""
            if "{slug}" in reference:
                return reference.replace("{slug}", slug)
            parsed = urllib.parse.urlparse(reference)
            base_domain = f"{parsed.scheme}://{parsed.netloc}"
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) > 1:
                prefix = "/".join(parts[:-1])
                return f"{base_domain}/{prefix}/{slug}"
            return f"{base_domain}/{slug}"

        for kind, raw_target, slug in pool:
            if len(tasks) >= worker_count:
                break
            if len(pool) >= worker_count:
                if slug in used_slugs or raw_target in used_urls:
                    continue

            final_url = raw_target
            ltype = "DIRECT"

            if kind == "SHORT_CUSTOM":
                ltype = "SHORT (OWN)"
            elif self.short_template and slug:
                roll = random.random()
                short_cand = make_short_url(slug, self.short_template)
                if roll >= 0.86 and short_cand:
                    final_url = short_cand
                    ltype = "SHORT (/)"
                else:
                    ltype = "FULL (?)"

            used_slugs.add(slug)
            used_urls.add(final_url)
            tasks.append((final_url, slug, ltype))

        while len(tasks) < worker_count and pool:
            kind, raw_target, slug = random.choice(pool)
            tasks.append((raw_target, slug, kind))

        return tasks


# ---------------------------------------------------------
# Managed Key Pool
# ---------------------------------------------------------
class ManagedKey:
    def __init__(self, name: str, token: str):
        self.name = name.strip()
        self.token = token.strip()
        self.masked = f"{self.token[:4]}...{self.token[-4:]}" if len(self.token) >= 8 else self.token
        self.tag = f"{self.name} [{self.masked}]"

class KeyPoolManager:
    def __init__(self, raw_str: str):
        self.active_keys = []
        self.dead_keys = []
        self.index = 0

        entries = [k.strip() for k in raw_str.split(",") if k.strip()]
        for idx, entry in enumerate(entries, start=1):
            if ":" in entry:
                name, token = entry.split(":", 1)
                self.active_keys.append(ManagedKey(name, token))
            else:
                self.active_keys.append(ManagedKey(f"Key#{idx}", entry))

    def get_key(self) -> ManagedKey:
        if not self.active_keys:
            return None
        key = self.active_keys[self.index % len(self.active_keys)]
        self.index = (self.index + 1) % len(self.active_keys)
        return key

    def mark_dead(self, key_obj: ManagedKey, reason: str):
        if key_obj in self.active_keys:
            self.active_keys.remove(key_obj)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.dead_keys.append((key_obj, reason, ts))

            print("\n" + "#" * 70)
            print(f" [PINNED ALERT] API KEY DIED / EXHAUSTED CREDITS")
            print(f"  Key Identifier : {key_obj.tag}")
            print(f"  Death Time     : {ts}")
            print(f"  Confirmed Cause: {reason}")
            print(f"  Active Remaining: {len(self.active_keys)} key(s)")
            print("#" * 70 + "\n")

    def print_pinned_status(self):
        if not self.dead_keys:
            return
        print("-" * 70)
        print(" [PINNED AUDIT] PERMANENTLY DEAD KEYS:")
        for k_obj, reason, ts in self.dead_keys:
            print(f"  -> {k_obj.tag} | Died: {ts} | Reason: {reason}")
        print("-" * 70)


# ---------------------------------------------------------
# Global Parameters & Dispatcher Initialization
# ---------------------------------------------------------
RAW_KEYS = get_env("API_KEYS", get_env("SCRAPERAPI_KEYS", ""))
pool = KeyPoolManager(RAW_KEYS)

PARALLELBOT = parse_bool("PARALLELBOT", default=True)
CYCLE_GAP = parse_num("CYCLE_GAP", default=0.0)

FOLLOW_REDIRECT = parse_bool("FOLLOW_REDIRECT", default=False)
MATCH_IP_LANGUAGE = parse_bool("LANGUAGE_ACCORDING_TO_IP", default=False)
WORKER_MIN, WORKER_MAX = parse_range("WORKER_COUNT_RANGE", 5, 7)
GAP_MIN, GAP_MAX = parse_range("WORKER_GAP_RANGE", 8.0, 12.0)
CYCLE_MIN, CYCLE_MAX = parse_range("CYCLE_INTERVAL_RANGE", 50.0, 70.0)
POST_WAIT_MIN, POST_WAIT_MAX = parse_range("POST_RESPONSE_WAIT_RANGE", 5.0, 15.0)

BROWSER_RENDERING = get_env("BROWSER_RENDERING", "true").lower()
RESIDENTIAL = parse_bool("RESIDENTIAL", default=False)

# Sticky Session: Default to TRUE
STICKY_SESSION = parse_bool("STICKY_SESSION", default=True)

# Generate a high-range base salt for distinct bot pools
BOT_POOL_SALT = random.randint(100000, 999999)

def get_bot_session_id(bot_id: int) -> str:
    session_num = 10000000 + (bot_id * 500000) + (BOT_POOL_SALT % 500000)
    return str(session_num)

DEFAULT_REFERRERS = [
    "none",
    "https://www.google.com/",
    "https://t.co/",
    "https://www.facebook.com",
    "https://www.instagram.com",
    "https://www.linkedin.com",
    "https://www.pinterest.com",
    "https://www.reddit.com",
    "https://www.youtube.com",
    "https://www.tiktok.com"
]
RAW_REFERRERS = parse_list("REFERRERS", DEFAULT_REFERRERS)
REFERRERS = RAW_REFERRERS if RAW_REFERRERS else DEFAULT_REFERRERS

INPUT_LINKS = parse_list("LINKS", parse_list("TARGET_URLS", parse_list("TARGET_SLUGS", [])))
SHORT_TEMPLATE = get_env("SHORT_LINK_TEMPLATE")
LINK_DISPATCHER = CycleLinkDispatcher(INPUT_LINKS, SHORT_TEMPLATE)

def calculate_credits(render_flag: str, is_residential: bool) -> int:
    is_render = render_flag == "true"
    if is_residential and is_render:
        return 25
    elif is_residential or is_render:
        return 10
    return 1


# ---------------------------------------------------------
# Worker Bot Execution
# ---------------------------------------------------------
def execute_bot(bot_id: int, total_bots: int, target_url: str, slug: str, ltype: str, stagger_delay: float, selected_cat: str, selected_ua: str, os_label: str, tier_label: str, code_upper: str, code_lower: str):
    bot_start_time = time.time()
    time.sleep(stagger_delay)

    key_obj = pool.get_key()
    if not key_obj:
        return

    expected_credits = calculate_credits(BROWSER_RENDERING, RESIDENTIAL)
    truncated_target = truncate_url(target_url, head_len=18, tail_len=14)

    if STICKY_SESSION:
        session_id = get_bot_session_id(bot_id)
        session_tag = f"Sticky:{session_id}"
    else:
        session_id = str(random.randint(1000000, 9999999))
        session_tag = "Rotate"

    # Evaluate dynamic scroll and button execution for this bot
    bot_scroll_timing, bot_scroll_pct = resolve_bot_scroll()
    needs_automation = bool((BUTTON_CLICK_ENABLE and BUTTON_TARGETS) or (bot_scroll_timing != "off"))
    
    runtime_render = BROWSER_RENDERING
    custom_js = ""
    tags = []

    if needs_automation:
        custom_js = generate_js_scenario_script(bot_scroll_timing, bot_scroll_pct)
        runtime_render = "true"  # Requires headless browser for scenario execution
        if BUTTON_CLICK_ENABLE and BUTTON_TARGETS:
            tags.append("[BTN-AUTO]")
        if bot_scroll_timing != "off":
            tags.append(f"[SCRL:{bot_scroll_timing}@{bot_scroll_pct}%]")

    extra_tags_str = " " + " ".join(tags) if tags else ""

    params = {
        "api_key": key_obj.token,
        "device_type": selected_cat,
        "render": runtime_render,
        "keep_headers": "true",
        "session_number": session_id,
        "follow_redirect": "true" if FOLLOW_REDIRECT else "false",
        "retry": "false",
        "url": target_url
    }
    if custom_js:
        params["js_scenario"] = custom_js
    if code_upper:
        params["country_code"] = code_upper.lower()
    if RESIDENTIAL:
        params["premium"] = "true"

    url = f"http://api.scraperapi.com?{urllib.parse.urlencode(params)}"
    
    if MATCH_IP_LANGUAGE and code_lower and code_upper:
        dynamic_lang = f"{code_lower}-{code_upper},{code_lower};q=0.9,en-US;q=0.8,en;q=0.5"
    else:
        dynamic_lang = "en-US,en;q=0.9"

    headers = {
        "User-Agent": selected_ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": dynamic_lang,
        "Connection": "close"
    }
    
    if selected_cat == "mobile":
        headers["Sec-CH-UA-Mobile"] = "?1"
        headers["Sec-CH-UA-Platform"] = '"Android"' if "Android" in selected_ua else '"iOS"'
    else:
        headers["Sec-CH-UA-Mobile"] = "?0"

    chosen_referrer = random.choice(REFERRERS)
    if chosen_referrer and chosen_referrer.lower() not in ("none", "direct", "empty"):
        headers["Referer"] = chosen_referrer
        ref_display = chosen_referrer
    else:
        ref_display = "Direct"

    req = urllib.request.Request(url, headers=headers)

    try:
        # Full 180s timeout prevents dropping the connection while ScraperAPI processes clicks/scrolls
        with urllib.request.urlopen(req, timeout=180) as resp:
            # Drain full response body to prevent closing before bot actions finish
            response_chunks = []
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                response_chunks.append(chunk)
            full_body = b"".join(response_chunks)
            body_size_kb = len(full_body) / 1024.0

            actual_credits = resp.headers.get("sa-credit-cost", str(expected_credits))
            exit_country = (
                resp.headers.get("sa-proxy-country") 
                or resp.headers.get("sa-country") 
                or resp.headers.get("x-scraperapi-country") 
                or ""
            ).strip().upper()

            if INC_COUNTRIES and code_upper and exit_country and exit_country != code_upper:
                print(
                    f"[Bot-{bot_id}/{total_bots}] [GEO REJECTED] [Requested: {code_upper} | ScraperAPI returned: {exit_country}] "
                    f"Traffic ignored to protect your stats. [Credits: ~{actual_credits}] [{key_obj.tag}]"
                )
            else:
                country_tag = exit_country if exit_country else code_upper
                print(
                    f"[Bot-{bot_id}/{total_bots}] [{tier_label}-{country_tag}] [{selected_cat.upper()}:{os_label}] "
                    f"[{ltype} {slug}] [{session_tag}]{extra_tags_str} [Target: {truncated_target}] [Ref: {ref_display}] "
                    f"[Body: {body_size_kb:.1f} KB] [Credits: ~{actual_credits} cost] [{key_obj.tag}] -> HTTP {resp.status} OK"
                )

    except urllib.error.HTTPError as e:
        try:
            raw_detail = e.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            raw_detail = "Could not read error body"
            
        if e.code in (401, 403):
            reason_msg = f"HTTP {e.code} - Actual ScraperAPI Error: {raw_detail}"
            pool.mark_dead(key_obj, reason_msg)
        elif e.code == 429:
            print(f"[Bot-{bot_id}/{total_bots}] [{key_obj.tag}] [Target: {truncated_target}] [Credits: 0] [TRANSIENT] HTTP 429 Rate limit exceeded")
        else:
            print(f"[Bot-{bot_id}/{total_bots}] [{key_obj.tag}] [Target: {truncated_target}] [Credits: 0] [WARN] HTTP {e.code}: {raw_detail}")

    except Exception as ex:
        print(f"[Bot-{bot_id}/{total_bots}] [{key_obj.tag}] [Target: {truncated_target}] [Credits: 0] [CLIENT ERROR]: {str(ex)}")

    post_wait = random.uniform(POST_WAIT_MIN, POST_WAIT_MAX)
    gap_wait = random.uniform(GAP_MIN, GAP_MAX)
    time.sleep(post_wait + gap_wait)

    duration = time.time() - bot_start_time
    print(f"[Bot-{bot_id}/{total_bots}] Finished fully and safely detached (Work Duration: {duration:.1f}s)")


# ---------------------------------------------------------
# Main Execution Loop
# ---------------------------------------------------------
def main():
    print("==================================================")
    print("      UNIVERSAL SCRAPER ENGINE RUNTIME            ")
    print("==================================================")

    if not INPUT_LINKS:
        print("\n" + "!" * 70)
        print(" [CONFIG ERROR] NO TARGET LINKS DETECTED!")
        print(" Supply target links via LINKS environment variable.")
        print("!" * 70 + "\n")
        sys.exit(1)

    inc_display = ", ".join(sorted(list(INC_COUNTRIES))) if INC_COUNTRIES else "None (ALL allowed)"
    exc_display = ", ".join(sorted(list(EXC_COUNTRIES))) if EXC_COUNTRIES else "None"
    
    print("---------------- COUNTRY CONFIGURATION -----------")
    print(f"Included Countries      : {inc_display}")
    print(f"Excluded Countries      : {exc_display}")
    print(f"Working Datacenter Pool : {TOTAL_VALID} active (T1: {len(VALID_T1)} | T2: {len(VALID_T2)} | T3: {len(VALID_T3)})")
    print(f"Tier Selection Ratios   : T1={T1_RATIO:.1f}% | T2={T2_RATIO:.1f}% | T3={T3_RATIO:.1f}%")
    print(f"Route Change Fallback   : {ROUTE_CHANGE_ON_UNAVAILABLE}")
    print("--------------------------------------------------")
    print(f"Follow Redirects        : {FOLLOW_REDIRECT}")
    print(f"Total Active Keys       : {len(pool.active_keys)}")
    print(f"Parallel Bots Enabled   : {PARALLELBOT}")
    print(f"Browser Rendering (JS)  : {BROWSER_RENDERING}")
    print(f"Residential Proxies     : {'ENABLED (premium=true)' if RESIDENTIAL else 'DISABLED (Standard DataCenter)'}")
    print(f"Sticky Sessions         : {'ENABLED (Each bot keeps its own locked IP)' if STICKY_SESSION else 'DISABLED (Rotating)'}")
    print(f"Scroll Engine           : {'OFF' if PARSED_SCROLL_MODE == 'off' else f'{PARSED_SCROLL_MODE.upper()} ({SCROLL_MIN_PCT}% - {SCROLL_MAX_PCT}%)'}")
    print(f"Button Auto-Click L1    : {'ENABLED' if BUTTON_CLICK_ENABLE else 'DISABLED'} (Targets: {BUTTON_TARGETS})")
    print(f"Button L1 Settings      : Mode={BUTTON_SELECTION_MODE} | Count={BUTTON_CLICK_COUNT} | PageLoadWait={BUTTON_PAGE_LOAD_WAIT}")
    print(f"Button Auto-Click L2    : {'ENABLED' if (BUTTON_CLICK_ENABLE and BUTTON_L2_CLICK_ENABLE) else 'DISABLED'} (Targets: {BUTTON_L2_TARGETS})")
    print(f"Button L2 Settings      : Mode={BUTTON_L2_SELECTION_MODE} | Count={BUTTON_L2_CLICK_COUNT} | Delay={L2_DELAY_MIN}s-{L2_DELAY_MAX}s")
    print(f"Est. Credits Per Hit    : ~{calculate_credits(BROWSER_RENDERING, RESIDENTIAL)} credits")
    print("==================================================\n")

    cycle_num = 1

    try:
        while True:
            if not pool.active_keys:
                print("\n" + "!" * 70)
                print(" [SHUTDOWN] ALL CONFIGURED KEYS HAVE EXHAUSTED CREDITS.")
                pool.print_pinned_status()
                print("!" * 70 + "\n")
                sys.exit(0)

            cycle_start = time.time()
            worker_count = random.randint(int(WORKER_MIN), int(WORKER_MAX))
            target_cycle_time = random.uniform(CYCLE_MIN, CYCLE_MAX)

            print(f"\n--- [Cycle #{cycle_num}] Starting {worker_count} bots | Target Window: {target_cycle_time:.1f}s | Active Keys: {len(pool.active_keys)} ---")

            tasks = LINK_DISPATCHER.generate_cycle_batch(worker_count)
            
            used_uas_this_cycle = set()
            bot_ua_assignments = [resolve_cycle_ua(used_uas_this_cycle) for _ in range(worker_count)]
            
            used_countries_this_cycle = set()
            bot_country_assignments = [pick_datacenter_country(used_countries_this_cycle) for _ in range(worker_count)]

            # -------------------------------------------------------------
            # STRICT EXECUTION BARRIER: AWAIT ALL ASSIGNED BOTS COMPLETION
            # -------------------------------------------------------------
            if PARALLELBOT:
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = []
                    for idx, (url, slug, ltype) in enumerate(tasks):
                        stagger = idx * random.uniform(GAP_MIN, GAP_MAX)
                        cat, ua_str, ua_label = bot_ua_assignments[idx]
                        tier_label, code_upper, code_lower = bot_country_assignments[idx]
                        
                        futures.append(
                            executor.submit(
                                execute_bot, idx + 1, worker_count, url, slug, ltype, stagger, 
                                cat, ua_str, ua_label, tier_label, code_upper, code_lower
                            )
                        )
                    # Guaranteed: All threads MUST complete their response + post waits before leaving this block
                    concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
            else:
                for idx, (url, slug, ltype) in enumerate(tasks):
                    stagger = idx * random.uniform(GAP_MIN, GAP_MAX)
                    cat, ua_str, ua_label = bot_ua_assignments[idx]
                    tier_label, code_upper, code_lower = bot_country_assignments[idx]
                    
                    execute_bot(idx + 1, worker_count, url, slug, ltype, stagger, 
                                cat, ua_str, ua_label, tier_label, code_upper, code_lower)

            elapsed = time.time() - cycle_start
            wait_time = target_cycle_time - elapsed

            pool.print_pinned_status()

            if wait_time > 0 and pool.active_keys:
                print(f"--- [Cycle #{cycle_num} Complete] All {worker_count} bots ended cleanly (Active: {elapsed:.1f}s) | Pausing {wait_time:.1f}s to reach target window ---")
                time.sleep(wait_time)
            elif pool.active_keys:
                print(f"--- [Cycle #{cycle_num} Complete] All {worker_count} bots ended cleanly (Total: {elapsed:.1f}s, exceeded target {target_cycle_time:.1f}s) | Moving to next cycle ---")

            if CYCLE_GAP > 0 and pool.active_keys:
                print(f"--- Applying explicit Cycle Gap of {CYCLE_GAP}s ---")
                time.sleep(CYCLE_GAP)

            cycle_num += 1

    except KeyboardInterrupt:
        print("\nShutdown signal caught. Exiting cleanly.")
        sys.exit(0)

if __name__ == "__main__":
    main()