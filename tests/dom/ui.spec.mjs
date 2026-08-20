import { expect, test } from "playwright/test";


const token = process.env.CULLUMI_DOM_TOKEN || "cullumi-dom-test";
const image = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='32' height='24'%3E%3Crect width='32' height='24' fill='%23d9b7bd'/%3E%3C/svg%3E";
const runtimeProblems = new WeakMap();

const profiles = [
  { id: "conservative", name: "保守筛选", builtin: true },
  { id: "custom-portrait", name: "人像精选", builtin: false, base_mode: "conservative" },
];

function projectPayload(decision = "") {
  return {
    id: "project-1",
    root: "C:\\照片\\夏日旅行",
    cache_root: "C:\\Cullumi缓存",
    profile_id: "custom-portrait",
    total: 2,
    similar_groups: 0,
    pairs: 0,
    counts: { unreadable: 0 },
    decisions: { keep: 0, remove: decision === "remove" ? 1 : 0 },
    library_counts: {
      readable: 2,
      ai_pending: 1,
      ai_remove_pending: 0,
      undecided: decision ? 1 : 2,
      keep: 0,
      remove: decision === "remove" ? 1 : 0,
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

function photoPayload(decision = "") {
  return {
    id: 1,
    relative_path: "旅行/海边.jpg",
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
    if (url.pathname === "/api/project") return fulfill(projectPayload(decision));
    if (url.pathname === "/api/photos") {
      const selected = url.searchParams.get("decisions") || "all";
      const current = decision || "undecided";
      const visible = selected === "all" || selected.split(",").includes(current);
      return fulfill({ total: visible ? 1 : 0, items: visible ? [photoPayload(decision)] : [] });
    }
    if (url.pathname === "/api/decision") {
      decision = body.decision;
      return fulfill({ saved: true });
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
  expect(scripts).toEqual(["runtime.js", "session.js", "similar.js", "settings.js", "app.js"]);

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
