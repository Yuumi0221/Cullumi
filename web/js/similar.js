function similarFolder(group, compact = false) {
  const coverImages = group.covers
    .map(
      (photo, index) =>
        `<img class="folder-cover cover-${index}" loading="lazy" src="${photo.thumb_url}" alt="">`,
    )
    .reverse()
    .join("");
  const name = group.recommended.relative_path.split("/").pop();
  return `<button class="similar-folder ${compact ? "compact" : ""} ${group.id === state.similar.selectedId ? "active" : ""}" data-similar-group="${group.id}"><span class="folder-stack">${coverImages}<i>${group.count} 张</i></span><span class="folder-caption"><b title="${esc(group.recommended.relative_path)}">${esc(name)}</b><small>${group.kind === "exact" ? "完全重复" : `${group.count} 张相似照片`}</small></span></button>`;
}
function renderSimilarFolders() {
  const selected = !!state.similar.selectedId;
  $("#similarFolders").innerHTML = state.similar.groups
    .map((group) => similarFolder(group, selected))
    .join("");
  $("#similarFolders").classList.toggle(
    "compact",
    selected && state.similar.mode === "side",
  );
  $("#similarFolderPane").classList.toggle(
    "hidden",
    selected && state.similar.mode === "expanded",
  );
}
function similarFormatValues() {
  return state.similar.formatCategories.map((item) => item.id);
}
function similarPhotoFormat(photo) {
  return FORMAT_VALUES.includes(photo.format_category)
    ? photo.format_category
    : "other";
}
function similarFormatCategories(members) {
  const counts = new Map();
  members.forEach((photo) => {
    const category = similarPhotoFormat(photo);
    counts.set(category, (counts.get(category) || 0) + 1);
  });
  return FORMAT_VALUES.filter((category) => counts.has(category)).map(
    (category) => ({
      id: category,
      label: FORMAT_LABELS[category],
      count: counts.get(category),
    }),
  );
}
function similarDecisionValue(photo) {
  return photo.decision || "undecided";
}
function similarAiValue(photo) {
  return ["remove", "review"].includes(photo.suggestion)
    ? photo.suggestion
    : "no_suggestion";
}
function similarFilterAllValues(group) {
  if (group === "decisions") return DECISION_VALUES;
  if (group === "ai") return AI_VALUES;
  return similarFormatValues();
}
function similarSuggestionRank(photo) {
  return { remove: 0, review: 1, unreadable: 2 }[photo.suggestion] ?? 3;
}
function compareSimilarPhotos(left, right) {
  const direction = state.similar.sortDirection === "desc" ? -1 : 1;
  let result = 0;
  if (state.similar.sort === "suggestion") {
    result = similarSuggestionRank(left) - similarSuggestionRank(right);
  } else if (state.similar.sort === "filename") {
    const leftName = left.relative_path.split("/").pop() || "",
      rightName = right.relative_path.split("/").pop() || "";
    result = leftName.localeCompare(rightName, undefined, {
      numeric: true,
      sensitivity: "base",
    });
  } else if (state.similar.sort === "size") {
    result = (Number(left.size) || 0) - (Number(right.size) || 0);
  } else if (state.similar.sort === "taken") {
    const leftTaken = String(left.taken || ""),
      rightTaken = String(right.taken || "");
    if (!leftTaken || !rightTaken) {
      if (leftTaken !== rightTaken) return leftTaken ? -1 : 1;
    } else result = leftTaken.localeCompare(rightTaken);
  }
  if (result) return result * direction;
  const pathResult = left.relative_path.localeCompare(
    right.relative_path,
    undefined,
    { numeric: true, sensitivity: "base" },
  );
  return pathResult || left.id - right.id;
}
function syncSimilarControls() {
  const availableValues = similarFormatValues(),
    available = new Set(availableValues),
    decisionSummary = $("#similarDecisionFilterSummary"),
    aiSummary = $("#similarAiFilterSummary"),
    formatSummary = $("#similarFormatFilterSummary");
  $$("[data-similar-format-option]").forEach((label) => {
    const category = label.dataset.similarFormatOption;
    label.classList.toggle("hidden", !available.has(category));
  });
  $$("[data-similar-filter-group]").forEach((input) => {
    input.checked = state.similar[input.dataset.similarFilterGroup].has(
      input.value,
    );
  });
  decisionSummary.textContent = filterSummary(
    state.similar.decisions,
    DECISION_VALUES,
    { undecided: "未决定", keep: "已保留", remove: "已移除" },
  );
  aiSummary.textContent = filterSummary(state.similar.ai, AI_VALUES, {
    remove: "建议移除",
    review: "人工复查",
    no_suggestion: "无建议",
  });
  formatSummary.textContent = filterSummary(
    state.similar.formats,
    availableValues,
    FORMAT_LABELS,
  );
  decisionSummary.closest(".gallery-view-option").classList.toggle(
    "empty-selection",
    !state.similar.decisions.size,
  );
  aiSummary.closest(".gallery-view-option").classList.toggle(
    "empty-selection",
    !state.similar.ai.size,
  );
  formatSummary.closest(".gallery-view-option").classList.toggle(
    "empty-selection",
    !!availableValues.length && !state.similar.formats.size,
  );
  $$("[data-similar-select-all]").forEach((button) => {
    const group = button.dataset.similarSelectAll,
      all = similarFilterAllValues(group);
    button.textContent =
      all.length && setEquals(state.similar[group], all) ? "全不选" : "全选";
  });
  $("#similarFormatViewItem").classList.toggle(
    "hidden",
    !availableValues.length,
  );
  $$("[data-similar-sort-value]").forEach((input) => {
    input.checked = input.dataset.similarSortValue === state.similar.sort;
  });
  $$("[data-similar-sort-direction]").forEach((input) => {
    input.checked =
      input.dataset.similarSortDirection === state.similar.sortDirection;
  });
  const sortLabels = {
      suggestion: "建议",
      filename: "名称",
      size: "大小",
      taken: "日期（拍摄日期）",
    },
    directionLabel = state.similar.sortDirection === "desc" ? "递减" : "递增";
  $("#similarSortTool .gallery-tool-trigger").title =
    `排序：${sortLabels[state.similar.sort]} · ${directionLabel}`;
}
function applySimilarMode() {
  const selected = !!state.similar.selectedId,
    expanded = state.similar.mode === "expanded",
    visible = state.view === "similar" && selected;
  $("#similarBrowser").classList.toggle("detail-open", selected);
  $("#similarBrowser").classList.toggle(
    "detail-expanded",
    selected && expanded,
  );
  $("#similarDetail").classList.toggle("hidden", !selected);
  $("#similarViewActions").classList.toggle("hidden", !visible);
  $("#similarCollapseBtn").classList.toggle("hidden", expanded);
  $("#similarExpandBtn").classList.toggle("hidden", expanded);
  $("#similarBackBtn").classList.toggle("hidden", !expanded);
  $("#similarFolderPane").classList.toggle("hidden", selected && expanded);
  syncSimilarControls();
  document.body.classList.toggle(
    "similar-detail-open",
    state.view === "similar" && selected,
  );
  document.body.classList.toggle(
    "similar-side-open",
    state.view === "similar" && selected && !expanded,
  );
}
function blinkStatusLabel(photo, recommended, kind) {
  if (
    recommended ||
    kind !== "similar" ||
    state.settings.blink_detection_enabled === false
  )
    return "";
  if (
    photo.blink_status !== "closed" ||
    (photo.blink_closed_face_count || 0) < 1
  )
    return "";
  const faceCount = Math.max(0, Number(photo.blink_face_count) || 0);
  const uncertainCount = Math.max(
    0,
    Number(photo.blink_uncertain_face_count) || 0,
  );
  if (!faceCount) return "";
  const profile = state.profiles.find(
    (item) => item.id === state.project?.profile_id,
  );
  const minimum = Number(
    profile?.similarity?.blink?.reliable_coverage_min ?? 0.8,
  );
  return (faceCount - uncertainCount) / faceCount >= minimum ? "眨眼" : "";
}
async function loadSimilarView() {
  const listSearch = encodeURIComponent(state.similar.listSearch);
  const list = await json(
    `/api/similar-groups?project_id=${state.project.id}&search=${listSearch}`,
  );
  state.similar.groups = list.items;
  if (
    state.similar.selectedId &&
    !list.items.some((group) => group.id === state.similar.selectedId)
  ) {
    closeSimilarDetail(false);
    toast("原相似组已发生变化，已返回相似组列表");
  }
  renderSimilarFolders();
  applySimilarMode();
  if (state.similar.selectedId) await loadSimilarGroupMembers();
  else {
    state.items = [];
    $("#viewSubtitle").textContent =
      `${list.total} 组相似照片${list.items.some((group) => group.face_safe) ? " · 人物照片请检查表情" : ""}`;
    $("#empty").classList.toggle("hidden", !!list.items.length);
  }
}
async function loadSimilarGroupMembers() {
  const groupId = state.similar.selectedId;
  const detail = await json(
    `/api/similar-group?project_id=${state.project.id}&group_id=${encodeURIComponent(groupId)}`,
  );
  if (groupId !== state.similar.selectedId) return;
  const decorated = detail.members.map((photo) => {
    const recommended =
      (photo.similarity_source_id || photo.id) === detail.recommended_id;
    const blinkLabel = blinkStatusLabel(photo, recommended, detail.kind);
    return {
      ...photo,
      _viewerBadge: recommended ? "推荐保留" : "可考虑移除",
      _viewerKind: recommended ? "recommended" : "candidate-remove",
      _blinkLabel: blinkLabel,
    };
  });
  const previousValues = similarFormatValues(),
    selectedAll =
      !!previousValues.length && setEquals(state.similar.formats, previousValues);
  state.similar.detail = { ...detail, members: decorated };
  state.similar.formatCategories = similarFormatCategories(decorated);
  const availableValues = similarFormatValues();
  state.similar.formats =
    !previousValues.length || selectedAll
      ? new Set(availableValues)
      : new Set(
          [...state.similar.formats].filter((value) =>
            availableValues.includes(value),
          ),
        );
  renderSimilarGroupMembers();
  renderSimilarFolders();
  applySimilarMode();
}
function renderSimilarGroupMembers() {
  const detail = state.similar.detail;
  if (!detail || detail.id !== state.similar.selectedId) return;
  const query = state.similar.memberSearch.trim().toLocaleLowerCase(),
    decorated = detail.members
      .filter(
        (photo) =>
          state.similar.decisions.has(similarDecisionValue(photo)) &&
          state.similar.ai.has(similarAiValue(photo)) &&
          state.similar.formats.has(similarPhotoFormat(photo)) &&
          (!query || photo.relative_path.toLocaleLowerCase().includes(query)),
      )
      .sort(compareSimilarPhotos),
    allDecisionsSelected = setEquals(
      state.similar.decisions,
      DECISION_VALUES,
    ),
    allAiSelected = setEquals(state.similar.ai, AI_VALUES),
    allFormatsSelected = setEquals(
      state.similar.formats,
      similarFormatValues(),
    ),
    filtered =
      !allDecisionsSelected || !allAiSelected || !allFormatsSelected;
  state.items = decorated;
  $("#viewSubtitle").textContent =
    `当前组 ${detail.count} 张${detail.face_safe ? " · 人物照片请检查表情" : ""}${query || filtered ? ` · 显示 ${decorated.length} 张` : ""}`;
  $("#similarDetailGallery").innerHTML = decorated
    .map((photo, index) => {
      const recommended =
        (photo.similarity_source_id || photo.id) === detail.recommended_id;
      const extra = recommended
        ? ""
        : detail.kind === "exact"
          ? "完全重复"
          : `相似度 ${Math.round((photo.group_similarity || 0) * 100)}%`;
      return photoCard(
        photo,
        index,
        recommended ? "推荐保留" : "可考虑移除",
        recommended ? "recommended" : "candidate-remove",
        extra,
      );
    })
    .join("");
  $("#empty").classList.toggle("hidden", !!decorated.length);
  if (!decorated.length) {
    $("#emptyTitle").textContent = "当前筛选没有结果";
    $("#emptyText").textContent = "没有照片符合当前组的搜索和查看条件";
  }
  syncSimilarControls();
}
async function openSimilarGroup(groupId) {
  state.similar.selectedId = groupId;
  state.similar.memberSearch = "";
  state.similar.detail = null;
  state.similar.formatCategories = [];
  state.similar.decisions = new Set(DECISION_VALUES);
  state.similar.ai = new Set(AI_VALUES);
  state.similar.formats = new Set();
  state.similar.sort = "suggestion";
  state.similar.sortDirection = "asc";
  state.similar.mode = window.innerWidth <= 850 ? "expanded" : "side";
  $("#searchInput").value = "";
  $("#searchInput").placeholder = "搜索当前组照片";
  renderSimilarFolders();
  applySimilarMode();
  try {
    await loadSimilarGroupMembers();
  } catch (e) {
    closeSimilarDetail();
    toast(e.message);
  }
}
function closeSimilarDetail(restoreSearch = true) {
  state.similar.selectedId = "";
  state.similar.mode = "closed";
  state.similar.memberSearch = "";
  state.similar.detail = null;
  state.similar.formatCategories = [];
  state.similar.decisions = new Set(DECISION_VALUES);
  state.similar.ai = new Set(AI_VALUES);
  state.similar.formats = new Set();
  state.similar.sort = "suggestion";
  state.similar.sortDirection = "asc";
  state.items = [];
  if (restoreSearch) {
    $("#searchInput").value = state.similar.listSearch;
    $("#searchInput").placeholder = "搜索相似组中的照片";
  }
  $("#similarDetailGallery").innerHTML = "";
  renderSimilarFolders();
  applySimilarMode();
  $("#viewSubtitle").textContent =
    `${state.similar.groups.length} 组相似照片${state.similar.groups.some((group) => group.face_safe) ? " · 人物照片请检查表情" : ""}`;
  $("#empty").classList.toggle("hidden", !!state.similar.groups.length);
}
function expandSimilarDetail() {
  if (!state.similar.selectedId) return;
  state.similar.mode = "expanded";
  renderSimilarFolders();
  applySimilarMode();
}

function bindSimilarEvents() {
  $("#similarCollapseBtn").onclick = () => closeSimilarDetail();
  $("#similarBackBtn").onclick = () => closeSimilarDetail();
  $("#similarExpandBtn").onclick = expandSimilarDetail;
  $$("[data-similar-filter-group]").forEach((input) => {
    input.onchange = () => {
      const values = state.similar[input.dataset.similarFilterGroup];
      input.checked
        ? values.add(input.value)
        : values.delete(input.value);
      renderSimilarGroupMembers();
    };
  });
  $$("[data-similar-select-all]").forEach((button) => {
    button.onclick = () => {
      const group = button.dataset.similarSelectAll,
        all = similarFilterAllValues(group);
      state.similar[group] =
        all.length && setEquals(state.similar[group], all)
          ? new Set()
          : new Set(all);
      renderSimilarGroupMembers();
    };
  });
  $$("[data-similar-sort-value]").forEach((input) => {
    input.onchange = () => {
      if (
        input.checked &&
        LIBRARY_SORT_VALUES.includes(input.dataset.similarSortValue)
      ) {
        state.similar.sort = input.dataset.similarSortValue;
        renderSimilarGroupMembers();
      } else syncSimilarControls();
    };
  });
  $$("[data-similar-sort-direction]").forEach((input) => {
    input.onchange = () => {
      if (
        input.checked &&
        ["asc", "desc"].includes(input.dataset.similarSortDirection)
      ) {
        state.similar.sortDirection = input.dataset.similarSortDirection;
        renderSimilarGroupMembers();
      } else syncSimilarControls();
    };
  });
  $("#similarFolderPane").onclick = (event) => {
    if (
      state.similar.mode === "side" &&
      !event.target.closest("[data-similar-group]")
    )
      closeSimilarDetail();
  };
  $("#similarFolders").onclick = (event) => {
    const button = event.target.closest("[data-similar-group]");
    if (!button) return;
    event.stopPropagation();
    openSimilarGroup(button.dataset.similarGroup);
  };
  window.addEventListener("resize", () => {
    if (
      state.view === "similar" &&
      state.similar.selectedId &&
      window.innerWidth <= 850 &&
      state.similar.mode === "side"
    )
      expandSimilarDetail();
  });
}
