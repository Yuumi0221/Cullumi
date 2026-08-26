const GALLERY_FILTER_DEFINITIONS = [
  {
    key: "decisions",
    title: "决定状态",
    values: [
      ["undecided", "未决定"],
      ["keep", "已保留"],
      ["remove", "已移除"],
    ],
  },
  {
    key: "ai",
    title: "筛选建议",
    values: [
      ["remove", "建议移除"],
      ["review", "人工复查"],
      ["no_suggestion", "无建议"],
    ],
  },
  {
    key: "formats",
    title: "照片格式",
    values: FORMAT_VALUES.map((value) => [value, FORMAT_LABELS[value]]),
  },
];
const GALLERY_SORT_DEFINITIONS = [
  ["suggestion", "建议", "建议"],
  ["filename", "名称", "名称"],
  ["size", "大小", "大小"],
  ["taken", "日期", "日期（拍摄日期）"],
];
const setEquals = (set, values) =>
  set.size === values.length && values.every((value) => set.has(value));

function filterSummary(values, available) {
  if (values.size === available.length) return "全部";
  if (!values.size) return "未选择";
  if (values.size === 1) {
    const selected = available.find((item) => item.id === [...values][0]);
    return selected?.label || [...values][0];
  }
  return `已选 ${values.size} 项`;
}

function galleryToolMarkup(options) {
  const filterItems = GALLERY_FILTER_DEFINITIONS.map((group) => {
    const ids = options.ids[group.key],
      filterAttribute =
        options.prefix === "similar"
          ? `data-similar-filter-group="${group.key}"`
          : `data-filter-group="${group.key}"`,
      formatAttribute = (value) =>
        group.key !== "formats"
          ? ""
          : options.prefix === "similar"
            ? `data-similar-format-option="${value}"`
            : `data-format-option="${value}"`;
    const labels = group.values
      .map(
        ([value, label]) =>
          `<label data-gallery-value="${value}" ${formatAttribute(value)}><input type="checkbox" data-gallery-filter="${group.key}" ${filterAttribute} value="${value}">${label}</label>`,
      )
      .join("");
    const selectAttribute =
      options.prefix === "similar"
        ? `data-similar-select-all="${group.key}"`
        : `data-select-all="${group.key}"`;
    return `<div id="${ids.item}" class="gallery-view-item">
      <button class="gallery-view-option" type="button"><span>${group.title}</span><b id="${ids.summary}">全部</b><svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="${ICONS_URL}#chevron-down"></use></svg></button>
      <div class="gallery-view-submenu">
        <div class="multi-filter-head"><b>${group.title}</b><button type="button" data-gallery-select-all="${group.key}" ${selectAttribute}>全选</button></div>
        ${labels}
      </div>
    </div>`;
  }).join("");
  const sortItems = GALLERY_SORT_DEFINITIONS.map(
    ([value, label]) => {
      const legacy =
        options.prefix === "similar"
          ? `data-similar-sort-value="${value}"`
          : `data-sort-value="${value}"`;
      return `<label><input type="radio" name="${options.prefix}-sort-value" data-gallery-sort-value="${value}" ${legacy}>${label}</label>`;
    },
  ).join("");
  const directionAttribute = (value) =>
    options.prefix === "similar"
      ? `data-similar-sort-direction="${value}"`
      : `data-sort-direction="${value}"`;
  return `<div id="${options.ids.viewTool}" class="multi-filter gallery-tool" data-filter-menu="${options.viewMenu}">
    <button class="multi-filter-trigger gallery-tool-trigger" type="button" aria-expanded="false" aria-controls="${options.ids.viewPanel}">
      <svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="${ICONS_URL}#gallery-filter"></use></svg><span>查看</span><svg class="gallery-tool-chevron" viewBox="0 0 1024 1024" aria-hidden="true"><use href="${ICONS_URL}#chevron-down"></use></svg>
    </button>
    <div id="${options.ids.viewPanel}" class="multi-filter-panel gallery-tool-panel gallery-view-panel hidden">${filterItems}</div>
  </div>
  <div id="${options.ids.sortTool}" class="multi-filter gallery-tool" data-filter-menu="${options.sortMenu}">
    <button class="multi-filter-trigger gallery-tool-trigger" type="button" aria-expanded="false" aria-controls="${options.ids.sortPanel}">
      <svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="${ICONS_URL}#gallery-sort"></use></svg><span>排序</span><svg class="gallery-tool-chevron" viewBox="0 0 1024 1024" aria-hidden="true"><use href="${ICONS_URL}#chevron-down"></use></svg>
    </button>
    <div id="${options.ids.sortPanel}" class="multi-filter-panel gallery-tool-panel gallery-sort-panel hidden">
      ${sortItems}
      <div class="gallery-menu-divider" aria-hidden="true"></div>
      <label><input type="radio" name="${options.prefix}-sort-direction" data-gallery-sort-direction="asc" ${directionAttribute("asc")}>递增</label>
      <label><input type="radio" name="${options.prefix}-sort-direction" data-gallery-sort-direction="desc" ${directionAttribute("desc")}>递减</label>
    </div>
  </div>`;
}

function createGalleryTools(options) {
  const root = $(options.root);
  root.innerHTML = galleryToolMarkup(options);

  function available(group) {
    return options.available(group).map((item) =>
      typeof item === "string"
        ? { id: item, label: item }
        : { id: item.id, label: item.label },
    );
  }

  function sync() {
    GALLERY_FILTER_DEFINITIONS.forEach((group) => {
      const values = options.values(group.key),
        choices = available(group.key),
        choiceIds = choices.map((item) => item.id),
        item = $(`#${options.ids[group.key].item}`),
        summary = $(`#${options.ids[group.key].summary}`);
      item.classList.toggle("hidden", group.key === "formats" && !choices.length);
      item.querySelectorAll("[data-gallery-value]").forEach((label) => {
        const value = label.dataset.galleryValue;
        label.classList.toggle("hidden", !choiceIds.includes(value));
        label.querySelector("input").checked = values.has(value);
      });
      summary.textContent = filterSummary(values, choices);
      summary.closest(".gallery-view-option").classList.toggle(
        "empty-selection",
        !!choices.length && !values.size,
      );
      item.querySelector("[data-gallery-select-all]").textContent =
        choiceIds.length && setEquals(values, choiceIds) ? "全不选" : "全选";
    });
    root.querySelectorAll("[data-gallery-sort-value]").forEach((input) => {
      input.checked = input.dataset.gallerySortValue === options.sort();
    });
    root.querySelectorAll("[data-gallery-sort-direction]").forEach((input) => {
      input.checked =
        input.dataset.gallerySortDirection === options.direction();
    });
    const sortLabel = GALLERY_SORT_DEFINITIONS.find(
      ([value]) => value === options.sort(),
    )?.[2];
    $(`#${options.ids.sortTool} .gallery-tool-trigger`).title =
      `排序：${sortLabel} · ${options.direction() === "desc" ? "递减" : "递增"}`;
  }

  root.addEventListener("click", (event) => {
    const trigger = event.target.closest(".multi-filter-trigger");
    if (trigger) {
      event.stopPropagation();
      const panel = trigger.closest(".multi-filter").querySelector(
        ".multi-filter-panel",
      );
      const opening = panel.classList.contains("hidden");
      closeFilterMenus();
      if (opening) {
        panel.classList.remove("hidden");
        trigger.setAttribute("aria-expanded", "true");
      }
      return;
    }
    if (event.target.closest(".multi-filter-panel")) event.stopPropagation();
    const selectAll = event.target.closest("[data-gallery-select-all]");
    if (!selectAll) return;
    const group = selectAll.dataset.gallerySelectAll,
      all = available(group).map((item) => item.id),
      current = options.values(group);
    options.setValues(
      group,
      all.length && setEquals(current, all) ? new Set() : new Set(all),
    );
    sync();
    options.onFilterChange();
  });
  root.addEventListener("change", (event) => {
    const filter = event.target.closest("[data-gallery-filter]");
    if (filter) {
      const group = filter.dataset.galleryFilter,
        values = new Set(options.values(group));
      filter.checked ? values.add(filter.value) : values.delete(filter.value);
      options.setValues(group, values);
      sync();
      options.onFilterChange();
      return;
    }
    const sort = event.target.closest("[data-gallery-sort-value]");
    if (sort) {
      if (sort.checked && LIBRARY_SORT_VALUES.includes(sort.dataset.gallerySortValue)) {
        options.setSort(sort.dataset.gallerySortValue);
        sync();
        options.onSortChange();
      } else sync();
      return;
    }
    const direction = event.target.closest("[data-gallery-sort-direction]");
    if (direction) {
      if (direction.checked && ["asc", "desc"].includes(direction.dataset.gallerySortDirection)) {
        options.setDirection(direction.dataset.gallerySortDirection);
        sync();
        options.onSortChange();
      } else sync();
    }
  });
  sync();
  return { sync };
}
