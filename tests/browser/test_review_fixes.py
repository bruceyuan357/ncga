"""Browser regression tests for the 2026-09 UX review fixes.

Each test pins one behaviour that was previously wrong. Grouped by the finding
they close:

  #1 key state    — the settings line claimed "using the server's default key"
                    even when no key existed anywhere.
  #2 no signal    — nothing told you AI was unavailable until you spent a rewrite.
  #4 fake result  — a failed rewrite rendered the user's own text with a dialect
                    banner glued on, complete with a stats bar.
  #6 drafts       — 存草稿 wrote localStorage and nothing ever read it back.
  #7 dead CTA     — the primary button looked enabled with no dialect chosen and
                    could only produce an error-styled "pick a dialect" banner.
  #8 dark mode    — there was none.

Run with:
    python -m playwright install chromium   # one-time
    pytest tests/browser/
"""

from __future__ import annotations

import pytest


def _goto_workbench(page, base_url: str):
    """Land on the workbench with boot finished.

    `domcontentloaded` (not `load`) for the same reason as the modal suite: the
    daily-phrase card fires background rewrites that keep `load` pending.
    """
    page.goto(f"{base_url}/#workbench", wait_until="domcontentloaded")
    _wait_for_presets(page)
    return page


def _wait_for_presets(page):
    """Block until /api/presets has populated the dialect <select>.

    Waiting on the <option> as a *selector* never resolves — options inside a
    closed select are not visible — so assert on the option count instead.
    """
    page.wait_for_function(
        "() => document.querySelectorAll('#target_variety option').length > 1",
        timeout=10_000,
    )


# --------------------------------------------------------------------------
# #2 / #1 — honest key state
# --------------------------------------------------------------------------


def test_no_key_banner_shows_when_server_has_no_key(live_app_no_key, page):
    _goto_workbench(page, live_app_no_key)
    banner = page.locator("#no-key-banner")
    banner.wait_for(state="visible", timeout=10_000)
    assert "API Key" in banner.inner_text()
    # It must offer the fix, not just state the problem.
    assert banner.locator("a[href='#settings']").count() == 1


def test_no_key_banner_hidden_when_key_is_configured(live_app, page):
    _goto_workbench(page, live_app)
    page.wait_for_timeout(1200)  # let the /api/healthz probe resolve
    assert page.locator("#no-key-banner").is_hidden()


def test_settings_does_not_claim_a_server_key_that_does_not_exist(live_app_no_key, page):
    page.goto(f"{live_app_no_key}/#settings", wait_until="domcontentloaded")
    status = page.locator("#byok-status")
    status.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(1200)
    text = status.inner_text()
    # The exact old lie. It must not come back.
    assert "当前使用服务器默认密钥" not in text
    assert "不可用" in text


def test_healthz_reports_llm_state(live_app_no_key, live_app, page):
    for base, expected in ((live_app_no_key, False), (live_app, True)):
        page.goto(f"{base}/", wait_until="domcontentloaded")
        got = page.evaluate(
            "async () => (await (await fetch('/api/healthz')).json()).llm_configured"
        )
        assert got is expected


# --------------------------------------------------------------------------
# #4 — a failed rewrite must not fabricate a result
# --------------------------------------------------------------------------


def test_failed_rewrite_shows_error_and_no_fabricated_result(live_app_no_key, page):
    _goto_workbench(page, live_app_no_key)
    page.fill("#text", "今天天气很好，我打算出去散步。")
    page.select_option("#target_variety", "dongbei_mandarin")
    page.click("#submit-button")

    status = page.locator("#status")
    page.wait_for_function(
        "() => document.querySelector('#status').classList.contains('error')",
        timeout=10_000,
    )
    assert "API Key" in status.inner_text()

    # The old fallback prefixed the input with a dialect banner and showed it
    # as a result, with a full stats bar underneath. Neither may happen.
    assert page.locator("#result").inner_text().strip() == ""
    assert "给你整成东北那股劲儿" not in page.content()
    assert page.locator("#result-stats").is_hidden()
    # Result actions stay unavailable — there is nothing to copy or rate.
    assert page.locator("#copy-button").is_disabled()
    assert page.locator("#favorite-button").is_disabled()


def test_error_message_is_chinese(live_app_no_key, page):
    _goto_workbench(page, live_app_no_key)
    page.fill("#text", "你好")
    page.select_option("#target_variety", "dongbei_mandarin")
    page.click("#submit-button")
    page.wait_for_function(
        "() => document.querySelector('#status').classList.contains('error')",
        timeout=10_000,
    )
    text = page.locator("#status").inner_text()
    # The raw upstream English used to be piped straight into this Chinese UI.
    assert "Using local heuristic fallback" not in text
    assert "No LLM provider is configured" not in text
    assert any("一" <= ch <= "鿿" for ch in text)


# --------------------------------------------------------------------------
# #7 — the CTA never invites a click that can only fail
# --------------------------------------------------------------------------


def test_submit_disabled_until_a_dialect_is_chosen(live_app, page):
    _goto_workbench(page, live_app)
    submit = page.locator("#submit-button")
    assert submit.is_disabled(), "no dialect chosen yet — the CTA must not look ready"
    page.select_option("#target_variety", "dongbei_mandarin")
    assert submit.is_enabled()


def test_idle_status_is_a_hint_not_an_error(live_app, page):
    _goto_workbench(page, live_app)
    status = page.locator("#status")
    classes = status.get_attribute("class") or ""
    assert "hint" in classes
    assert "error" not in classes


# --------------------------------------------------------------------------
# #6 — drafts actually come back
# --------------------------------------------------------------------------


def test_draft_is_restored_after_reload(live_app, page):
    _goto_workbench(page, live_app)
    page.fill("#text", "草稿应该活下来。")
    page.wait_for_timeout(900)  # let the text-only autosave settle FIRST…
    # …then choose the dialect. Autosave used to listen only to the textarea,
    # so a dialect picked after the debounce window was never persisted and
    # this test only passed by landing inside the window.
    page.select_option("#target_variety", "dongbei_mandarin")
    page.wait_for_timeout(900)

    page.reload(wait_until="domcontentloaded")
    _wait_for_presets(page)
    page.wait_for_function(
        "() => document.querySelector('#text').value.includes('草稿应该活下来')",
        timeout=10_000,
    )
    assert page.locator("#target_variety").input_value() == "dongbei_mandarin"


def test_clearing_the_workbench_drops_the_stored_draft(live_app, page):
    _goto_workbench(page, live_app)
    page.fill("#text", "这条要被清掉。")
    page.wait_for_timeout(900)
    page.on("dialog", lambda d: d.accept())
    page.click("#clear-button")
    page.wait_for_timeout(300)
    assert page.evaluate("() => localStorage.getItem('ncga.draft.v1')") is None


# --------------------------------------------------------------------------
# #5 / #6 — information architecture
# --------------------------------------------------------------------------


def test_landmark_name_is_not_result_metadata(live_app, page):
    _goto_workbench(page, live_app)
    # It must live outside the result card's title row, where it used to sit
    # beside the 已降级 status chip and read as a fact about the rewrite.
    assert page.locator(".card-head-title #landmark-tag").count() == 0
    assert page.locator("#landmark-tag").count() == 1


def test_quality_dashboard_is_reachable_from_the_nav(live_app, page):
    _goto_workbench(page, live_app)
    link = page.locator("nav a[href='/quality']")
    assert link.count() == 1
    # Counting the link proved nothing: the SPA router used to preventDefault
    # every .nav-item click and route an unknown hash to the workbench. Click it.
    with page.expect_navigation(wait_until="domcontentloaded"):
        link.click()
    assert page.url.endswith("/quality")
    assert "质量看板" in page.content()


def test_offpage_primary_button_reads_as_navigation(live_app, page):
    page.goto(f"{live_app}/#atlas", wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    label = page.locator("#cmdbar-rewrite-label").inner_text()
    # Off the workbench this button navigates; labelling it 开始改写 made it look
    # like a second run button (and on #batch it fought 开始批量重写).
    assert label == "去重写台"


# --------------------------------------------------------------------------
# #8 — dark mode exists and the explicit choice wins
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scheme", "expect_dark"),
    [("light", False), ("dark", True)],
)
def test_follows_system_colour_scheme(live_app, browser, scheme, expect_dark):
    ctx = browser.new_context(color_scheme=scheme)
    page = ctx.new_page()
    _goto_workbench(page, live_app)
    paper = page.evaluate(
        "() => getComputedStyle(document.body).getPropertyValue('--at-paper-rgb').trim()"
    )
    assert paper.startswith("20" if expect_dark else "244")
    ctx.close()


@pytest.mark.parametrize(
    ("os_scheme", "choice", "expect_dark"),
    [("dark", "light", False), ("light", "dark", True)],
)
def test_explicit_theme_choice_overrides_the_system(live_app, browser, os_scheme, choice, expect_dark):
    ctx = browser.new_context(color_scheme=os_scheme)
    page = ctx.new_page()
    page.goto(f"{live_app}/", wait_until="domcontentloaded")
    page.evaluate(
        "t => localStorage.setItem('ncga.settings.v2', JSON.stringify({version:'v3', theme:t}))",
        choice,
    )
    # A hash-only goto is a same-document navigation — the boot script would
    # never re-run and the stored choice would not be applied.
    page.reload(wait_until="domcontentloaded")
    _wait_for_presets(page)
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == choice
    paper = page.evaluate(
        "() => getComputedStyle(document.body).getPropertyValue('--at-paper-rgb').trim()"
    )
    assert paper.startswith("20" if expect_dark else "244")
    ctx.close()
