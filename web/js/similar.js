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
  similarTools?.sync();
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
function updateSimilarGroupSentinel() {
  const sentinel = $("#similarGroupSentinel");
  sentinel.textContent = state.similar.done
    ? ""
    : state.similar.loading
      ? "正在加载更多相似组…"
      : "继续向下滚动加载";
  sentinel.classList.toggle("hidden", state.similar.done);
}
async function loadSimilarView(reset = false) {
  if (!state.project || state.view !== "similar") return;
  if (reset) {
    state.similar.offset = 0;
    state.similar.total = 0;
    state.similar.done = false;
    state.similar.loading = false;
    state.similar.generation += 1;
    state.similar.groups = [];
    renderSimilarFolders();
  }
  if (state.similar.loading || state.similar.done) return;
  const generation = state.similar.generation,
    params = new URLSearchParams({
      project_id: state.project.id,
      search: state.similar.listSearch,
      limit: String(SIMILAR_GROUP_PAGE_SIZE),
      offset: String(state.similar.offset),
    });
  state.similar.loading = true;
  updateSimilarGroupSentinel();
  try {
    const list = await json(`/api/similar-groups?${params.toString()}`);
    if (generation !== state.similar.generation || state.view !== "similar")
      return;
    const known = new Set(state.similar.groups.map((group) => group.id));
    state.similar.groups.push(
      ...list.items.filter((group) => !known.has(group.id)),
    );
    state.similar.offset += list.items.length;
    state.similar.total = list.total;
    state.similar.done =
      state.similar.offset >= list.total || !list.items.length;
    renderSimilarFolders();
    applySimilarMode();
    if (state.similar.selectedId && reset) {
      try {
        await loadSimilarGroupMembers();
      } catch (error) {
        closeSimilarDetail(false);
        toast("原相似组已发生变化，已返回相似组列表");
      }
    } else if (!state.similar.selectedId) {
      state.items = [];
      $("#viewSubtitle").textContent =
        `${list.total} 组相似照片${state.similar.groups.some((group) => group.face_safe) ? " · 人物照片请检查表情" : ""}`;
      $("#empty").classList.toggle("hidden", !!list.total);
    }
  } finally {
    if (generation === state.similar.generation) {
      state.similar.loading = false;
      updateSimilarGroupSentinel();
    }
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
    `${state.similar.total} 组相似照片${state.similar.groups.some((group) => group.face_safe) ? " · 人物照片请检查表情" : ""}`;
  $("#empty").classList.toggle("hidden", !!state.similar.total);
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
  const similarObserver = new IntersectionObserver(
    (entries) => {
      if (
        entries.some((entry) => entry.isIntersecting) &&
        state.view === "similar"
      )
        loadSimilarView(false).catch((error) => toast(error.message));
    },
    { root: $("#similarFolderPane"), rootMargin: "400px 0px" },
  );
  similarObserver.observe($("#similarGroupSentinel"));
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
