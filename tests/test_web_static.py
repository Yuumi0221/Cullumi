import re
from pathlib import Path
import unittest


class WebStaticTests(unittest.TestCase):
    def test_configuration_recovery_warning_is_visible_once(self):
        root = Path(__file__).parents[1]
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        server = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn('id="startupWarning"', markup)
        self.assertIn('b.startup_warning&&!$("#startupWarning").dataset.shown', script)
        self.assertIn('"startup_warning": CONFIG.load_warning', server)

    def test_scan_reports_files_that_become_unavailable(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("p.unavailable_count", script)
        self.assertIn("张照片在扫描期间不可用，已安全跳过", script)

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
        matches = re.findall(r"background:\s*(?:rgba\(13,\s*16,\s*14,\s*0\.76\)|#[0-9A-Fa-f]{8})\s*;?", styles)
        self.assertGreaterEqual(len(matches), 3)
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
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("margin-right: 12px;", styles)
        self.assertIn("overflow-x: hidden;", styles)
        self.assertIn("padding: 3px 4px 40px;", styles)
        self.assertIn("padding: 2px 2px 24px;", styles)
        self.assertIn("*::-webkit-scrollbar-track", styles)
        self.assertIn("scrollbar-color: transparent transparent;", styles)
        self.assertIn(".scroll-fade-region:hover {", styles)
        self.assertIn("transition:scrollbar-color .5s ease;", styles)
        self.assertNotIn("transition:background-color .5s ease;", styles)
        self.assertEqual(styles.count("transition-duration:.15s;"), 1)
        self.assertIn(".scroll-fade-region:hover::-webkit-scrollbar-thumb {", styles)
        self.assertIn(".scroll-fade-region:hover::-webkit-scrollbar-thumb:hover {", styles)
        self.assertIn('id="similarFolderPane" class="similar-folder-pane scroll-fade-region"', markup)
        self.assertIn('id="similarDetail" class="similar-detail scroll-fade-region hidden"', markup)
        self.assertIn('class="recent-grid scroll-fade-region"', markup)
        self.assertIn('class="confirm-list scroll-fade-region"', script)
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

    def test_active_custom_profile_delete_shows_switch_warning(self):
        root = Path(__file__).parents[1]
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="profileInUseWarning"', markup)
        self.assertIn("projectsUsingProfile(profile.id)", script)
        self.assertIn("project.profile_id===profileId", script)
        self.assertIn("project.id===state.project?.id&&project!==state.project", script)
        self.assertIn("正在被当前项目使用，暂时不能删除", script)
        self.assertIn("请先在窗口顶部切换到其他分析模式", script)
        self.assertIn('e.message.includes("被项目使用")', script)

    def test_notice_only_dialogs_close_when_the_backdrop_is_clicked(self):
        root = Path(__file__).parents[1]
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(markup.count("data-backdrop-close"), 2)
        self.assertIn(
            'id="startupWarning" class="confirm" data-backdrop-close', markup
        )
        self.assertIn(
            'id="profileInUseWarning" class="confirm" data-backdrop-close', markup
        )
        self.assertNotIn(
            'id="confirm" class="confirm" data-backdrop-close', markup
        )
        self.assertNotIn(
            'id="updateDialog" class="confirm" data-backdrop-close', markup
        )
        self.assertIn("function closeNoticeOnBackdrop(event)", script)
        self.assertIn("dialog.getBoundingClientRect()", script)
        self.assertIn("if(outside)dialog.close()", script)
        self.assertIn(
            '$$("dialog[data-backdrop-close]").forEach(dialog=>dialog.addEventListener("click",closeNoticeOnBackdrop))',
            script,
        )

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
        self.assertIn("Cullumi-theme", script)
        self.assertIn('json("/api/settings",{theme})', script)
        self.assertIn("project-open", script)
        self.assertIn("body:not(.project-open) .project-only-action", styles)
        self.assertIn('[data-theme="night"]', styles)

    def test_application_and_topbar_brand_icons_are_wired(self):
        root = Path(__file__).parents[1]
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        spec = (root / "Cullumi.spec").read_text(encoding="utf-8")
        app_entry = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn('/static/brand-icon.png', markup)
        self.assertIn('icon="web/brand-icon.ico"', spec)
        self.assertIn('APP_ICON = WEB_ROOT / "brand-icon.ico"', app_entry)
        self.assertIn('webview.start(apply_native_window_icon, (window,), icon=str(APP_ICON))', app_entry)
        self.assertIn('native.Invoke(Action(lambda: setattr(native, "Icon", icon)))', app_entry)
        for name in ("brand-icon.png", "brand-icon.ico"):
            self.assertTrue((root / "web" / name).is_file())

    def test_png_assets_have_no_problematic_icc_profile(self):
        web_root = Path(__file__).parents[1] / "web"
        for path in web_root.glob("*.png"):
            self.assertNotIn(b"iCCP", path.read_bytes(), path.name)

    def test_native_dialogs_use_current_pywebview_enum(self):
        app_entry = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn("webview.FileDialog.FOLDER", app_entry)
        self.assertIn("webview.FileDialog.OPEN", app_entry)
        self.assertIn("webview.FileDialog.SAVE", app_entry)
        self.assertNotIn("webview.FOLDER_DIALOG", app_entry)
        self.assertNotIn("webview.OPEN_DIALOG", app_entry)
        self.assertNotIn("webview.SAVE_DIALOG", app_entry)

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
        self.assertIn("button.primary:hover:not(:disabled),", styles)
        self.assertIn(
            '[data-theme="night"] button.primary:hover:not(:disabled),', styles
        )
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
        self.assertIn("background: var(--image-surface);", styles)

    def test_settings_tabs_use_full_outline_and_subtle_night_hover(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".settings-tabs button.active {", styles)
        self.assertIn("border: 1px solid var(--pink);", styles)
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
        self.assertIn("background: var(--surface-hover);", styles)

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

    def test_home_uses_adaptive_golden_split_and_searchable_two_column_recents(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="recent-panel"', markup)
        self.assertIn('class="primary hero-choose"', markup)
        self.assertIn('<div class="section-title recent-title-row">', markup)
        self.assertLess(markup.index('id="recentTitle"'), markup.index('id="recentSearch"'))
        self.assertIn('id="recentSearch"', markup)
        self.assertIn('<h1><span>快速留下</span><span>美好瞬间</span></h1>', markup)
        self.assertIn("function renderRecentProjects()", script)
        self.assertIn('$("#recentSearch").oninput=', script)
        self.assertIn('class="recent-more"', script)
        self.assertIn('<circle cx="2" cy="2" r="2"/>', script)
        self.assertIn('event.target.closest(".recent-more")?openRecentMenu(', script)
        self.assertIn("item.oncontextmenu=event=>openRecentMenu(", script)
        self.assertIn(".recent-more:hover {", styles)
        self.assertIn("background:var(--home-more-hover);", styles)
        self.assertIn("--home-split:61.8034vw;", styles)
        self.assertIn("inset:0 0 0 var(--home-split);", styles)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr));", styles)
        self.assertIn("padding:37px 68px 40px 68px;", styles)
        self.assertIn("height:calc(100vh - 70px);", styles)
        self.assertNotIn(".hero > div:first-child::before {", styles)
        self.assertNotIn(".hero::before {", styles)
        self.assertIn(".recent-grid {\n  width:674px;\n  max-width:calc(100% + 22px);\n  flex:1;\n  min-height:0;", styles)
        self.assertIn("margin:-10px 0 0 -22px;", styles)
        self.assertIn("padding:10px 36px 80px 22px;", styles)
        self.assertIn(".recent-empty {\n  grid-column:1 / -1;\n  width:100%;", styles)
        self.assertIn("padding:80px 0;\n  justify-self:stretch;", styles)
        self.assertIn("display:grid;\n  grid-template-columns:repeat(2,minmax(0,1fr));", styles)
        self.assertIn("top:185px;", styles)
        self.assertIn("top:160px;", styles)
        self.assertIn("@media (min-width: 1501px) and (max-height: 850px)", styles)
        self.assertIn("overscroll-behavior:contain;", styles)
        self.assertIn(".home .hero-choose {\n  width:308px;", styles)
        self.assertIn('[data-theme="night"] .home .hero-choose {', styles)
        self.assertIn(".recent-panel {\n  position:absolute;\n  inset:0 0 0 var(--home-split);\n  min-width:0;\n  min-height:0;", styles)
        self.assertIn("background:transparent", styles)
        self.assertIn("box-shadow:none", styles)
        self.assertIn('font:700 25px/1.2 "Segoe UI","Microsoft YaHei",sans-serif;', styles)
        self.assertIn('font:400 20px/1.45 "Segoe UI","Microsoft YaHei",sans-serif;', styles)
        self.assertEqual(markup.count('class="filter-chevron"'), 2)
        self.assertNotIn("<i>⌄</i>", markup)
        self.assertIn("align-items:center;", styles)
        self.assertIn("--keep-hover-dark:#294f3a;", styles)
        self.assertIn("button:focus-visible", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)

    def test_warm_theme_preserves_semantic_action_colors_and_resets_similar_actions(self):
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn("--home-accent:#ca7576;", styles)
        self.assertIn("--pink2:#f4dfe2;", styles)
        self.assertIn("--home-accent:#e59691;", styles)
        self.assertIn("--keep:#5e7a47;", styles)
        self.assertIn("--red:#ae431e;", styles)
        self.assertIn("background: var(--keep);", styles)
        self.assertIn("background: var(--red);", styles)
        self.assertIn('visible=state.view==="similar"&&selected', script)
        self.assertIn('$("#similarViewActions").classList.toggle("hidden",!visible)', script)
        self.assertIn("document.body.classList.toggle(\"similar-view-open\",state.view===\"similar\");\n  applySimilarMode();", script)

    def test_current_project_card_opens_its_folder_on_double_click(self):
        markup = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="projectBox"', markup)
        self.assertIn('title="双击在文件管理器中打开"', markup)
        self.assertIn("async function openCurrentProjectFolder()", script)
        self.assertIn('$("#projectBox").ondblclick=openCurrentProjectFolder', script)
        self.assertIn('json("/api/project/open-folder",{project_id:state.project.id})', script)


if __name__ == "__main__":
    unittest.main()
