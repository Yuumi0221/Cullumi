from pathlib import Path
import unittest


class WebStaticTests(unittest.TestCase):
    def test_project_removal_can_delete_cache_without_touching_photos(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="deleteProjectCache"', script)
        self.assertIn("delete_cache:deleteCache", script)
        self.assertIn("真实照片不会被删除或移动", script)
        self.assertIn('id="recentRemove"', markup)

    def test_update_controls_and_download_prompt_are_wired(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="autoCheckUpdates"', markup)
        self.assertIn('id="checkUpdateBtn"', markup)
        self.assertIn('id="updateDialog"', markup)
        self.assertIn('json("/api/update/check",{})', script)
        self.assertIn('json("/api/update/download",{})', script)
        self.assertIn("auto_check_updates:e.target.checked", script)

    def test_mode_selects_are_rounded_and_csv_actions_share_a_row(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="csv-actions"', markup)
        self.assertIn(".csv-actions {", styles)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);", styles)
        self.assertIn("#profileEditorSelect {", styles)
        self.assertIn(".form-grid select[data-p] {", styles)
        self.assertIn("appearance: none;", styles)

    def test_sidebar_uses_library_presets_instead_of_duplicate_pages(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-nav="library" data-preset="library"', markup)
        self.assertIn('data-nav="ai" data-preset="ai"', markup)
        self.assertIn('data-nav="undecided" data-preset="undecided"', markup)
        self.assertIn('data-nav="keep" data-preset="keep"', markup)
        self.assertIn('data-nav="remove" data-preset="remove"', markup)
        self.assertNotIn('data-view="quality"', markup)
        self.assertNotIn('data-view="all"', markup)
        self.assertNotIn('data-view="decided"', markup)
        self.assertNotIn("qualityFilter", script)
        self.assertIn("function applyLibraryPreset(", script)

    def test_library_multiselects_support_all_none_and_accessible_panels(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('aria-controls="decisionFilterPanel"', markup)
        self.assertIn('aria-controls="aiFilterPanel"', markup)
        self.assertEqual(markup.count('data-filter-group="decisions"'), 3)
        self.assertEqual(markup.count('data-filter-group="ai"'), 3)
        self.assertIn('data-select-all="decisions"', markup)
        self.assertIn('data-select-all="ai"', markup)
        self.assertIn('if(!state.filters.decisions.size)', script)
        self.assertIn('if(!state.filters.ai.size)', script)
        self.assertIn('return "none"', script)
        self.assertIn("function closeFilterMenus()", script)
        self.assertIn(".multi-filter-panel", styles)
        self.assertIn(".multi-filter-trigger.empty-selection", styles)

    def test_library_uses_incremental_loading_and_stale_request_generation(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="librarySentinel"', markup)
        self.assertIn("LIBRARY_PAGE_SIZE=120", script)
        self.assertIn("new IntersectionObserver", script)
        self.assertIn("generation!==state.library.generation", script)
        self.assertIn("offset:String(state.library.offset)", script)
        self.assertIn('insertAdjacentHTML("beforeend"', script)

    def test_library_decisions_reconcile_without_full_page_reload(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function photoMatchesLibrary(", script)
        self.assertIn("function reconcileLibraryDecision(", script)
        self.assertIn("state.library.offset=Math.max(0,state.library.offset-1)", script)
        self.assertIn("state.viewerDirtyIds.add(id)", script)

    def test_similar_decisions_update_every_visible_copy_of_a_photo(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('$$(`[data-photo-id="${id}"]`).forEach(card=>{', script)
        self.assertNotIn('const card=$(`[data-photo-id="${id}"]`);if(!card)return;', script)

    def test_photo_card_is_not_passed_directly_to_array_map(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn(".map(photoCard)", script)
        self.assertIn(".map((photo,index)=>photoCard(photo,index))", script)

    def test_similar_groups_and_viewer_use_explicit_recommendation_state(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('photo.id===detail.recommended_id', script)
        self.assertIn('_viewerBadge:recommended?"推荐保留":"可考虑移除"', script)
        self.assertIn('p.decision==="keep"', script)
        self.assertIn('p.decision==="remove"', script)

    def test_suggestion_badges_share_dark_translucent_background(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertGreaterEqual(styles.count("background: rgba(13, 16, 14, 0.76);"), 3)
        self.assertIn(".badge-candidate-remove", styles)
        self.assertIn(".badge-review", styles)
        self.assertIn(".badge-recommended", styles)

    def test_viewer_navigation_icons_are_centered_svg_buttons(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="close" data-close aria-label="关闭预览"><svg', markup)
        self.assertIn('id="viewerPrev" aria-label="上一张"><svg', markup)
        self.assertIn('id="viewerNext" aria-label="下一张"><svg', markup)
        self.assertIn("place-items: center;", styles)
        self.assertIn("width: 42px;", styles)
        self.assertIn("height: 42px;", styles)

    def test_profile_estimate_and_save_have_visible_status(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="profileSaveStatus"', markup)
        self.assertIn("estimated_pairs", script)
        self.assertIn("estimated_groups", script)
        self.assertIn("保存失败：", script)
        self.assertIn("自定义模式已保存", script)

    def test_custom_profile_empty_fields_are_validated(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("validateProfileInputs", script)
        self.assertIn("还有项目没有输入完整", script)
        self.assertIn("el.placeholder=", script)
        self.assertIn(".field-invalid", styles)
        self.assertIn(".input-invalid", styles)

    def test_legacy_pair_view_is_removed(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        server = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("function renderPairs(", script)
        self.assertNotIn("pair-row", styles)
        self.assertNotIn("pair-card", styles)
        self.assertNotIn('"/api/pairs"', server)
        self.assertNotIn("def api_pairs(", server)

    def test_similar_groups_use_folder_and_two_level_browser(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        server = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('id="similarBrowser"', markup)
        self.assertIn('id="similarCollapseBtn"', markup)
        self.assertIn('id="similarExpandBtn"', markup)
        self.assertIn('id="similarBackBtn"', markup)
        self.assertIn("/api/similar-groups", script)
        self.assertIn("/api/similar-group", script)
        self.assertNotIn('json(`/api/pairs?', script)
        self.assertIn("function closeSimilarDetail(", script)
        self.assertIn("function expandSimilarDetail(", script)
        self.assertIn(".folder-cover.cover-3", styles)
        self.assertIn(".similar-browser.detail-open", styles)
        self.assertIn('"/api/similar-groups": self.api_similar_groups', server)
        self.assertIn('"/api/similar-group": self.api_similar_group', server)

    def test_similar_group_escape_respects_open_dialogs(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if($$("dialog[open]").length)return', script)
        self.assertIn('state.view==="similar"&&state.similar.selectedId', script)

    def test_similar_side_panels_scroll_independently_and_share_toolbar(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertLess(markup.index('id="similarViewActions"'), markup.index('class="search"'))
        self.assertNotIn("similar-detail-head", markup)
        self.assertNotIn('id="similarDetailTitle"', markup)
        self.assertIn("body.similar-side-open > main", styles)
        self.assertIn(".similar-browser.detail-open:not(.detail-expanded) .similar-folder-pane", styles)
        self.assertIn(".similar-browser.detail-open:not(.detail-expanded) .similar-detail", styles)
        self.assertGreaterEqual(styles.count("overflow-y: auto;"), 3)
        self.assertIn('detail.face_safe?" · 人物照片请检查表情":""', script)

    def test_similar_detail_matches_library_card_size_and_keeps_toolbar_on_one_line(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('classList.toggle("similar-view-open",state.view==="similar")', script)
        self.assertIn('classList.toggle("similar-detail-open"', script)
        self.assertIn("body.similar-view-open .toolbar {", styles)
        self.assertIn("body.similar-view-open .toolbar > div:first-child {", styles)
        self.assertIn("body.similar-view-open .search {", styles)
        self.assertIn("flex-wrap: nowrap;", styles)
        self.assertIn(".similar-detail-gallery {", styles)
        self.assertIn("repeat(auto-fill, minmax(210px, 210px))", styles)
        self.assertIn("repeat(auto-fill, minmax(170px, 210px))", styles)

    def test_similar_selection_outlines_have_safe_insets_and_subtle_scrollbars(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("margin-right: 12px;", styles)
        self.assertIn("overflow-x: hidden;", styles)
        self.assertIn("padding: 3px 4px 40px;", styles)
        self.assertIn("padding: 2px 2px 24px;", styles)
        self.assertIn("*::-webkit-scrollbar-track", styles)
        self.assertIn("scrollbar-color: rgba(99,105,100,.24) transparent;", styles)
        self.assertIn('[data-theme="night"] * {', styles)
        self.assertIn("scrollbar-color:rgba(160,172,162,.26) transparent", styles)
        self.assertIn("*::-webkit-scrollbar-button", styles)

    def test_viewer_supports_click_wheel_reset_and_drag(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="viewerImage" draggable="false"', markup)
        self.assertIn("function zoomViewer(", script)
        self.assertIn('addEventListener("dblclick"', script)
        self.assertIn('addEventListener("wheel"', script)
        self.assertIn('addEventListener("mousedown"', script)
        self.assertIn("Math.max(1,Math.min(8", script)

    def test_destructive_ui_actions_require_confirmation(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function confirmDeleteProfile()", script)
        self.assertIn("function confirmClearDecisions()", script)
        self.assertIn('$("#deleteProfile").onclick=confirmDeleteProfile', script)
        self.assertIn('$("#clearDecisionsBtn").onclick=confirmClearDecisions', script)
        self.assertIn('$("#confirmOk").textContent="确认删除"', script)
        self.assertIn('$("#confirmOk").textContent="确认清空"', script)

    def test_export_uses_native_save_route_and_restored_batches_have_no_button(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        server = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('json("/api/export/save"', script)
        self.assertNotIn("window.open('/api/export", script)
        self.assertIn('"/api/export/save": self.api_export_save', server)
        self.assertIn('x.restored_at?"":`<div class="card-actions">', script)

    def test_theme_toggle_and_home_action_visibility(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('id="themeBtn"', markup)
        self.assertIn("photo-culler-theme", script)
        self.assertIn('json("/api/settings",{theme})', script)
        self.assertIn("project-open", script)
        self.assertIn("body:not(.project-open) .project-only-action", styles)
        self.assertIn('[data-theme="night"]', styles)

    def test_night_theme_folders_and_search_have_consistent_backgrounds(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('[data-theme="night"] .similar-folder {', styles)
        self.assertIn('[data-theme="night"] .similar-folder:hover {', styles)
        self.assertIn('[data-theme="night"] .search input {', styles)
        self.assertIn("background: transparent;", styles)
        self.assertIn("background: var(--card);", styles)

    def test_night_mode_select_and_button_hover_states_are_legible(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('[data-theme="night"] select option {', styles)
        self.assertIn('[data-theme="night"] #profileSelect {', styles)
        self.assertIn("button:hover:not(:disabled)", styles)
        self.assertIn('[data-theme="night"] nav button:not(.active) {', styles)
        self.assertIn('[data-theme="night"] nav button:not(.active):hover {', styles)
        self.assertIn('[data-theme="night"] .logo {', styles)
        self.assertIn('[data-theme="night"] .logo:hover:not(:disabled) {', styles)

    def test_primary_danger_and_top_controls_have_explicit_theme_hover_states(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        for selector in (
            "#chooseBtn:hover:not(:disabled)",
            "#saveProfile:hover:not(:disabled)",
            "#homeBtn:hover:not(:disabled)",
            "#quarantineBtn:hover:not(:disabled)",
            "#deleteProfile:hover:not(:disabled)",
            '[data-theme="night"] #profileSelect:hover',
            '[data-theme="night"] #settingsBtn:hover',
        ):
            self.assertIn(selector, styles)
        self.assertIn("#profileSelect:hover,", styles)
        self.assertIn("#settingsBtn:hover {", styles)

    def test_context_settings_viewer_and_similar_hover_refinements(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('[data-theme="night"] .context-menu button {', styles)
        self.assertIn('[data-theme="night"] .context-menu button:hover:not(:disabled) {', styles)
        self.assertIn('[data-theme="night"] #settingsBtn {', styles)
        self.assertIn(".viewer .close:hover:not(:disabled) {", styles)
        self.assertIn('[data-theme="day"] button.similar-folder:hover:not(:disabled):not(.active) {', styles)
        self.assertIn('[data-theme="day"] button.similar-folder.active:hover:not(:disabled) {', styles)
        self.assertIn('[data-theme="day"] .similar-toolbar-actions button {', styles)
        self.assertIn('[data-theme="day"] .similar-toolbar-actions button:hover:not(:disabled) {', styles)
        self.assertIn(".viewer .side:hover:not(:disabled)", styles)
        self.assertIn(".viewer .viewer-decision:hover:not(:disabled)", styles)
        self.assertIn(".viewer .viewer-keep.active:hover:not(:disabled)", styles)
        self.assertIn(".viewer .viewer-remove.active:hover:not(:disabled)", styles)

    def test_confirmation_hover_and_night_quarantine_list_are_theme_correct(self):
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("#confirmOk:hover:not(:disabled)", styles)
        self.assertIn('[data-theme="night"] #confirmOk:hover:not(:disabled)', styles)
        self.assertIn('[data-theme="night"] .confirm-list {', styles)
        self.assertIn("background: #171b18;", styles)

    def test_settings_tabs_use_full_outline_and_subtle_night_hover(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".settings-tabs button.active {", styles)
        self.assertIn("border: 1px solid var(--green);", styles)
        self.assertIn("border-bottom:0", styles)
        self.assertIn("border:1px solid var(--line);", styles)
        self.assertNotIn("border-bottom-color:var(--card)", styles)
        self.assertIn("#generalSettings > .toggle {", styles)
        self.assertIn("margin-bottom:8px", styles)
        self.assertIn("background:transparent", styles)
        self.assertIn('class="update-row">\n        <label class="toggle"', markup)
        self.assertIn('<button id="checkUpdateBtn">检查更新</button>', markup)
        self.assertIn('[data-theme="night"] .settings-tabs button:not(.active) {', styles)
        self.assertIn('[data-theme="night"] .settings-tabs button:not(.active):hover {', styles)
        self.assertIn('[data-theme="night"] .settings-tabs button.active:hover {', styles)
        self.assertIn("background: #303631;", styles)

    def test_viewer_close_refreshes_card_decisions(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("viewerNeedsRefresh", script)
        self.assertIn("async function syncViewerDecisions()", script)
        self.assertIn('$("#viewer").addEventListener("close"', script)

    def test_ai_suggestions_can_be_marked_for_removal_in_one_confirmed_action(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        server = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('id="markAiRemoveBtn"', markup)
        self.assertIn('id="aiRemovePendingCount"', markup)
        self.assertIn("function confirmAiRemoveSuggestions()", script)
        self.assertIn('json("/api/decision/ai-remove"', script)
        self.assertIn('state.activeNav!=="ai"', script)
        self.assertIn('"/api/decision/ai-remove": self.api_decision_ai_remove', server)
        self.assertIn(".ai-sweep-button {", styles)
        self.assertNotIn("batch-confirm-summary", script)
        self.assertNotIn("batch-confirm-summary", styles)
        self.assertIn(".toolbar button.ai-sweep-button:hover:not(:disabled) {", styles)
        self.assertIn('[data-theme="night"] .toolbar button.ai-sweep-button:hover:not(:disabled) {', styles)
        self.assertIn("transform:none;", styles)
        self.assertIn("box-shadow:none;", styles)

    def test_home_uses_golden_split_unified_fonts_and_svg_filter_chevrons(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="recent-panel"', markup)
        self.assertIn("grid-template-columns:minmax(0,1fr) minmax(330px,.618fr);", styles)
        self.assertEqual(styles.count('"Segoe UI","Microsoft YaHei",sans-serif;'), 3)
        self.assertEqual(markup.count('class="filter-chevron"'), 2)
        self.assertNotIn("<i>⌄</i>", markup)
        self.assertIn("align-items:center;", styles)
        self.assertIn("background:linear-gradient(145deg,#467257", styles)
        self.assertIn("button:focus-visible", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)


if __name__ == "__main__":
    unittest.main()
