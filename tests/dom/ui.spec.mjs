import { expect, test } from "playwright/test";


const token = process.env.CULLUMI_DOM_TOKEN || "cullumi-dom-test";
const image = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='32' height='24'%3E%3Crect width='32' height='24' fill='%23d9b7bd'/%3E%3C/svg%3E";
const runtimeProblems = new WeakMap();

const profiles = [
  { id: "conservative", name: "保守筛选", builtin: true, similarity: { blink: { face_confidence_min: 0.85, open_confidence_min: 0.8, closed_confidence_min: 0.8, min_eye_distance_px: 12, reliable_coverage_min: 0.8 } } },
  { id: "custom-portrait", name: "人像精选", builtin: false, base_mode: "conservative", similarity: { blink: { face_confidence_min: 0.85, open_confidence_min: 0.8, closed_confidence_min: 0.8, min_eye_distance_px: 12, reliable_coverage_min: 0.8 } } },
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
    format_categories: [{ id: "jpeg", label: "JPEG", count: photoCount }],
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
    media_type: "image",
    format_category: "jpeg",
    variant_extensions: [],
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
  let blinkEnabled = options.blinkEnabled ?? true;
  let syncVariantDecisions = options.syncVariantDecisions ?? true;
  const decisions = new Map();
  const requests = [];
  const similarPhoto = id => {
    const photo = photoPayload("", id);
    photo.size = id * 10_000;
    photo.taken = `2026-08-${String((id % 28) + 1).padStart(2, "0")} 10:00:00`;
    const category = options.similarFormats?.[id - 1] || "jpeg";
    if (options.similarFormats) {
      const extension = {
        raw: "CR3",
        jpeg: "JPG",
        heif: "HEIC",
        png: "PNG",
        other: "WEBP",
      }[category];
      photo.relative_path = `旅行/海边-${id}.${extension}`;
      photo.format_category = category;
    }
    if (options.similarVariantExtensions)
      photo.variant_extensions = [...options.similarVariantExtensions];
    if (options.blinkSimilar)
      Object.assign(photo, {
        suggestion: "keep",
        reason: "",
        blink_status: "closed",
        blink_face_count: 1,
        blink_closed_face_count: 1,
        blink_uncertain_face_count: 0,
        blink_closed_ratio: 1,
      });
    return photo;
  };
  const similarMembers = () => {
    const sources = [similarPhoto(1), similarPhoto(2)];
    if (!options.similarVariantPairs) return sources;
    return sources.flatMap((source, index) => {
      const sourceId = source.id;
      source.relative_path = `旅行/IMG_10${index + 1}.JPG`;
      source.format_category = "jpeg";
      source.variant_extensions = ["CR3", "JPG"];
      source.similarity_source_id = sourceId;
      source.is_capture_variant = false;
      const raw = similarPhoto(101 + index);
      raw.relative_path = `旅行/IMG_10${index + 1}.CR3`;
      raw.format_category = "raw";
      raw.variant_extensions = ["CR3", "JPG"];
      raw.similarity_source_id = sourceId;
      raw.is_capture_variant = true;
      return [source, raw];
    });
  };
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postDataJSON?.() || null;
    requests.push({
      path: url.pathname,
      method: request.method(),
      body,
      query: Object.fromEntries(url.searchParams),
    });

    const fulfill = (payload, status = 200) => route.fulfill({
      status,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify(payload),
    });

    if (url.pathname === "/api/bootstrap") {
      return fulfill({
        version: "1.0.3",
        profiles,
        settings: {
          theme: "day",
          auto_advance: options.autoAdvance ?? false,
          auto_check_updates: false,
          blink_detection_enabled: blinkEnabled,
          sync_variant_decisions: syncVariantDecisions,
          motion_cover_writeback: writebackMode,
          default_cache_root: "C:\\Cullumi缓存",
        },
        recent_projects: [recentPayload(false)],
        startup_warning: "",
      });
    }
    if (url.pathname === "/api/recent-project") return fulfill(recentPayload(true));
    if (url.pathname === "/api/project") {
      const project = projectPayload(decision, options.photoCount || 2, decisions);
      if (options.variantPair) {
        project.format_categories = [
          { id: "raw", label: "RAW", count: 1 },
          { id: "jpeg", label: "JPEG", count: 1 },
        ];
      }
      return fulfill(project);
    }
    if (url.pathname === "/api/photos") {
      const selected = url.searchParams.get("decisions") || "all";
      const photoCount = options.photoCount || 1;
      const matching = Array.from({ length: photoCount }, (_, index) => {
        const id = index + 1;
        const photo = options.motionPhoto
          ? motionPhotoPayload(decisions.get(id) || "", id, options.motionStillTime || 0)
          : photoPayload(decisions.get(id) || "", id);
        if (options.variantPair && id <= 2) {
          const raw = id === 2;
          photo.relative_path = raw ? "旅行/IMG_0042.CR3" : "旅行/IMG_0042.JPG";
          photo.format_category = raw ? "raw" : "jpeg";
          photo.variant_extensions = ["CR3", "JPG"];
        }
        return photo;
      }).filter(photo => {
        const current = photo.decision || "undecided";
        const selectedFormats = url.searchParams.get("formats") || "all";
        return (
          (selected === "all" || selected.split(",").includes(current)) &&
          (selectedFormats === "all" ||
            selectedFormats.split(",").includes(photo.format_category))
        );
      });
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || 200);
      return fulfill({ total: matching.length, items: matching.slice(offset, offset + limit) });
    }
    if (url.pathname === "/api/decision") {
      if (options.decisionFails) return fulfill({ error: "数据库暂时不可写" }, 500);
      decision = body.decision;
      const targets =
        options.variantPair && syncVariantDecisions && body.photo_id <= 2
          ? [1, 2]
          : [body.photo_id];
      const previous = new Map(targets.map(id => [id, decisions.get(id) || ""]));
      targets.forEach(id => decisions.set(id, decision));
      const affected = targets.map(id => {
        const photo = photoPayload(decision, id);
        if (options.variantPair) {
          const raw = id === 2;
          photo.relative_path = raw ? "旅行/IMG_0042.CR3" : "旅行/IMG_0042.JPG";
          photo.format_category = raw ? "raw" : "jpeg";
          photo.variant_extensions = ["CR3", "JPG"];
        }
        photo.previous_decision = previous.get(id);
        return photo;
      });
      return fulfill({
        saved: true,
        photo_id: body.photo_id,
        decision,
        affected_photos: affected,
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
      if (typeof body.blink_detection_enabled === "boolean") blinkEnabled = body.blink_detection_enabled;
      if (typeof body.sync_variant_decisions === "boolean") syncVariantDecisions = body.sync_variant_decisions;
      return fulfill({ saved: true, settings: { theme: body.theme || "day", motion_cover_writeback: writebackMode, blink_detection_enabled: blinkEnabled, sync_variant_decisions: syncVariantDecisions }, blink_rescan_required: blinkEnabled && !!options.blinkRescanRequired });
    }
    if (url.pathname === "/api/choose-csv") {
      return fulfill({ path: "C:\\照片\\决定.csv" });
    }
    if (url.pathname === "/api/import") {
      if (options.csvVariantConflict && syncVariantDecisions) {
        return fulfill({
          imported: 0,
          matched: 2,
          missing: 0,
          affected: 0,
          requires_sync_disable: true,
          conflicting_groups: 1,
        });
      }
      return fulfill({
        imported: 2,
        matched: 2,
        missing: 0,
        affected: 2,
        requires_sync_disable: false,
        conflicting_groups: 0,
      });
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
      const sources = [similarPhoto(1), similarPhoto(2)];
      return fulfill({
        total: 1,
        items: [{
          id: "similar-1",
          count: options.similarVariantPairs ? 4 : 2,
          capture_count: 2,
          kind: "similar",
          face_safe: false,
          recommended: sources[0],
          covers: sources,
        }],
      });
    }
    if (url.pathname === "/api/similar-group") {
      const members = similarMembers();
      return fulfill({
        id: "similar-1",
        count: members.length,
        capture_count: 2,
        kind: "similar",
        face_safe: false,
        recommended_id: 1,
        members: members.map(photo => ({
          ...photo,
          group_similarity:
            (photo.similarity_source_id || photo.id) === 2 ? 0.91 : 1,
        })),
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
  await expect(page.locator("#appVersion")).toHaveText("v1.0.3");
  await expect(page.locator("#chooseBtn svg use")).toHaveAttribute("href", "/static/assets/icons.svg?v=2#home-folder");
  await expect(page.locator("#recentList .recent-meta")).toContainText("2 张");
  await expect(page.locator("#recentList .recent-thumb img")).toHaveCount(1);
  await expect(page.locator("#recentList .recent-more svg use").first()).toHaveAttribute("href", "/static/assets/icons.svg?v=3#home-more");

  const scripts = await page.locator("script[src]").evaluateAll(nodes =>
    nodes.map(node => new URL(node.src).pathname.split("/").pop())
  );
  expect(scripts).toEqual(["runtime.js", "session.js", "similar.js", "settings.js", "gallery.js", "viewer.js", "app.js"]);

  await page.locator("#recentSearch").fill("不存在的项目");
  await expect(page.locator("#recentList")).toContainText("没有匹配的项目");
  await page.locator("#recentSearch").fill("夏日");
  await expect(page.locator("#recentList .recent")).toHaveCount(1);
});

test("项目照片可以通过真实卡片交互标记为移除", async ({ page }) => {
  const requests = await openApp(page);
  await openProject(page);

  const viewMenu = page.locator('[data-filter-menu="view"]');
  const viewTrigger = viewMenu.locator(".gallery-tool-trigger");
  await expect(viewTrigger.locator("svg use").first()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=7#gallery-filter",
  );
  await expect(viewTrigger.locator("svg use").last()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=1#chevron-down",
  );
  await viewTrigger.click();
  await expect(viewTrigger).toHaveAttribute("aria-expanded", "true");
  const decisionOption = viewMenu.locator(".gallery-view-item").first();
  await decisionOption.locator(".gallery-view-option").hover();
  await expect(decisionOption.locator(".gallery-view-submenu")).toBeVisible();
  const positions = await decisionOption.evaluate(item => {
    const option = item.querySelector(".gallery-view-option").getBoundingClientRect();
    const submenu = item.querySelector(".gallery-view-submenu").getBoundingClientRect();
    return { optionRight: option.right, submenuLeft: submenu.left };
  });
  expect(positions.submenuLeft).toBeGreaterThan(positions.optionRight);

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

test("照片库仅显示项目存在的格式选项并可筛选 RAW", async ({ page }) => {
  const requests = await openApp(page, { variantPair: true, photoCount: 2 });
  await openProject(page);

  const viewMenu = page.locator('[data-filter-menu="view"]');
  await viewMenu.locator(".gallery-tool-trigger").click();
  const formatFilter = page.locator("#formatFilter");
  await expect(formatFilter).toBeVisible();
  await formatFilter.locator(".gallery-view-option").hover();
  await expect(formatFilter.locator(".gallery-view-submenu")).toBeVisible();
  await expect(formatFilter.locator('[data-format-option="raw"]')).toBeVisible();
  await expect(formatFilter.locator('[data-format-option="jpeg"]')).toBeVisible();
  await expect(formatFilter.locator('[data-format-option="heif"]')).toBeHidden();
  await expect(page.locator(".variant-badge")).toHaveCount(2);
  await expect(page.locator(".variant-badge").first()).toHaveText("CR3 + JPG");

  await formatFilter.locator('input[value="jpeg"]').uncheck();
  await expect(page.locator('[data-photo-id="1"]')).toHaveCount(0);
  await expect(page.locator('[data-photo-id="2"]')).toBeVisible();
  await expect(formatFilter.locator("#formatFilterSummary")).toHaveText("RAW");
  await expect.poll(() =>
    requests.filter(request => request.path === "/api/photos").at(-1)?.query?.formats,
  ).toBe("raw");
});

test("照片库多选项全部选中时可以一键全不选并恢复全选", async ({ page }) => {
  await openApp(page);
  await openProject(page);

  const viewMenu = page.locator('[data-filter-menu="view"]');
  await viewMenu.locator(".gallery-tool-trigger").click();
  const menu = viewMenu.locator(".gallery-view-item").first();
  await menu.locator(".gallery-view-option").hover();
  const toggle = menu.locator('[data-select-all="decisions"]');
  await expect(toggle).toHaveText("全不选");

  await toggle.click();
  await expect(menu.locator('input[type="checkbox"]:checked')).toHaveCount(0);
  await expect(toggle).toHaveText("全选");
  await expect(page.locator("#emptyTitle")).toHaveText("当前筛选没有结果");

  await toggle.click();
  await expect(menu.locator('input[type="checkbox"]:checked')).toHaveCount(3);
  await expect(toggle).toHaveText("全不选");
  await expect(page.locator('[data-photo-id="1"]')).toBeVisible();
});

test("照片库排序菜单使用实心圆点单选样式并传递排序方向", async ({ page }) => {
  const requests = await openApp(page, { photoCount: 3 });
  await openProject(page);

  const sortMenu = page.locator('[data-filter-menu="sort"]');
  const trigger = sortMenu.locator(".gallery-tool-trigger");
  await expect(trigger.locator("svg use").first()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=7#gallery-sort",
  );
  await expect(trigger.locator("svg use").last()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=1#chevron-down",
  );
  const centered = await page
    .locator("#libraryFilters .gallery-tool-trigger")
    .evaluateAll(buttons => buttons.map(button => {
      const buttonBox = button.getBoundingClientRect();
      const contentBoxes = [...button.children].map(child =>
        child.getBoundingClientRect(),
      );
      return {
        horizontal: Math.abs(
          (buttonBox.left + buttonBox.right) / 2 -
          (Math.min(...contentBoxes.map(box => box.left)) +
            Math.max(...contentBoxes.map(box => box.right))) / 2,
        ),
        vertical: Math.abs(
          (buttonBox.top + buttonBox.bottom) / 2 -
          (Math.min(...contentBoxes.map(box => box.top)) +
            Math.max(...contentBoxes.map(box => box.bottom))) / 2,
        ),
      };
    }));
  centered.forEach(offset => {
    expect(offset.horizontal).toBeLessThan(1);
    expect(offset.vertical).toBeLessThan(1);
  });
  await trigger.click();
  await expect(sortMenu.locator('[data-sort-value="suggestion"]')).toBeChecked();
  await expect(sortMenu.locator('[data-sort-direction="asc"]')).toBeChecked();
  await expect(sortMenu.locator("input").first()).toHaveAttribute("type", "radio");
  const initialDots = await sortMenu.locator("input").evaluateAll(inputs =>
    inputs.map(input => getComputedStyle(input).backgroundImage),
  );
  expect(initialDots[0]).not.toBe("none");
  expect(initialDots[1]).toBe("none");
  expect(
    requests.filter(request => request.path === "/api/photos").at(-1)?.query
      ?.sort,
  ).toBe("suggestion");
  expect(
    requests.filter(request => request.path === "/api/photos").at(-1)?.query
      ?.direction,
  ).toBe("asc");

  await sortMenu.locator('[data-sort-value="size"]').check();
  await sortMenu.locator('[data-sort-direction="desc"]').check();
  await expect(sortMenu.locator('[data-sort-value="suggestion"]')).not.toBeChecked();
  await expect(sortMenu.locator('[data-sort-direction="asc"]')).not.toBeChecked();
  const changedDots = await sortMenu.locator("input").evaluateAll(inputs =>
    inputs.map(input => getComputedStyle(input).backgroundImage),
  );
  expect(changedDots[0]).toBe("none");
  expect(changedDots[2]).not.toBe("none");
  expect(changedDots[4]).toBe("none");
  expect(changedDots[5]).not.toBe("none");
  await expect.poll(() => {
    const query = requests.filter(request => request.path === "/api/photos").at(-1)?.query;
    return `${query?.sort}:${query?.direction}`;
  }).toBe("size:desc");
});

test("相似组使用照片库同款查看排序组件并组合筛选 RAW", async ({ page }) => {
  await openApp(page, {
    similarVariantPairs: true,
  });
  await openProject(page);
  await page.locator('[data-nav="similar"]').click();
  await page.locator('[data-similar-group="similar-1"]').click();

  await expect(page.locator("#similarFormatFilter")).toHaveCount(0);
  const viewTool = page.locator("#similarViewTool");
  const sortTool = page.locator("#similarSortTool");
  await expect(viewTool).toBeVisible();
  await expect(sortTool).toBeVisible();
  await expect(viewTool.locator(".gallery-tool-trigger svg use").first()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=7#gallery-filter",
  );
  await expect(viewTool.locator(".gallery-tool-trigger svg use").last()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=1#chevron-down",
  );
  await expect(sortTool.locator(".gallery-tool-trigger svg use").first()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=7#gallery-sort",
  );
  await expect(sortTool.locator(".gallery-tool-trigger svg use").last()).toHaveAttribute(
    "href",
    "/static/assets/icons.svg?v=1#chevron-down",
  );
  await viewTool.locator(".gallery-tool-trigger").click();

  const decisionFilter = viewTool.locator(".gallery-view-item").nth(0);
  await decisionFilter.locator(".gallery-view-option").hover();
  await expect(decisionFilter.locator(".gallery-view-submenu")).toBeVisible();
  await expect(decisionFilter.locator('[data-similar-select-all="decisions"]')).toHaveText("全不选");
  await decisionFilter.locator('input[value="undecided"]').uncheck();
  await expect(page.locator("#similarDetailGallery [data-photo-id]")).toHaveCount(0);
  await decisionFilter.locator('input[value="undecided"]').check();

  const aiFilter = viewTool.locator(".gallery-view-item").nth(1);
  await aiFilter.locator(".gallery-view-option").hover();
  await expect(aiFilter.locator(".gallery-view-submenu")).toBeVisible();
  await aiFilter.locator('input[value="review"]').uncheck();
  await expect(page.locator("#similarDetailGallery [data-photo-id]")).toHaveCount(0);
  await aiFilter.locator('input[value="review"]').check();

  const formatFilter = page.locator("#similarFormatViewItem");
  await expect(formatFilter).toBeVisible();
  await formatFilter.locator(".gallery-view-option").hover();
  await expect(formatFilter.locator(".gallery-view-submenu")).toBeVisible();
  await expect(formatFilter.locator('[data-similar-format-option="jpeg"]')).toBeVisible();
  await expect(formatFilter.locator('[data-similar-format-option="raw"]')).toBeVisible();
  await expect(formatFilter.locator('[data-similar-format-option="heif"]')).toBeHidden();
  await expect(formatFilter.locator('[data-similar-select-all="formats"]')).toHaveText("全不选");
  await expect(page.locator('#similarDetailGallery [data-photo-id="101"]')).toBeVisible();
  await expect(page.locator('#similarDetailGallery [data-photo-id="102"]')).toBeVisible();
  await expect(page.locator('#similarDetailGallery [data-photo-id="101"] [data-context-badge]')).toHaveText("推荐保留");

  await formatFilter.locator('input[value="jpeg"]').uncheck();
  await expect(page.locator('#similarDetailGallery [data-photo-id="1"]')).toHaveCount(0);
  await expect(page.locator('#similarDetailGallery [data-photo-id="101"]')).toBeVisible();
  await expect(page.locator("#similarFormatFilterSummary")).toHaveText("RAW");

  await page.locator("#searchInput").fill("IMG_102");
  await expect(page.locator("#similarDetailGallery [data-photo-id]")).toHaveCount(1);
  await expect(page.locator('#similarDetailGallery [data-photo-id="102"]')).toBeVisible();
  await page.locator("#searchInput").fill("");
  await expect(page.locator('#similarDetailGallery [data-photo-id="101"]')).toBeVisible();

  await viewTool.locator(".gallery-tool-trigger").click();
  await sortTool.locator(".gallery-tool-trigger").click();
  await expect(sortTool.locator('[data-similar-sort-value="suggestion"]')).toBeChecked();
  await expect(sortTool.locator('[data-similar-sort-direction="asc"]')).toBeChecked();
  await expect(sortTool.locator("input").first()).toHaveAttribute("type", "radio");
  await sortTool.locator('[data-similar-sort-value="size"]').check();
  await sortTool.locator('[data-similar-sort-direction="desc"]').check();
  await expect(page.locator("#similarDetailGallery [data-photo-id]").first()).toHaveAttribute("data-photo-id", "102");

  await page.locator("#similarExpandBtn").click();
  await expect(page.locator("#similarBackBtn")).toBeVisible();
  const positions = await page.evaluate(() => {
    const back = document.querySelector("#similarBackBtn").getBoundingClientRect();
    const view = document.querySelector("#similarViewTool .gallery-tool-trigger").getBoundingClientRect();
    const sort = document.querySelector("#similarSortTool .gallery-tool-trigger").getBoundingClientRect();
    return { backRight: back.right, viewLeft: view.left, viewRight: view.right, sortLeft: sort.left };
  });
  expect(positions.viewLeft).toBeGreaterThan(positions.backRight);
  expect(positions.sortLeft).toBeGreaterThan(positions.viewRight);
});

test("多格式决定同步更新卡片且自动前进跳过关联格式", async ({ page }) => {
  await openApp(page, {
    variantPair: true,
    photoCount: 3,
    autoAdvance: true,
  });
  await openProject(page);

  await page.locator('[data-photo-id="1"] [data-open-id]').click();
  await expect(page.locator("#viewerName")).toHaveText("IMG_0042.JPG");
  await expect(page.locator("#viewerVariantBadge")).toHaveText("CR3 + JPG");
  await page.locator("#viewerRemove").click();
  await expect(page.locator("#toast")).toContainText("已同步将 2 个格式标记为移除");
  await expect(page.locator("#viewerName")).toHaveText("海边-3.jpg");
  await page.locator('#viewer [data-close]').click();

  await expect(page.locator('[data-photo-id="1"]')).toHaveClass(/decision-remove/);
  await expect(page.locator('[data-photo-id="2"]')).toHaveClass(/decision-remove/);
});

test("CSV 多格式冲突需关闭同步后才按文件导入", async ({ page }) => {
  const requests = await openApp(page, { csvVariantConflict: true });
  await openProject(page);

  await page.locator("#importBtn").click();
  await expect(page.locator("#confirm")).toBeVisible();
  await expect(page.locator("#confirmTitle")).toHaveText("CSV 中存在多格式决定冲突");
  await expect(page.locator("#confirmBody")).toContainText("1 组");
  await page.locator("#confirmOk").click();
  await expect(page.locator("#confirm")).toBeHidden();
  await expect(page.locator("#syncVariantDecisions")).not.toBeChecked();
  await expect.poll(
    () => requests.filter(request => request.path === "/api/import").length,
  ).toBe(2);
  expect(
    requests.findLast(request => request.path === "/api/settings")?.body,
  ).toMatchObject({ sync_variant_decisions: false });
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

test("眨眼检测重新开启时按项目状态提示需要重新扫描", async ({ page }) => {
  const requests = await openApp(page, { blinkRescanRequired: true });
  await openProject(page);
  await page.locator("#settingsBtn").click();

  const toggle = page.locator("#blinkDetectionEnabled");
  await expect(toggle).toBeChecked();
  await page.locator('label[aria-label="启用眨眼检测"]').click();
  await expect(toggle).not.toBeChecked();
  await expect(page.locator("#blinkRescanStatus")).toBeHidden();
  await expect.poll(() => requests.findLast(request => request.path === "/api/settings")?.body?.blink_detection_enabled).toBe(false);
  await page.locator('label[aria-label="启用眨眼检测"]').click();
  await expect(toggle).toBeChecked();
  await expect(page.locator("#blinkRescanStatus")).toHaveText("需要重新扫描");
  await expect(page.locator("#blinkRescanStatus")).toBeVisible();
  await expect.poll(() => requests.findLast(request => request.path === "/api/settings")?.body?.project_id).toBe("project-1");

  await page.locator('[data-setting="profiles"]').click();
  const threshold = page.locator('[data-p="similarity.blink.face_confidence_min"]');
  await expect(threshold).toHaveAttribute("min", "0.5");
  await expect(threshold).toHaveAttribute("max", "0.99");
  await threshold.fill("0.7");
  await threshold.locator("xpath=ancestor::label").locator(".field-reset").click();
  await expect(threshold).toHaveValue("0.85");
  await expect(page.locator('[data-p="similarity.blink.min_eye_distance_px"]')).toHaveAttribute("step", "1");
});

test("眨眼作为问题显示在非推荐照片的信息行并隐藏文件信息", async ({ page }) => {
  await openApp(page, { blinkSimilar: true });
  await openProject(page);
  await page.locator('[data-nav="similar"]').click();
  await page.locator('[data-similar-group="similar-1"]').click();

  const detail = page.locator("#similarDetailGallery");
  await expect(detail.locator('[data-photo-id="1"]')).not.toContainText("眨眼");
  const candidate = detail.locator('[data-photo-id="2"]');
  await expect(candidate.locator("small")).toHaveText("眨眼");
  await expect(candidate.locator("small")).not.toContainText("4000×3000");
  await expect(candidate.locator("small")).not.toContainText("MB");
  await expect(candidate.locator(".similarity-score")).toHaveText("相似度 91%");
  await expect(candidate.locator(".similarity-score")).not.toContainText("眨眼");
  expect(await page.evaluate(() => cardDetailText({
    reason: "严重失焦、对比度极低",
    _blinkLabel: "眨眼",
    width: 4000,
    height: 3000,
    size: 2_500_000,
  }))).toBe("严重失焦、对比度极低、眨眼");
  await candidate.locator(".thumb").click();
  await expect(page.locator("#viewerMeta")).toContainText("眨眼");
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
  await page.locator('[data-setting="storage"]').click();
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

  const viewMenu = page.locator('[data-filter-menu="view"]');
  await viewMenu.locator(".gallery-tool-trigger").click();
  await viewMenu.locator("#formatFilter .gallery-view-option").hover();
  await expect(page).toHaveScreenshot("library-view-menu-night.png", {
    animations: "disabled",
  });
  await viewMenu.locator(".gallery-tool-trigger").click();

  const sortMenu = page.locator('[data-filter-menu="sort"]');
  await sortMenu.locator(".gallery-tool-trigger").click();
  await expect(page).toHaveScreenshot("library-sort-menu-night.png", {
    animations: "disabled",
  });

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
