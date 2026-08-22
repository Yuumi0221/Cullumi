import { expect, test } from "playwright/test";


const token = process.env.CULLUMI_DOM_TOKEN || "cullumi-dom-test";
const image = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='32' height='24'%3E%3Crect width='32' height='24' fill='%23d9b7bd'/%3E%3C/svg%3E";
const runtimeProblems = new WeakMap();

const profiles = [
  { id: "conservative", name: "保守筛选", builtin: true },
  { id: "custom-portrait", name: "人像精选", builtin: false, base_mode: "conservative" },
];

function projectPayload(decision = "", photoCount = 2, decisions = null) {
  const values = decisions ? [...decisions.values()] : [decision].filter(Boolean);
  const kept = values.filter(value => value === "keep").length;
  const removed = values.filter(value => value === "remove").length;
  return {
    id: "project-1",
    root: "C:\\照片\\夏日旅行",
    cache_root: "C:\\Cullumi缓存",
    profile_id: "custom-portrait",
    total: photoCount,
    similar_groups: 0,
    pairs: 0,
    counts: { unreadable: 0 },
    decisions: { keep: kept, remove: removed },
    library_counts: {
      readable: photoCount,
      ai_pending: 1,
      ai_remove_pending: 0,
      undecided: photoCount - kept - removed,
      keep: kept,
      remove: removed,
      unreadable: 0,
    },
  };
}

function recentPayload(loaded = false) {
  const today = new Date();
  const localDate = [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, "0"),
    String(today.getDate()).padStart(2, "0"),
  ].join("-");
  return {
    id: "project-1",
    root: "C:\\照片\\夏日旅行",
    cache_root: "C:\\Cullumi缓存",
    profile_id: "custom-portrait",
    last_opened: `${localDate}T09:30:00`,
    available: true,
    stats_loaded: loaded,
    total: loaded ? 2 : 0,
    kept: 0,
    thumbnail_url: loaded ? image : "",
  };
}

function photoPayload(decision = "", id = 1) {
  return {
    id,
    relative_path: `旅行/海边-${id}.jpg`,
    width: 4000,
    height: 3000,
    size: 2_500_000,
    suggestion: "review",
    reason: "建议人工复查",
    decision,
    thumb_url: image,
    photo_url: image,
  };
}

function motionPhotoPayload(decision = "", id = 1, stillTime = 0) {
  return {
    ...photoPayload(decision, id),
    media_type: "motion_photo",
    quality_score: 86.5,
    motion: {
      kind: "apple_sidecar",
      duration_ms: 1200,
      fps: 30,
      frame_count: 36,
      still_time_ms: stillTime,
      cover_source: "still",
      cover_time_ms: 0,
      cover_frame_index: 0,
      error: "",
      video_url: image,
    },
  };
}

async function installApi(page, options = {}) {
  let decision = "";
  let writebackMode = options.writebackMode || "never";
  const decisions = new Map();
  const requests = [];
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postDataJSON?.() || null;
    requests.push({ path: url.pathname, method: request.method(), body });

    const fulfill = (payload, status = 200) => route.fulfill({
      status,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify(payload),
    });

    if (url.pathname === "/api/bootstrap") {
      return fulfill({
        version: "1.0.1",
        profiles,
        settings: {
          theme: "day",
          auto_advance: false,
          auto_check_updates: false,
          motion_cover_writeback: writebackMode,
          default_cache_root: "C:\\Cullumi缓存",
        },
        recent_projects: [recentPayload(false)],
        startup_warning: "",
      });
    }
    if (url.pathname === "/api/recent-project") return fulfill(recentPayload(true));
    if (url.pathname === "/api/project") {
      return fulfill(projectPayload(decision, options.photoCount || 2, decisions));
    }
    if (url.pathname === "/api/photos") {
      const selected = url.searchParams.get("decisions") || "all";
      const photoCount = options.photoCount || 1;
      const matching = Array.from({ length: photoCount }, (_, index) => {
        const id = index + 1;
        return options.motionPhoto
          ? motionPhotoPayload(decisions.get(id) || "", id, options.motionStillTime || 0)
          : photoPayload(decisions.get(id) || "", id);
      }).filter(photo => {
        const current = photo.decision || "undecided";
        return selected === "all" || selected.split(",").includes(current);
      });
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || 200);
      return fulfill({ total: matching.length, items: matching.slice(offset, offset + limit) });
    }
    if (url.pathname === "/api/decision") {
      if (options.decisionFails) return fulfill({ error: "数据库暂时不可写" }, 500);
      decision = body.decision;
      decisions.set(body.photo_id, decision);
      return fulfill({
        saved: true,
        photo_id: body.photo_id,
        decision,
        project_counts: projectPayload(decision, options.photoCount || 2, decisions),
      });
    }
    if (url.pathname === "/api/motion/cover") {
      const photo = motionPhotoPayload(decisions.get(body.photo_id) || "", body.photo_id, options.motionStillTime || 0);
      photo.motion.cover_source = body.source;
      photo.motion.cover_time_ms = body.time_ms || 0;
      if (body.write_source) {
        photo.motion.cover_source = "still";
        photo.motion.cover_time_ms = 0;
        photo.motion.still_time_ms = body.time_ms || 0;
      }
      if (options.motionCoverSuggestion) {
        photo.suggestion = options.motionCoverSuggestion;
        photo.reason = options.motionCoverSuggestion === "remove" ? "严重失焦" : "建议人工复查";
      }
      return fulfill({ saved: true, source_written: !!body.write_source, source_backup: body.write_source ? "C:\\backup\\photo.jpg" : "", photo, project_counts: projectPayload("", options.photoCount || 2, decisions) });
    }
    if (url.pathname === "/api/motion/locate") {
      return fulfill({ still_time_ms: options.locatedMotionStillTime || 0 });
    }
    if (url.pathname === "/api/motion/recommend") {
      return fulfill({ recommended: { time_ms: 600, frame_index: 18, quality_score: 91.2 }, candidates: [] });
    }
    if (url.pathname === "/api/settings") {
      if (body.motion_cover_writeback) writebackMode = body.motion_cover_writeback;
      return fulfill({ saved: true, settings: { theme: body.theme || "day", motion_cover_writeback: writebackMode } });
    }
    if (url.pathname === "/api/choose-cache") {
      return fulfill({ path: "D:\\新缓存" });
    }
    if (url.pathname === "/api/project/cache") {
      if (options.cacheMigrationFails) return fulfill({ error: "目标文件夹无法写入" }, 400);
      return fulfill({ changed: true, cache_root: "D:\\新缓存", old_cache: "" });
    }
    if (url.pathname === "/api/quarantine/batches") {
      return fulfill({
        items: [{ id: "batch-1", created_at: "2026-08-20 10:00", count: 1, total_size: 1024, restored_at: "" }],
      });
    }
    if (url.pathname === "/api/quarantine/preview") {
      return fulfill({
        count: 1,
        total_size: 2_500_000,
        items: [{ relative_path: "旅行/海边-1.jpg" }],
      });
    }
    if (url.pathname === "/api/similar-groups") {
      return fulfill({
        total: 1,
        items: [{
          id: "similar-1",
          count: 2,
          kind: "similar",
          face_safe: false,
          recommended: photoPayload("", 1),
          covers: [photoPayload("", 1), photoPayload("", 2)],
        }],
      });
    }
    if (url.pathname === "/api/similar-group") {
      return fulfill({
        id: "similar-1",
        count: 2,
        kind: "similar",
        face_safe: false,
        recommended_id: 1,
        members: [
          { ...photoPayload("", 1), group_similarity: 1 },
          { ...photoPayload("", 2), group_similarity: 0.91 },
        ],
      });
    }
    if (url.pathname === "/api/quarantine/restore") {
      return fulfill({ restored: 1, conflicts: 0, missing: 0 });
    }
    return fulfill({ error: `DOM 测试未模拟接口 ${url.pathname}` }, 501);
  });
  return requests;
}

async function openApp(page, options = {}) {
  const requests = await installApi(page, options);
  await page.goto(`/?token=${token}`);
  await expect(page.locator("#home")).toBeVisible();
  await expect(page.locator("#recentList .recent")).toHaveCount(1);
  return requests;
}

async function openProject(page) {
  await page.locator("#recentList .recent").click();
  await expect(page.locator("#workspace")).toBeVisible();
  await expect(page.locator('[data-photo-id="1"]')).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  const problems = [];
  runtimeProblems.set(page, problems);
  page.on("pageerror", error => problems.push(`pageerror: ${error.message}`));
  page.on("console", message => {
    if (message.type() === "error" && !message.text().includes("Failed to load resource")) {
      problems.push(`console: ${message.text()}`);
    }
  });
});

test.afterEach(async ({ page }) => {
  expect(runtimeProblems.get(page)).toEqual([]);
});

test("首页加载全部脚本并异步渲染最近项目", async ({ page }) => {
  await openApp(page);

  await expect(page).toHaveTitle("Cullumi");
  await expect(page.locator("#appVersion")).toHaveText("v1.0.1");
  await expect(page.locator("#chooseBtn svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=2#home-folder");
  await expect(page.locator("#recentList .recent-meta")).toContainText("2 张");
  await expect(page.locator("#recentList .recent-thumb img")).toHaveCount(1);
  await expect(page.locator("#recentList .recent-more svg use").first()).toHaveAttribute("href", "/static/assets/icons.svg?v=3#home-more");

  const scripts = await page.locator("script[src]").evaluateAll(nodes =>
    nodes.map(node => new URL(node.src).pathname.split("/").pop())
  );
  expect(scripts).toEqual(["runtime.js", "session.js", "similar.js", "settings.js", "gallery.js", "app.js"]);

  await page.locator("#recentSearch").fill("不存在的项目");
  await expect(page.locator("#recentList")).toContainText("没有匹配的项目");
  await page.locator("#recentSearch").fill("夏日");
  await expect(page.locator("#recentList .recent")).toHaveCount(1);
});

test("项目照片可以通过真实卡片交互标记为移除", async ({ page }) => {
  const requests = await openApp(page);
  await openProject(page);

  const decisionFilter = page.locator('[data-filter-menu="decisions"] .multi-filter-trigger');
  const decisionChevron = decisionFilter.locator(".filter-chevron");
  await expect(decisionFilter.locator(".filter-chevron-down")).toBeVisible();
  await expect(decisionFilter.locator(".filter-chevron-down")).toHaveAttribute("href", "/static/assets/icons.svg?v=1#chevron-down");
  await expect(decisionChevron).toHaveCSS("transform", "none");
  await decisionFilter.click();
  await expect(decisionFilter).toHaveAttribute("aria-expanded", "true");
  await expect(decisionChevron).toHaveCSS("transform", "matrix(-1, 0, 0, -1, 0, 0)");
  await decisionFilter.click();
  await expect(decisionChevron).toHaveCSS("transform", "none");

  await page.locator('[data-photo-id="1"] [data-decision="remove"]').click();
  await expect(page.locator('[data-photo-id="1"]')).toHaveClass(/decision-remove/);
  await expect(page.locator("#toast")).toContainText("已标记移除");

  const decisionRequest = requests.find(request => request.path === "/api/decision");
  expect(decisionRequest?.body).toMatchObject({
    project_id: "project-1",
    photo_id: 1,
    decision: "remove",
  });
});

test("自定义模式恢复按钮使用统一图标并停留在字段标题行", async ({ page }) => {
  await openApp(page);
  await openProject(page);
  await page.locator("#settingsBtn").click();
  await page.locator('[data-setting="profiles"]').click();

  const fields = page.locator(".form-grid [data-p]");
  const resets = page.locator(".form-grid .field-reset");
  expect(await resets.count()).toBe(await fields.count());
  await expect(resets.first().locator("svg use")).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=1#motion-reset",
  );
  await expect(resets.first().locator("svg")).toHaveCSS("width", "14px");

  const selectLabel = page.locator(".form-grid label:has(select[data-p])").first();
  const positions = await selectLabel.evaluate(label => {
    const reset = label.querySelector(".field-reset").getBoundingClientRect();
    const field = label.querySelector(".form-select").getBoundingClientRect();
    return { resetBottom: reset.bottom, fieldTop: field.top };
  });
  expect(positions.resetBottom).toBeLessThanOrEqual(positions.fieldTop);
});

test("放大预览中的决定局部同步到图库且不刷新完整项目", async ({ page }) => {
  const requests = await openApp(page);
  await openProject(page);
  const projectRequestsBefore = requests.filter(request => request.path === "/api/project").length;

  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await expect(page.locator("#viewer")).toBeVisible();
  await page.locator("#viewerRemove").click();
  await expect(page.locator("#viewerRemove")).toHaveClass(/active/);
  await page.locator('#viewer [data-close]').click();

  await expect(page.locator('[data-photo-id="1"]')).toHaveClass(/decision-remove/);
  await expect(page.locator("#removeCount")).toHaveText("1");
  expect(requests.filter(request => request.path === "/api/project")).toHaveLength(projectRequestsBefore);
});

test("放大预览连续决定多张照片后批量同步图库和计数", async ({ page }) => {
  const requests = await openApp(page, { photoCount: 2 });
  await openProject(page);
  const projectRequestsBefore = requests.filter(request => request.path === "/api/project").length;

  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await page.locator("#viewerRemove").click();
  await page.locator("#viewerNext").click();
  await page.locator("#viewerKeep").click();
  await page.locator('#viewer [data-close]').click();

  await expect(page.locator('[data-photo-id="1"]')).toHaveClass(/decision-remove/);
  await expect(page.locator('[data-photo-id="2"]')).toHaveClass(/decision-keep/);
  await expect(page.locator("#removeCount")).toHaveText("1");
  await expect(page.locator("#keepCount")).toHaveText("1");
  expect(requests.filter(request => request.path === "/api/project")).toHaveLength(projectRequestsBefore);
});

test("预览决定不再符合当前筛选时只移除对应卡片", async ({ page }) => {
  const requests = await openApp(page, { photoCount: 2 });
  await openProject(page);
  await page.locator('[data-nav="undecided"]').click();
  await expect(page.locator('[data-photo-id="1"]')).toBeVisible();
  const projectRequestsBefore = requests.filter(request => request.path === "/api/project").length;

  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await page.locator("#viewerRemove").click();
  await page.locator('#viewer [data-close]').click();

  await expect(page.locator('[data-photo-id="1"]')).toHaveCount(0);
  await expect(page.locator('[data-photo-id="2"]')).toBeVisible();
  await expect(page.locator("#viewSubtitle")).toHaveText("显示 1 / 1");
  await expect(page.locator("#removeCount")).toHaveText("1");
  expect(requests.filter(request => request.path === "/api/project")).toHaveLength(projectRequestsBefore);
});

test("决定保存失败时保持图库和预览原状态", async ({ page }) => {
  await openApp(page, { decisionFails: true });
  await openProject(page);

  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await page.locator("#viewerRemove").click();

  await expect(page.locator("#toast")).toContainText("保存决定失败：数据库暂时不可写");
  await expect(page.locator("#viewerRemove")).not.toHaveClass(/active/);
  await page.locator('#viewer [data-close]').click();
  await expect(page.locator('[data-photo-id="1"]')).not.toHaveClass(/decision-remove/);
});

test("增量加载后的照片继续使用同一个委托事件入口", async ({ page }) => {
  const requests = await openApp(page, { photoCount: 125 });
  await openProject(page);
  await page.locator("#librarySentinel").scrollIntoViewIfNeeded();
  await expect(page.locator('[data-photo-id="125"]')).toBeVisible();

  await page.locator('[data-photo-id="125"] [data-decision="remove"]').click();
  await expect(page.locator('[data-photo-id="125"]')).toHaveClass(/decision-remove/);
  expect(requests.filter(request => request.path === "/api/photos").length).toBeGreaterThanOrEqual(2);
});

test("连续加载五页图库时容器监听器数量保持不变", async ({ page }) => {
  await page.addInitScript(() => {
    const original = EventTarget.prototype.addEventListener;
    globalThis.__cullumiContainerListeners = {};
    EventTarget.prototype.addEventListener = function(type, listener, options) {
      if (this instanceof HTMLElement && ["gallery", "similarDetailGallery"].includes(this.id)) {
        const key = `${this.id}:${type}`;
        globalThis.__cullumiContainerListeners[key] =
          (globalThis.__cullumiContainerListeners[key] || 0) + 1;
      }
      return original.call(this, type, listener, options);
    };
  });
  await openApp(page, { photoCount: 605 });
  await openProject(page);
  const listenersBefore = await page.evaluate(() => globalThis.__cullumiContainerListeners);

  for (const expected of [240, 360, 480, 600]) {
    await page.locator("#librarySentinel").scrollIntoViewIfNeeded();
    await expect.poll(() => page.locator("[data-photo-id]").count()).toBeGreaterThanOrEqual(expected);
  }

  const listenersAfter = await page.evaluate(() => globalThis.__cullumiContainerListeners);
  expect(listenersAfter).toEqual(listenersBefore);
  expect(listenersAfter["gallery:click"]).toBe(1);
  expect(listenersAfter["similarDetailGallery:click"]).toBe(1);
});

test("查看器可以在真实照片集合中前后导航", async ({ page }) => {
  await openApp(page, { photoCount: 2 });
  await openProject(page);
  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await expect(page.locator("#viewerName")).toHaveText("海边-1.jpg");
  await expect(page.locator('#viewer [data-close] svg use')).toHaveAttribute("href", "/static/assets/icons.svg?v=1#viewer-close");
  await expect(page.locator("#viewerPrev svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=1#viewer-prev");
  await expect(page.locator("#viewerNext svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=1#viewer-next");
  await expect(page.locator("#viewerBadge.badge-review")).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(page.locator("#viewerBadge.badge-review")).toHaveCSS("color", "rgb(166, 111, 0)");
  await expect(page.locator("#viewerBadge.badge-review")).toHaveCSS("border-color", "rgb(166, 111, 0)");
  await page.evaluate(() => { state.items[0].suggestion = "remove"; openViewer(0); });
  await expect(page.locator("#viewerBadge.badge-remove")).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(page.locator("#viewerBadge.badge-remove")).toHaveCSS("color", "rgb(174, 67, 30)");
  await expect(page.locator("#viewerBadge.badge-remove")).toHaveCSS("border-color", "rgb(174, 67, 30)");
  await page.locator("#viewerNext").click();
  await expect(page.locator("#viewerName")).toHaveText("海边-2.jpg");
  await page.locator("#viewerPrev").click();
  await expect(page.locator("#viewerName")).toHaveText("海边-1.jpg");
});

test("动态照片使用 SVG 控件并支持播放、缩放和末帧封面", async ({ page }) => {
  const requests = await openApp(page, { motionPhoto: true, photoCount: 1 });
  await openProject(page);

  const cardMark = page.locator('[data-photo-id="1"] .card-live-mark');
  await expect(cardMark.locator(".live-photo-ring")).toHaveAttribute("fill", "none");
  await expect(cardMark.locator(".live-photo-ring")).toHaveAttribute("r", "4.2");
  await expect(cardMark.locator(".live-photo-dot")).toHaveCount(16);
  await expect(cardMark.locator(".live-photo-dot").first()).toHaveAttribute("r", ".62");
  await expect(cardMark).toHaveCSS("left", "10px");
  await expect(cardMark).toHaveCSS("bottom", "10px");
  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await expect(page.locator("#motionControls")).toBeVisible();
  await expect(page.locator("#motionMute")).toHaveAttribute("aria-label", "播放声音");
  await expect(page.locator("#motionMute svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=1#motion-muted");
  expect(await page.locator("#motionMute").evaluate(button => button.previousElementSibling?.id)).toBe("motionTimelineWrap");
  expect(await page.locator("#motionSetCover").evaluate(button => button.previousElementSibling?.id)).toBe("motionMute");
  expect(await page.locator("#motionResetCover").evaluate(button => button.previousElementSibling?.id)).toBe("motionSetCover");
  await expect(page.locator("#motionSetCover")).toHaveAttribute("aria-label", "设为封面");
  await expect(page.locator("#motionSetCover svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=2#motion-set-cover");
  await expect(page.locator("#motionSetCover")).toHaveText("");
  await expect(page.locator("#motionRecommend")).toHaveCount(0);
  await expect(page.locator("#motionResetCover")).toHaveAttribute("aria-label", "恢复原始封面");
  await expect(page.locator("#motionResetCover svg")).toHaveAttribute("viewBox", "0 0 1024 1024");
  await expect(page.locator("#motionResetCover svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=1#motion-reset");
  await expect(page.locator("#motionResetCover svg use")).toHaveAttribute("transform", "translate(1024 0) scale(-1 1)");
  await expect(page.locator("#motionResetCover")).toHaveText("");
  await expect(page.locator("#motionCoverMarker")).toBeVisible();
  await expect(page.locator("#motionOriginalMarker")).toBeHidden();
  const initialCoverPosition = await page.locator("#motionTimelineWrap").evaluate(wrap => getComputedStyle(wrap).getPropertyValue("--motion-cover-percent").trim());
  expect(initialCoverPosition).toBe("0%");
  const initialMarkerAlignment = await page.evaluate(() => {
    const timeline = document.querySelector("#motionTimeline").getBoundingClientRect();
    const marker = document.querySelector("#motionCoverMarker").getBoundingClientRect();
    return marker.left + marker.width / 2 - (timeline.left + 6);
  });
  expect(Math.abs(initialMarkerAlignment)).toBeLessThanOrEqual(0.5);
  await expect(page.locator("#viewerVideo")).toHaveCSS("position", "absolute");
  const motionLayout = await page.evaluate(() => {
    const media = document.querySelector(".viewer-media").getBoundingClientRect();
    const video = document.querySelector("#viewerVideo").getBoundingClientRect();
    const controls = document.querySelector("#motionControls").getBoundingClientRect();
    const figure = document.querySelector("#viewer figure").getBoundingClientRect();
    return {
      media: { top: media.top, width: media.width, height: media.height, bottom: media.bottom },
      video: { top: video.top, width: video.width, height: video.height },
      controls: { top: controls.top, bottom: controls.bottom },
      figure: { bottom: figure.bottom },
    };
  });
  expect(motionLayout.video.width).toBeCloseTo(motionLayout.media.width, 0);
  expect(motionLayout.video.top).toBeCloseTo(motionLayout.media.top + 8, 0);
  expect(motionLayout.video.height).toBeCloseTo(motionLayout.media.height - 8, 0);
  expect(motionLayout.controls.top).toBeGreaterThanOrEqual(motionLayout.media.bottom - 1);
  expect(motionLayout.controls.bottom).toBeLessThanOrEqual(motionLayout.figure.bottom + 1);
  await expect(page.locator("#viewerBadge.viewer-live-mark .live-photo-ring")).toHaveCount(1);
  await expect(page.locator("#viewerBadge.viewer-live-mark .live-photo-dot")).toHaveCount(16);
  await expect(page.locator("#viewerBadge.viewer-live-mark")).toHaveCSS("color", "rgb(0, 0, 0)");
  await expect(page.locator("#motionTimeline")).toHaveAttribute("max", "1166");
  const dayTheme = await page.evaluate(() => ({
    viewer: getComputedStyle(document.querySelector("#viewer")).backgroundColor,
    background: getComputedStyle(document.body).backgroundColor,
    footer: getComputedStyle(document.querySelector("#viewer footer")).backgroundColor,
    footerBorder: getComputedStyle(document.querySelector("#viewer footer")).borderTopWidth,
    button: getComputedStyle(document.querySelector("#motionPlay")).backgroundColor,
    card: getComputedStyle(document.querySelector(".photo-card")).backgroundColor,
  }));
  expect(dayTheme.viewer).toBe(dayTheme.background);
  expect(dayTheme.footer).toBe(dayTheme.viewer);
  expect(dayTheme.footerBorder).toBe("0px");
  expect(dayTheme.button).toBe(dayTheme.card);
  const playAlignment = await page.locator("#motionPlay").evaluate(button => {
    const buttonBox = button.getBoundingClientRect();
    const iconBox = button.querySelector("svg").getBoundingClientRect();
    return {
      x: iconBox.left + iconBox.width / 2 - (buttonBox.left + buttonBox.width / 2),
      y: iconBox.top + iconBox.height / 2 - (buttonBox.top + buttonBox.height / 2),
    };
  });
  expect(Math.abs(playAlignment.x)).toBeLessThanOrEqual(0.5);
  expect(Math.abs(playAlignment.y)).toBeLessThanOrEqual(0.5);
  const resetAlignment = await page.locator("#motionResetCover").evaluate(button => {
    const buttonBox = button.getBoundingClientRect();
    const iconBox = button.querySelector("svg").getBoundingClientRect();
    return {
      x: iconBox.left + iconBox.width / 2 - (buttonBox.left + buttonBox.width / 2),
      y: iconBox.top + iconBox.height / 2 - (buttonBox.top + buttonBox.height / 2),
    };
  });
  expect(Math.abs(resetAlignment.x)).toBeLessThanOrEqual(0.5);
  expect(Math.abs(resetAlignment.y)).toBeLessThanOrEqual(0.5);
  await expect(page.locator("#motionResetCover svg")).toHaveCSS("width", "17px");
  await expect(page.locator("#motionResetCover svg")).toHaveCSS("height", "17px");
  await expect(page.locator("#motionCoverMarker")).toHaveCSS("width", "6px");
  await expect(page.locator("#motionCoverMarker")).toHaveCSS("height", "6px");
  await expect(page.locator("#motionCoverMarker")).toHaveCSS("top", "-4px");
  await expect(page.locator("#motionCoverMarker")).toHaveCSS("box-shadow", "none");
  await expect(page.locator("#motionTimelineWrap")).toHaveCSS("height", "18px");

  await page.locator("#viewerVideo").evaluate(video => {
    video.dataset.testPlaying = "0";
    video.dataset.testCurrent = "1.2";
    Object.defineProperty(video, "paused", {
      configurable: true,
      get() { return this.dataset.testPlaying !== "1"; },
    });
    Object.defineProperty(video, "ended", {
      configurable: true,
      get() { return this.dataset.testEnded === "1"; },
    });
    Object.defineProperty(video, "currentTime", {
      configurable: true,
      get() { return Number(this.dataset.testCurrent || 0); },
      set(value) { this.dataset.testCurrent = String(value); },
    });
    video.play = function play() {
      this.dataset.testPlaying = "1";
      this.dataset.testEnded = "0";
      this.dispatchEvent(new Event("play"));
      return Promise.resolve();
    };
    video.pause = function pause() {
      this.dataset.testPlaying = "0";
      this.dispatchEvent(new Event("pause"));
    };
  });

  await expect(page.locator("#viewerVideo")).toHaveJSProperty("muted", true);
  await page.locator("#motionMute").click();
  await expect(page.locator("#viewerVideo")).toHaveJSProperty("muted", false);
  await expect(page.locator("#motionMute")).toHaveAttribute("aria-label", "静音");
  await expect(page.locator("#motionMute svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=1#motion-sound");
  await page.locator("#motionMute").click();
  await expect(page.locator("#viewerVideo")).toHaveJSProperty("muted", true);
  await expect(page.locator("#motionMute")).toHaveAttribute("aria-label", "播放声音");

  await page.locator("#viewerVideo").click();
  await expect(page.locator("#motionPlay")).toHaveAttribute("aria-label", "暂停");
  await expect(page.locator("#motionPlay svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=2#motion-pause");
  await page.locator("#viewerVideo").click();
  await expect(page.locator("#motionPlay")).toHaveAttribute("aria-label", "播放");
  await expect(page.locator("#motionPlay svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=2#motion-play");
  await page.keyboard.press("Space");
  await expect(page.locator("#motionPlay")).toHaveAttribute("aria-label", "暂停");
  await page.keyboard.press("Space");
  await expect(page.locator("#motionPlay")).toHaveAttribute("aria-label", "播放");

  await page.locator("#viewerVideo").dispatchEvent("wheel", { deltaY: -100, clientX: 640, clientY: 360 });
  await expect(page.locator("#viewerVideo")).toHaveCSS("cursor", "grab");
  await expect(page.locator("#viewerVideo")).toHaveAttribute("style", /scale\(1\.18\)/);

  const dayColors = await page.locator("#motionControls").evaluate(control => {
    const root = getComputedStyle(document.documentElement);
    const style = getComputedStyle(control);
    return [style.getPropertyValue("--motion-accent").trim(), root.getPropertyValue("--pink").trim()];
  });
  expect(dayColors[0]).toBe(dayColors[1]);
  const dayMarkerColors = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const timeline = getComputedStyle(document.querySelector("#motionTimeline"));
    const marker = getComputedStyle(document.querySelector("#motionCoverMarker"));
    return {
      fill: timeline.getPropertyValue("--motion-thumb-fill").trim(),
      marker: marker.backgroundColor,
      ink: root.getPropertyValue("--ink").trim(),
    };
  });
  expect(dayMarkerColors.fill).toBe("#fff");
  expect(dayMarkerColors.marker).toBe("rgb(52, 40, 44)");
  await page.locator("#motionTimeline").hover();
  const hoverColors = await page.locator("#motionTimeline").evaluate(timeline => {
    const root = getComputedStyle(document.documentElement);
    const style = getComputedStyle(timeline);
    return [style.getPropertyValue("--motion-accent").trim(), root.getPropertyValue("--pink-hover").trim()];
  });
  expect(hoverColors[0]).toBe(hoverColors[1]);

  await page.locator("#motionSetCover").click();
  await expect(page.locator("#toast")).toContainText("封面和照片分析已更新");
  await expect(page.locator("#motionOriginalMarker")).toBeVisible();
  const savedDayMarkers = await page.evaluate(() => {
    const rgb = value => {
      const channels = value.match(/[\d.]+/g).slice(0, 3).map(Number);
      return value.startsWith("color(") ? channels.map(channel => channel * 255) : channels;
    };
    const current = rgb(getComputedStyle(document.querySelector("#motionCoverMarker")).backgroundColor);
    const original = rgb(getComputedStyle(document.querySelector("#motionOriginalMarker")).backgroundColor);
    return {
      position: getComputedStyle(document.querySelector("#motionTimelineWrap")).getPropertyValue("--motion-cover-percent").trim(),
      endAlignment: (() => {
        const timeline = document.querySelector("#motionTimeline").getBoundingClientRect();
        const marker = document.querySelector("#motionCoverMarker").getBoundingClientRect();
        return marker.left + marker.width / 2 - (timeline.right - 6);
      })(),
      currentLightness: current.reduce((sum, value) => sum + value, 0),
      originalLightness: original.reduce((sum, value) => sum + value, 0),
    };
  });
  expect(savedDayMarkers.position).toBe("100%");
  expect(Math.abs(savedDayMarkers.endAlignment)).toBeLessThanOrEqual(0.5);
  expect(savedDayMarkers.originalLightness).toBeGreaterThan(savedDayMarkers.currentLightness);

  await page.evaluate(() => applyTheme("night"));
  await page.waitForTimeout(200);
  await expect(page.locator("#viewerBadge.viewer-live-mark")).toHaveCSS("color", "rgb(255, 255, 255)");
  await page.locator("#viewerName").hover();
  const nightBaseColors = await page.locator("#motionTimeline").evaluate(timeline => {
    const root = getComputedStyle(document.documentElement);
    const style = getComputedStyle(timeline);
    return [style.getPropertyValue("--motion-accent").trim(), root.getPropertyValue("--pink").trim()];
  });
  expect(nightBaseColors[0]).toBe(nightBaseColors[1]);
  await page.locator("#motionTimeline").hover();
  const nightHoverColors = await page.locator("#motionTimeline").evaluate(timeline => {
    const root = getComputedStyle(document.documentElement);
    const style = getComputedStyle(timeline);
    return [style.getPropertyValue("--motion-accent").trim(), root.getPropertyValue("--pink-hover").trim()];
  });
  expect(nightHoverColors[0]).toBe(nightHoverColors[1]);
  expect(nightHoverColors[0]).not.toBe(nightBaseColors[0]);
  const savedNightMarkers = await page.evaluate(() => {
    const rgb = value => {
      const channels = value.match(/[\d.]+/g).slice(0, 3).map(Number);
      return value.startsWith("color(") ? channels.map(channel => channel * 255) : channels;
    };
    const root = getComputedStyle(document.documentElement);
    const timeline = getComputedStyle(document.querySelector("#motionTimeline"));
    const current = rgb(getComputedStyle(document.querySelector("#motionCoverMarker")).backgroundColor);
    const original = rgb(getComputedStyle(document.querySelector("#motionOriginalMarker")).backgroundColor);
    return {
      fill: timeline.getPropertyValue("--motion-thumb-fill").trim(),
      currentLightness: current.reduce((sum, value) => sum + value, 0),
      originalLightness: original.reduce((sum, value) => sum + value, 0),
    };
  });
  expect(savedNightMarkers.fill).toBe("#000");
  expect(savedNightMarkers.originalLightness).toBeLessThan(savedNightMarkers.currentLightness);
  const nightTheme = await page.evaluate(() => ({
    viewer: getComputedStyle(document.querySelector("#viewer")).backgroundColor,
    background: getComputedStyle(document.body).backgroundColor,
    footer: getComputedStyle(document.querySelector("#viewer footer")).backgroundColor,
    footerBorder: getComputedStyle(document.querySelector("#viewer footer")).borderTopWidth,
    button: getComputedStyle(document.querySelector("#motionPlay")).backgroundColor,
    card: getComputedStyle(document.querySelector(".photo-card")).backgroundColor,
  }));
  expect(nightTheme.viewer).toBe(nightTheme.background);
  expect(nightTheme.footer).toBe(nightTheme.viewer);
  expect(nightTheme.footerBorder).toBe("0px");
  expect(nightTheme.button).toBe(nightTheme.card);

  const coverRequest = requests.find(request => request.path === "/api/motion/cover");
  expect(coverRequest?.body).toMatchObject({
    project_id: "project-1",
    photo_id: 1,
    source: "motion",
    time_ms: 1166,
  });
});

test("动态照片当前封面标志使用真实帧位置并按需升级旧项目", async ({ page }) => {
  const requests = await openApp(page, {
    motionPhoto: true,
    motionStillTime: -1,
    locatedMotionStillTime: 400,
    photoCount: 1,
  });
  await openProject(page);
  await page.locator('[data-photo-id="1"] [data-open-id]').click();

  await expect(page.locator("#motionTimeline")).toHaveValue("400");
  await expect(page.locator("#motionCoverMarker")).toHaveAttribute("title", "当前封面 · 0:00");
  const coverPosition = await page.locator("#motionTimelineWrap").evaluate(
    wrap => getComputedStyle(wrap).getPropertyValue("--motion-cover-percent").trim(),
  );
  expect(Number.parseFloat(coverPosition)).toBeCloseTo(400 / 1166 * 100, 4);
  await expect(page.locator("#motionOriginalMarker")).toBeHidden();
  expect(requests.find(request => request.path === "/api/motion/locate")?.body).toMatchObject({
    project_id: "project-1",
    photo_id: 1,
  });
});

test("动态照片打开时定格当前封面并在修改后同步分析标识", async ({ page }) => {
  await openApp(page, {
    motionPhoto: true,
    motionStillTime: 400,
    motionCoverSuggestion: "remove",
    photoCount: 1,
  });
  await openProject(page);
  await page.locator("#viewerVideo").evaluate(video => {
    video.dataset.testCurrent = "0";
    video.dataset.testPlaying = "0";
    Object.defineProperty(video, "readyState", { configurable: true, get() { return 1; } });
    Object.defineProperty(video, "currentTime", {
      configurable: true,
      get() { return Number(this.dataset.testCurrent || 0); },
      set(value) { this.dataset.testCurrent = String(value); },
    });
    Object.defineProperty(video, "paused", {
      configurable: true,
      get() { return this.dataset.testPlaying !== "1"; },
    });
    video.pause = function pause() {
      this.dataset.testPlaying = "0";
      this.dispatchEvent(new Event("pause"));
    };
  });

  await expect(page.locator('[data-photo-id="1"] [data-analysis-badge]')).toHaveText("人工复查");
  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await expect(page.locator("#viewerVideo")).toHaveAttribute("data-test-current", "0.4");
  await expect(page.locator("#motionPlay")).toHaveAttribute("aria-label", "播放");
  await expect(page.locator("#viewerAnalysisBadge.badge-review")).toHaveText("人工复查");

  await page.locator("#viewerVideo").evaluate(video => { video.currentTime = 0.8; });
  await page.locator("#motionSetCover").click();

  await expect(page.locator('[data-photo-id="1"] [data-analysis-badge].badge-remove')).toHaveText("建议移除");
  await expect(page.locator("#viewerAnalysisBadge.badge-remove")).toHaveText("建议移除");
  await expect(page.locator("#viewerMeta")).toContainText("严重失焦");
});

test("动态封面修改提醒可以记住确认修改", async ({ page }) => {
  const requests = await openApp(page, {
    motionPhoto: true,
    writebackMode: "ask",
    photoCount: 1,
  });
  await openProject(page);
  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await page.locator("#motionSetCover").click();

  await expect(page.locator("#motionWritebackConfirm")).toBeVisible();
  await expect(page.locator("#motionWritebackYes")).toHaveText("确认修改");
  await expect(page.locator("#motionWritebackNo")).toHaveText("不修改");
  await page.locator("#motionWritebackDontAsk").check();
  await page.locator("#motionWritebackYes").click();

  await expect(page.locator("#toast")).toContainText("原图已备份并修改");
  expect(requests.find(request => request.path === "/api/settings" && request.body?.motion_cover_writeback)?.body).toMatchObject({
    motion_cover_writeback: "always",
  });
  expect(requests.find(request => request.path === "/api/motion/cover")?.body).toMatchObject({
    write_source: true,
  });
  await expect(page.locator("#motionCoverWriteback")).toHaveValue("always");

  const before = requests.filter(request => request.path === "/api/motion/cover").length;
  await page.locator("#motionSetCover").click();
  await expect.poll(() => requests.filter(request => request.path === "/api/motion/cover").length).toBe(before + 1);
  await expect(page.locator("#motionWritebackConfirm")).toBeHidden();
});

test("动态封面修改提醒可以记住不修改", async ({ page }) => {
  const requests = await openApp(page, {
    motionPhoto: true,
    writebackMode: "ask",
    photoCount: 1,
  });
  await openProject(page);
  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await page.locator("#motionSetCover").click();
  await page.locator("#motionWritebackDontAsk").check();
  await page.locator("#motionWritebackNo").click();

  await expect.poll(() => requests.some(request => request.path === "/api/motion/cover")).toBe(true);
  expect(requests.find(request => request.path === "/api/settings" && request.body?.motion_cover_writeback)?.body).toMatchObject({
    motion_cover_writeback: "never",
  });
  expect(requests.find(request => request.path === "/api/motion/cover")?.body).toMatchObject({
    write_source: false,
  });
  await expect(page.locator("#motionCoverWriteback")).toHaveValue("never");
});

test("隔离历史可以通过委托事件恢复批次", async ({ page }) => {
  const requests = await openApp(page);
  await openProject(page);
  await page.locator('[data-nav="quarantine"]').click();
  await expect(page.locator('[data-restore="batch-1"]')).toBeVisible();
  await page.locator('[data-restore="batch-1"]').click();
  await expect(page.locator("#toast")).toContainText("恢复 1 张");
  expect(requests.find(request => request.path === "/api/quarantine/restore")?.body).toMatchObject({
    project_id: "project-1",
    batch_id: "batch-1",
  });
});

test("存储迁移失败提示显示在设置对话框顶层", async ({ page }) => {
  await openApp(page, { cacheMigrationFails: true });
  await openProject(page);

  await page.locator("#settingsBtn").click();
  await expect(page.locator("#settings")).toBeVisible();
  await page.locator("#projectCacheBtn").click();

  await expect(page.locator("#settings > #toast")).toContainText("迁移失败：目标文件夹无法写入");
  await expect(page.locator("#settings > #toast")).toBeVisible();
  await expect(page.locator("#settings")).toHaveAttribute("open", "");
});

test("使用中的自定义模式显示切换模式警告", async ({ page }) => {
  await openApp(page);
  await openProject(page);

  await page.locator("#settingsBtn").click();
  await page.locator('[data-setting="profiles"]').click();
  await expect(page.locator("#profileEditorSelect")).toHaveValue("custom-portrait");
  await page.locator("#deleteProfile").click();

  await expect(page.locator("#profileInUseWarning")).toBeVisible();
  await expect(page.locator("#profileInUseWarningBody")).toContainText("正在被当前项目使用");
  await expect(page.locator("#profileInUseWarningBody")).toContainText("切换到其他分析模式");
  await expect(page.locator("#confirm")).not.toBeVisible();
});

test("日夜主题与关键工作区保持视觉回归", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await openApp(page);
  await expect(page).toHaveScreenshot("home-day.png", { animations: "disabled" });

  await page.locator("#themeBtn").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "night");
  await expect(page).toHaveScreenshot("home-night.png", { animations: "disabled" });

  await openProject(page);
  await expect(page).toHaveScreenshot("library-night.png", { animations: "disabled" });

  await page.locator('[data-nav="similar"]').click();
  await expect(page.locator('[data-similar-group="similar-1"]')).toBeVisible();
  await expect(page).toHaveScreenshot("similar-groups-night.png", { animations: "disabled" });

  await page.locator("#settingsBtn").click();
  await expect(page.locator("#settings")).toBeVisible();
  await expect(page).toHaveScreenshot("settings-night.png", { animations: "disabled" });
  await page.locator('#settings [data-close]').click();

  await page.locator("#quarantineBtn").click();
  await expect(page.locator("#confirm")).toBeVisible();
  await expect(page).toHaveScreenshot("confirm-night.png", { animations: "disabled" });
});
