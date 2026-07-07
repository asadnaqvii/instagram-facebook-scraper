"""Self-healing selector logic + LLM content analysis.

The healer is the bridge between the extraction layer and Ollama:

  resolve(page, key, ...)  — try cached/built-in selectors; if all miss and
                             healing is on, hand the DOM snippet to the LLM,
                             validate its proposal against the live page, and
                             on success cache it for next time.

  analyze(text, ctx)       — optional sentiment/entity/topic enrichment.

Every path is safe when Ollama is down: resolve() just returns whatever the
heuristic selectors found (possibly nothing), and analyze() returns None.
"""
from __future__ import annotations

import re
from typing import Optional

from playwright.async_api import Page

from .. import config
from .ollama_client import OllamaClient
from .selector_store import SelectorStore


class ResolvedSelector:
    """Result of a resolve() call."""

    def __init__(self, selector: Optional[str], match_count: int, healed: bool):
        self.selector = selector
        self.match_count = match_count
        self.healed = healed

    @property
    def found(self) -> bool:
        return self.selector is not None and self.match_count > 0


class SelectorHealer:
    def __init__(
        self,
        store: SelectorStore | None = None,
        client: OllamaClient | None = None,
    ):
        self.store = store or SelectorStore()
        self.client = client or OllamaClient()
        self.healing_enabled = config.LLM_SELECTOR_HEALING
        self.analysis_enabled = config.LLM_CONTENT_ANALYSIS
        self._heal_attempts: dict[str, int] = {}  # per-session guard

    # ── selector resolution ──────────────────────────────────────────

    async def resolve(
        self,
        page: Page,
        key: str,
        *,
        scope_selector: Optional[str] = None,
        description: Optional[str] = None,
        min_matches: int = 1,
        expect: Optional[str] = None,
    ) -> ResolvedSelector:
        """Return a working selector for `key` on the current page.

        Tries the store's candidates in order. If none match and healing is
        enabled + Ollama is up, asks the LLM for a fresh selector, validates
        it, caches it on success.

        `scope_selector` optionally restricts matching to a subtree.
        `description` is a human hint that helps the LLM (e.g. "the like
        count number under a post").
        `expect` gates what counts as a valid match, so a selector that hits
        the wrong node is rejected rather than cached:
            "text"  — matched element must contain non-trivial text
            "digit" — matched element text must contain a digit
            "link"  — matched element must be/contain an <a href>
        This is what stops the healer from "succeeding" on, say, an <img> when
        it was asked for a caption.
        """
        # 1. Try known selectors.
        for sel in self.store.candidates(key):
            count = await self._count(page, sel, scope_selector)
            if count >= min_matches and await self._validate(page, sel, scope_selector, expect):
                return ResolvedSelector(sel, count, healed=False)

        # 2. Heal via LLM (once per key per session, to bound cost).
        if not (self.healing_enabled and self.client.is_available()):
            return ResolvedSelector(None, 0, healed=False)
        if self._heal_attempts.get(key, 0) >= 1:
            return ResolvedSelector(None, 0, healed=False)
        self._heal_attempts[key] = self._heal_attempts.get(key, 0) + 1

        healed = await self._heal(page, key, scope_selector, description, min_matches, expect)
        if healed:
            return healed

        # 3. A previously-healed selector that no longer matches is dead weight.
        self.store.clear_heal(key)
        return ResolvedSelector(None, 0, healed=False)

    async def _validate(self, page: Page, selector: str, scope: Optional[str], expect: Optional[str]) -> bool:
        """Boolean form of the expect contract (used for built-in candidates)."""
        return (await self._match_score(page, selector, scope, expect)) > 0

    async def _match_score(self, page: Page, selector: str, scope: Optional[str], expect: Optional[str]) -> int:
        """Score how well the FIRST matched element fits `expect`.

        Returns 0 when the contract is not met (reject), or a positive score
        used to pick the best among several passing candidates. For "text" the
        score is the text length, so the caption (long) beats a UI label like
        '48,213 likes' (short) even though both technically contain text."""
        if not expect:
            return 1
        try:
            info = await page.evaluate(
                """([scopeSel, sel]) => {
                    const root = scopeSel ? document.querySelector(scopeSel) : document;
                    if (!root) return null;
                    let el; try { el = root.querySelector(sel); } catch (e) { return null; }
                    if (!el) return null;
                    const text = (el.textContent || '').trim();
                    const hasLink = el.tagName === 'A' || !!el.querySelector('a[href]') || !!el.closest('a[href]');
                    return { len: text.length, hasDigit: /\\d/.test(text), hasLink };
                }""",
                [scope, selector],
            )
        except Exception:
            return 0
        if not info:
            return 0
        if expect == "text":
            # Require more than a short UI label; longer text scores higher.
            return info["len"] if info["len"] >= 25 else 0
        if expect == "digit":
            return 1 if info["hasDigit"] else 0
        if expect == "link":
            return 1 if info["hasLink"] else 0
        return 1

    async def _count(self, page: Page, selector: str, scope: Optional[str]) -> int:
        """How many nodes a selector matches (0 on any error)."""
        try:
            if scope:
                return await page.evaluate(
                    """([scopeSel, sel]) => {
                        const root = document.querySelector(scopeSel);
                        if (!root) return 0;
                        try { return root.querySelectorAll(sel).length; }
                        catch (e) { return -1; }
                    }""",
                    [scope, selector],
                )
            return await page.evaluate(
                """(sel) => { try { return document.querySelectorAll(sel).length; }
                              catch (e) { return -1; } }""",
                selector,
            )
        except Exception:
            return 0

    async def _heal(
        self,
        page: Page,
        key: str,
        scope: Optional[str],
        description: Optional[str],
        min_matches: int,
        expect: Optional[str] = None,
    ) -> Optional[ResolvedSelector]:
        html = await self._capture_html(page, scope)
        if not html:
            return None

        hint = description or key.replace(".", " ")
        prompt = _HEAL_PROMPT.format(target=key, description=hint, html=html[:12000])

        # Try the fast model first, then escalate to a stronger one if every
        # proposal fails validation. llama3.2 is quick but frequently emits
        # malformed or class-brittle selectors; the escalation model fixes the
        # hard cases. Each proposal is sanitized before it touches the page,
        # and must satisfy the `expect` contract so a selector that hits the
        # wrong element is never cached.
        models_to_try: list[Optional[str]] = [None]  # None = default (fast) model
        heal_model = getattr(config, "OLLAMA_HEAL_MODEL", "")
        if heal_model and heal_model != self.client.model:
            models_to_try.append(heal_model)

        best: Optional[tuple[str, int, int]] = None  # (selector, count, score)
        for model in models_to_try:
            proposal = self.client.generate_json(
                prompt, temperature=0.0, num_predict=220, model=model
            )
            candidates = _sanitize_selectors(_extract_selector_list(proposal))
            for sel in candidates:
                count = await self._count(page, sel, scope)
                if count < min_matches:
                    continue
                score = await self._match_score(page, sel, scope, expect)
                if score <= 0:
                    continue  # failed the expect contract
                if best is None or score > best[2]:
                    best = (sel, count, score)
            # If this model produced a solid match, take it — don't burn the
            # escalation model unnecessarily.
            if best is not None:
                sel, count, _ = best
                self.store.record_heal(key, sel)
                return ResolvedSelector(sel, count, healed=True)
        return None

    async def _capture_html(self, page: Page, scope: Optional[str]) -> Optional[str]:
        """Grab a trimmed HTML snippet to feed the LLM (attrs kept, noise cut)."""
        try:
            return await page.evaluate(
                """(scopeSel) => {
                    const root = scopeSel ? document.querySelector(scopeSel) : document.body;
                    if (!root) return '';
                    // Clone and strip heavy/irrelevant nodes to keep the prompt small.
                    const clone = root.cloneNode(true);
                    clone.querySelectorAll('script,style,svg,noscript,link,iframe').forEach(n => n.remove());
                    let html = clone.outerHTML || '';
                    // Collapse whitespace.
                    html = html.replace(/\\s+/g, ' ');
                    return html;
                }""",
                scope,
            )
        except Exception:
            return None

    # ── content analysis ─────────────────────────────────────────────

    def analyze(self, text: str, context: Optional[dict] = None) -> Optional[dict]:
        """Sentiment + entities + topics for a post. None when unavailable."""
        if not (self.analysis_enabled and self.client.is_available()):
            return None
        if not text or len(text.strip()) < 12:
            return None
        context = context or {}
        prompt = _ANALYSIS_PROMPT.format(
            platform=context.get("platform", ""),
            author=context.get("author", ""),
            text=text[:1500],
        )
        parsed = self.client.generate_json(prompt, temperature=0.1, num_predict=400)
        if not isinstance(parsed, dict):
            return None
        return {
            "sentiment": parsed.get("sentiment", "neutral"),
            "sentiment_score": _clamp(parsed.get("score"), -1, 1),
            "entities_people": _str_list(parsed.get("people")),
            "entities_orgs": _str_list(parsed.get("organizations")),
            "entities_locations": _str_list(parsed.get("locations")),
            "topics": _str_list(parsed.get("topics")),
            "language": parsed.get("language"),
            "model_used": self.client.model,
        }


# ── prompts ──────────────────────────────────────────────────────────

_HEAL_PROMPT = """You are a CSS-selector expert helping a web scraper recover \
after a site changed its HTML markup.

Target id: {target}
What to find: {description}

Below is the CURRENT HTML of the relevant region. Return CSS selectors \
(usable with document.querySelectorAll) that select ONLY the described \
element(s).

STRICT RULES — follow all of them:
1. Prefer STABLE attributes: attribute selectors on href/role/aria-label/dir/\
data-* and tag structure. Example: a[href*="liked_by"] span
2. AVOID class names that look random/generated (e.g. x1i10hfl, _ap3a, \
html-span, xdj266r). They change constantly — do not rely on them.
3. Attribute selectors MUST use square brackets and quotes: \
a[href*="/reel/"], div[role="article"]. Never write `a href*='x'` without \
brackets — that is invalid CSS.
4. Give 2-4 selectors, most specific/reliable first.
5. Each selector must be valid CSS that querySelectorAll accepts.

Respond with ONLY a JSON object, nothing else:
{{"selectors": ["best selector", "fallback 1", "fallback 2"]}}

HTML:
{html}

JSON:"""

_ANALYSIS_PROMPT = """Analyze this social media post. Platform: {platform}. \
Author: {author}.

Post: "{text}"

Respond with ONLY a JSON object in exactly this shape:
{{"sentiment": "very_negative|negative|neutral|positive|very_positive", \
"score": <number -1..1>, "people": [], "organizations": [], "locations": [], \
"topics": [], "language": "<ISO 639-1 code>"}}

JSON:"""


# ── parsing helpers ──────────────────────────────────────────────────


def _sanitize_selectors(selectors: list[str]) -> list[str]:
    """Repair common malformed selectors from LLMs and drop hopeless ones.

    The frequent failure is an unbracketed attribute selector, e.g.
    `a href*='liked_by/' span` — valid-looking to a model, rejected by CSS.
    We wrap bare `attr*='v'` / `attr='v'` fragments in brackets. Selectors we
    can't rescue are returned as-is (the live-page validation still gates
    them, so a bad one simply fails to match rather than caching garbage)."""
    out: list[str] = []
    seen: set[str] = set()
    # An unbracketed attribute fragment with optional whitespace before it:
    #   `a href*='x'`  ->  `a[href*='x']`  (the space is the bug, not a combinator)
    # We consume the leading space too, so legitimate descendant combinators
    # (e.g. `section [role="x"]` — bracket already present) are left untouched.
    bare_attr = re.compile(r"\s+([a-zA-Z-]+)([*^$~|]?=)(['\"])(.*?)\3")

    for sel in selectors:
        if not sel or not isinstance(sel, str):
            continue
        fixed = sel.strip().strip(",").strip()
        if not fixed:
            continue
        fixed = bare_attr.sub(
            lambda m: f"[{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{m.group(3)}]",
            fixed,
        )
        if fixed and fixed not in seen:
            seen.add(fixed)
            out.append(fixed)
    return out


def _extract_selector_list(proposal) -> list[str]:
    if isinstance(proposal, dict):
        sels = proposal.get("selectors") or proposal.get("selector")
        if isinstance(sels, str):
            return [sels]
        if isinstance(sels, list):
            return [s for s in sels if isinstance(s, str) and s.strip()]
    if isinstance(proposal, list):
        return [s for s in proposal if isinstance(s, str) and s.strip()]
    if isinstance(proposal, str) and proposal.strip():
        return [proposal.strip()]
    return []


def _str_list(value, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def _clamp(value, lo: float, hi: float) -> Optional[float]:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return None
