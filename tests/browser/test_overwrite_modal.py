"""Cycle 18 v2 — Playwright behavior tests for the example-overwrite modal.

These cover the four scenarios you asked about, in a real Chromium with the
real DOM + real CSS:

  1. Empty textarea + click chip  → sample loads silently, no modal.
  2. User content + click chip    → modal shown, content NOT yet overwritten.
  3. Click "保留原文"             → modal closes, content unchanged.
  4. Click "套用示例"             → modal closes, sample replaces content.

Run with:
    python -m playwright install chromium   # one-time
    pytest tests/browser/                   # excluded from default unittest run

The pure-Python regression-guard tests in tests/test_app.py (class
ExampleOverwriteModalRegressionTests) cover the same scenarios via static
code-shape assertions and run on every CI invocation. These browser tests
are the ground-truth behavior layer — slow but accurate.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def page_at_workbench(live_app, page):
    """Browser page navigated to the workbench, ready for chip clicks.

    We wait for `domcontentloaded` instead of `load` because the daily-phrase
    card kicks off ~10 stub-LLM rewrites async — those keep the `load` event
    pending and timeout the test. The chip handlers are wired during the same
    DOMContentLoaded synchronous init pass, so we can interact immediately.

    2026-08: the app now boots on the daily-phrase landing page (v3 随四时
    design); the workbench lives at #workbench. The old fixture went to `/`
    and waited for chips that are display:none on the daily route — every
    test timed out in setup."""
    page.goto(live_app + "/#workbench", wait_until="domcontentloaded")
    page.wait_for_selector(".chip[data-example]", state="visible")
    # Daily-phrase card might appear later; tests don't depend on it.
    return page


def test_empty_textarea_chip_click_loads_sample_silently(page_at_workbench):
    """Scenario 1: empty textarea → first chip click fills with no modal."""
    page = page_at_workbench
    ta = page.locator("#text")
    assert ta.input_value() == ""
    first_chip = page.locator(".chip[data-example]").first
    expected = first_chip.get_attribute("data-example")
    first_chip.click()
    # Modal must NOT be shown
    assert page.locator("#example-overwrite-modal").is_hidden(), "modal should not open for empty textarea"
    # Text loaded
    assert ta.input_value() == expected


def test_nonempty_chip_click_opens_modal_does_not_overwrite(page_at_workbench):
    """Scenario 2: user content → modal shown, textarea NOT overwritten yet."""
    page = page_at_workbench
    ta = page.locator("#text")
    user_text = "我自己写的稿子，正在打磨"
    ta.fill(user_text)
    page.locator(".chip[data-example]").first.click()
    # Modal visible
    assert page.locator("#example-overwrite-modal").is_visible()
    # Textarea unchanged
    assert ta.input_value() == user_text


def test_keep_button_preserves_user_content(page_at_workbench):
    """Scenario 3: 保留原文 → modal closes, user content intact."""
    page = page_at_workbench
    ta = page.locator("#text")
    user_text = "保留这一段"
    ta.fill(user_text)
    page.locator(".chip[data-example]").first.click()
    assert page.locator("#example-overwrite-modal").is_visible()
    page.locator("#example-overwrite-keep").click()
    assert page.locator("#example-overwrite-modal").is_hidden()
    assert ta.input_value() == user_text


def test_load_button_replaces_with_sample(page_at_workbench):
    """Scenario 4: 套用示例 → modal closes, textarea = sample text."""
    page = page_at_workbench
    ta = page.locator("#text")
    ta.fill("旧的稿子")
    chip = page.locator(".chip[data-example]").first
    incoming = chip.get_attribute("data-example")
    chip.click()
    page.locator("#example-overwrite-modal").wait_for(state="visible")
    page.locator("#example-overwrite-load").click()
    page.locator("#example-overwrite-modal").wait_for(state="hidden")
    assert ta.input_value() == incoming
