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
  return {
    id: "project-1",
    root: "C:\\照片\\夏日旅行",
    cache_root: "C:\\Cullumi缓存",
    profile_id: "custom-portrait",
    last_opened: "2026-08-20T09:30:00",
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

async function installApi(page, options = {}) {
  let decision = "";
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
        return photoPayload(decisions.get(id) || "", id);
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
    if (url.pathname === "/api/settings") {
      return fulfill({ saved: true, settings: { theme: body.theme || "day" } });
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
  await expect(page.locator("#recentList .recent-meta")).toContainText("2 张");
  await expect(page.locator("#recentList .recent-thumb img")).toHaveCount(1);

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
  await page.locator("#viewerNext").click();
  await expect(page.locator("#viewerName")).toHaveText("海边-2.jpg");
  await page.locator("#viewerPrev").click();
  await expect(page.locator("#viewerName")).toHaveText("海边-1.jpg");
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
