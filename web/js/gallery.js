const setEquals=(set,values)=>set.size===values.length&&values.every(value=>set.has(value));
function libraryPresetName(){
  if(setEquals(state.filters.decisions,DECISION_VALUES)&&setEquals(state.filters.ai,AI_VALUES))return "library";
  if(setEquals(state.filters.decisions,["undecided"])&&setEquals(state.filters.ai,["remove","review"]))return "ai";
  if(setEquals(state.filters.decisions,["undecided"])&&setEquals(state.filters.ai,AI_VALUES))return "undecided";
  if(setEquals(state.filters.decisions,["keep"])&&setEquals(state.filters.ai,AI_VALUES))return "keep";
  if(setEquals(state.filters.decisions,["remove"])&&setEquals(state.filters.ai,AI_VALUES))return "remove";
  return "library";
}
function setActiveNav(name){state.activeNav=name;$$("[data-nav]").forEach(button=>button.classList.toggle("active",button.dataset.nav===name))}
function filterSummary(values,allValues,labels){
  if(values.size===allValues.length)return "全部";
  if(!values.size)return "未选择";
  if(values.size===1)return labels[[...values][0]];
  return `已选 ${values.size} 项`;
}
function syncFilterControls(){
  $$("[data-filter-group]").forEach(input=>input.checked=state.filters[input.dataset.filterGroup].has(input.value));
  const decision=$("#decisionFilterSummary"),ai=$("#aiFilterSummary");
  decision.textContent=filterSummary(state.filters.decisions,DECISION_VALUES,{undecided:"未决定",keep:"已保留",remove:"已移除"});
  ai.textContent=filterSummary(state.filters.ai,AI_VALUES,{remove:"建议移除",review:"人工复查",no_suggestion:"无建议"});
  decision.closest(".multi-filter-trigger").classList.toggle("empty-selection",!state.filters.decisions.size);
  ai.closest(".multi-filter-trigger").classList.toggle("empty-selection",!state.filters.ai.size);
}
function applyLibraryPreset(name){
  const presets={
    library:[DECISION_VALUES,AI_VALUES],
    ai:[["undecided"],["remove","review"]],
    undecided:[["undecided"],AI_VALUES],
    keep:[["keep"],AI_VALUES],
    remove:[["remove"],AI_VALUES],
  };
  const preset=presets[name];if(!preset)return;
  if(state.view==="similar")closeSimilarDetail(false);
  state.view="library";state.filters={decisions:new Set(preset[0]),ai:new Set(preset[1])};
  $("#searchInput").value="";$("#searchInput").placeholder="搜索文件名或路径";closeFilterMenus();syncFilterControls();setActiveNav(name);loadView();
}
function filterQueryValue(values,allValues){if(!values.size)return "none";if(values.size===allValues.length)return "all";return allValues.filter(value=>values.has(value)).join(",")}
function renderLibraryEmpty(message){
  state.items=[];state.library={...state.library,offset:0,total:0,done:true,loading:false};
  $("#gallery").innerHTML="";$("#librarySentinel").classList.add("hidden");$("#viewSubtitle").textContent="显示 0 / 0";
  $("#emptyTitle").textContent="当前筛选没有结果";$("#emptyText").textContent=message;$("#empty").classList.remove("hidden");
}
async function loadLibraryPage(reset=false){
  if(!state.project||state.view!=="library")return;
  if(reset){
    state.library={offset:0,total:0,done:false,loading:false,generation:state.library.generation+1};
    state.items=[];$("#gallery").innerHTML="";$("#empty").classList.add("hidden");
  }
  if(!state.filters.decisions.size){renderLibraryEmpty("当前没有选择决定状态");return}
  if(!state.filters.ai.size){renderLibraryEmpty("当前没有选择筛选状态");return}
  if(state.library.loading||state.library.done)return;
  const generation=state.library.generation;state.library.loading=true;
  const sentinel=$("#librarySentinel");sentinel.textContent="正在加载更多照片…";sentinel.classList.remove("hidden");
  const params=new URLSearchParams({
    project_id:state.project.id,file:"readable",
    decisions:filterQueryValue(state.filters.decisions,DECISION_VALUES),
    ai_states:filterQueryValue(state.filters.ai,AI_VALUES),
    search:$("#searchInput").value.trim(),limit:String(LIBRARY_PAGE_SIZE),offset:String(state.library.offset),
  });
  try{
    const data=await json(`/api/photos?${params.toString()}`);
    if(generation!==state.library.generation||state.view!=="library")return;
    const start=state.items.length;state.items.push(...data.items);state.library.offset+=data.items.length;state.library.total=data.total;
    state.library.done=state.library.offset>=data.total||!data.items.length;
    $("#gallery").insertAdjacentHTML("beforeend",data.items.map((photo,index)=>photoCard(photo,start+index)).join(""));
    $("#viewSubtitle").textContent=`显示 ${state.items.length} / ${data.total}`;$("#empty").classList.toggle("hidden",!!state.items.length);
    if(!state.items.length){$("#emptyTitle").textContent="这里还没有内容";$("#emptyText").textContent="没有照片符合当前组合筛选"}
    sentinel.textContent=state.library.done?(data.total?"已加载全部照片":""):"继续向下滚动加载";
    sentinel.classList.toggle("hidden",state.library.done&&!data.total);
  }catch(error){if(generation===state.library.generation)toast(error.message)}
  finally{if(generation===state.library.generation)state.library.loading=false}
}
async function loadView(){
  if(!state.project)return;const search=encodeURIComponent($("#searchInput").value.trim());
  document.body.classList.toggle("similar-view-open",state.view==="similar");
  applySimilarMode();
  const libraryTitle={library:"照片库",ai:"智能建议",undecided:"待决定",keep:"已保留",remove:"已移除"}[state.activeNav]||"照片库";
  $("#viewTitle").textContent=state.view==="library"?libraryTitle:{similar:"相似连拍",unreadable:"无法读取",quarantine:"隔离历史"}[state.view];
  $("#libraryFilters").classList.toggle("hidden",state.view!=="library");
  $("#aiBatchAction").classList.toggle("hidden",state.view!=="library"||state.activeNav!=="ai");
  $("#gallery").classList.toggle("hidden",state.view==="similar");
  $("#similarBrowser").classList.toggle("hidden",state.view!=="similar");
  $("#librarySentinel").classList.toggle("hidden",state.view!=="library");
  try{
    if(state.view==="library"){await loadLibraryPage(true)}
    else if(state.view==="similar"){await loadSimilarView()}
    else if(state.view==="quarantine"){const d=await json(`/api/quarantine/batches?project_id=${state.project.id}`);renderBatches(d.items)}
    else{const d=await json(`/api/photos?project_id=${state.project.id}&file=unreadable&decisions=all&ai_states=all&search=${search}&limit=500`);state.items=d.items;renderPhotos(d.items,d.total)}
  }catch(e){toast(e.message)}
}
const LIVE_PHOTO_ICON=`<svg class="live-photo-icon" viewBox="0 0 24 24" aria-hidden="true"><circle class="live-photo-ring" cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="1.2"/><g class="live-photo-dots"><circle class="live-photo-dot" cx="12" cy="4.2" r=".62"/><circle class="live-photo-dot" cx="14.99" cy="4.81" r=".62"/><circle class="live-photo-dot" cx="17.52" cy="6.48" r=".62"/><circle class="live-photo-dot" cx="19.19" cy="9.01" r=".62"/><circle class="live-photo-dot" cx="19.8" cy="12" r=".62"/><circle class="live-photo-dot" cx="19.19" cy="14.99" r=".62"/><circle class="live-photo-dot" cx="17.52" cy="17.52" r=".62"/><circle class="live-photo-dot" cx="14.99" cy="19.19" r=".62"/><circle class="live-photo-dot" cx="12" cy="19.8" r=".62"/><circle class="live-photo-dot" cx="9.01" cy="19.19" r=".62"/><circle class="live-photo-dot" cx="6.48" cy="17.52" r=".62"/><circle class="live-photo-dot" cx="4.81" cy="14.99" r=".62"/><circle class="live-photo-dot" cx="4.2" cy="12" r=".62"/><circle class="live-photo-dot" cx="4.81" cy="9.01" r=".62"/><circle class="live-photo-dot" cx="6.48" cy="6.48" r=".62"/><circle class="live-photo-dot" cx="9.01" cy="4.81" r=".62"/></g></svg>`;
function cardDetailText(p){
  const problems=[p.reason,p._blinkLabel].filter(Boolean);
  return problems.length?problems.join("、"):`${p.width||0}×${p.height||0} · ${formatSize(p.size||0)}`;
}
function photoCard(p,index,customBadge="",customKind="",extraInfo=""){
  const badge=customBadge||(p.suggestion==="remove"?"建议移除":p.suggestion==="review"?"人工复查":p.suggestion==="unreadable"?"无法读取":"");
  const badgeKind=customKind||(p.suggestion==="remove"?"remove":p.suggestion==="review"?"review":p.suggestion==="unreadable"?"unreadable":"");
  const decisionClass=p.decision==="keep"?"decision-keep":p.decision==="remove"?"decision-remove":"";
  const badgeAttribute=customBadge?"data-context-badge":"data-analysis-badge";
  return `<article class="photo-card ${decisionClass}" data-photo-id="${p.id}"><div class="thumb" data-open-id="${p.id}"><img loading="lazy" src="${p.thumb_url}" alt="">${p.media_type==="motion_photo"?`<span class="live-mark card-live-mark" aria-label="动态照片">${LIVE_PHOTO_ICON}</span>`:""}${badge?`<span class="badge badge-${badgeKind}" ${badgeAttribute}>${esc(badge)}</span>`:""}</div><div class="card-info"><b title="${esc(p.relative_path)}">${esc(p.relative_path.split("/").pop())}</b><small>${esc(cardDetailText(p))}</small>${extraInfo?`<span class="similarity-score">${esc(extraInfo)}</span>`:""}</div><div class="card-actions"><button class="keep" data-decision="keep" data-id="${p.id}">保留</button><button class="danger" data-decision="remove" data-id="${p.id}">移除</button></div></article>`;
}
function renderPhotos(items,total){$("#viewSubtitle").textContent=`显示 ${items.length} / ${total}`;$("#gallery").innerHTML=items.map((photo,index)=>photoCard(photo,index)).join("");const empty=!items.length;$("#empty").classList.toggle("hidden",!empty);if(empty&&state.lastScan?.total===0){$("#emptyTitle").textContent="没有发现支持的照片";const p=state.lastScan;const ext=Object.entries(p.unsupported_extensions||{}).sort((a,b)=>b[1]-a[1]).map(([x,n])=>`${x} ${n} 个`).join("、");$("#emptyText").textContent=p.video_count?`此文件夹有 ${p.video_count} 个视频，但Cullumi暂不支持视频。${ext?" 文件统计："+ext:""}`:`发现 ${p.unsupported_count||0} 个不支持的文件。${ext}`;}else if(empty){$("#emptyTitle").textContent="这里还没有内容";$("#emptyText").textContent="扫描完成后会显示结果。"}}
function handleGalleryClick(event){
  const decision=event.target.closest("[data-decision]");
  if(decision){event.stopPropagation();setDecision(+decision.dataset.id,decision.dataset.decision,false);return}
  const restoreButton=event.target.closest("[data-restore]");
  if(restoreButton){restore(restoreButton.dataset.restore);return}
  const opener=event.target.closest("[data-open-id]");
  if(opener){const index=state.items.findIndex(photo=>photo.id===+opener.dataset.openId);if(index>=0)openViewer(index)}
}
function renderBatches(items){
  $("#viewSubtitle").textContent=`${items.length} 个批次`;$("#gallery").innerHTML=items.map(x=>`<article class="photo-card"><div class="card-info"><b>${esc(x.created_at)}</b><small>${x.count} 张 · ${formatSize(x.total_size)}${x.restored_at?" · 已恢复":""}</small></div>${x.restored_at?"":`<div class="card-actions"><button data-restore="${x.id}">恢复此批次</button></div>`}</article>`).join("");$("#empty").classList.toggle("hidden",!!items.length)
}
function photoMatchesLibrary(photo){
  const decision=photo.decision||"undecided";
  const ai=photo.suggestion==="keep"?"no_suggestion":photo.suggestion;
  return state.filters.decisions.has(decision)&&state.filters.ai.has(ai);
}
function updateCardDecision(id,decision){
  $$(`[data-photo-id="${id}"]`).forEach(card=>{
    card.classList.toggle("decision-keep",decision==="keep");card.classList.toggle("decision-remove",decision==="remove");
  });
}
function reconcileLibraryDecision(id){
  const photo=state.items.find(item=>item.id===id);if(!photo)return;
  if(photoMatchesLibrary(photo)){updateCardDecision(id,photo.decision);return}
  const card=$(`[data-photo-id="${id}"]`);if(card)card.remove();
  state.items=state.items.filter(item=>item.id!==id);state.library.offset=Math.max(0,state.library.offset-1);
  state.library.total=Math.max(0,state.library.total-1);$("#viewSubtitle").textContent=`显示 ${state.items.length} / ${state.library.total}`;
  if(!state.items.length&&state.library.done){$("#emptyTitle").textContent="这里还没有内容";$("#emptyText").textContent="没有照片符合当前组合筛选。";$("#empty").classList.remove("hidden")}
}
async function syncViewerDecisions(){
  if(!state.viewerNeedsRefresh||!state.project)return;
  const ids=[...state.viewerDirtyIds];state.viewerNeedsRefresh=false;state.viewerDirtyIds.clear();
  if(state.view==="library"){ids.forEach(reconcileLibraryDecision);return}
  await loadView();
}
async function setDecision(id,decision,fromViewer=true){
  let result;
  try{result=await json("/api/decision",{project_id:state.project.id,photo_id:id,decision})}
  catch(error){toast(`保存决定失败：${error.message}`);return false}
  const savedDecision=result.decision??decision,p=state.items.find(x=>x.id===id);if(p)p.decision=savedDecision;
  applyProjectCounts(result.project_counts);
  if(fromViewer){state.viewerNeedsRefresh=true;state.viewerDirtyIds.add(id)}
  updateCardDecision(id,savedDecision);toast(savedDecision==="keep"?"已标记保留":"已标记移除");
  if(fromViewer&&!$("#viewer").open){await syncViewerDecisions();return}
  if(fromViewer&&state.settings.auto_advance)moveViewer(1);
  else if(fromViewer&&p)updateViewerDecision(p);
  else if(!fromViewer){if(state.view==="library")reconcileLibraryDecision(id);else updateCardDecision(id,savedDecision)}
  return true;
}
async function quarantine(){
  const d=await json(`/api/quarantine/preview?project_id=${state.project.id}`);if(!d.count){toast("没有已标记移除的照片");return}
  $("#confirmTitle").textContent=`确认隔离 ${d.count} 张照片？`;$("#confirmBody").innerHTML=`<p>总大小 ${formatSize(d.total_size)}。照片将移入项目内的可恢复隔离区，不会直接删除。</p><div class="confirm-list scroll-fade-region">${d.items.map(x=>esc(x.relative_path)).join("<br>")}</div>`;
  $("#confirmOk").textContent="确认隔离";
  $("#confirmOk").onclick=async()=>{$("#confirm").close();const r=await json("/api/quarantine/apply",{project_id:state.project.id});toast(`已隔离 ${r.moved} 张，跳过 ${r.skipped} 张`);await refreshProject();loadView()};$("#confirm").showModal()
}
async function restore(id){const r=await json("/api/quarantine/restore",{project_id:state.project.id,batch_id:id});toast(`恢复 ${r.restored} 张，文件名冲突 ${r.conflicts} 张`);await refreshProject();loadView()}

function selectNavigationView(event){
  const button=event.target.closest("[data-nav]");
  if(!button)return;
  if(button.dataset.preset){
    applyLibraryPreset(button.dataset.preset);
    return;
  }
  const next=button.dataset.view;
  if(!next)return;
  if(state.view==="similar"&&next!=="similar")closeSimilarDetail(false);
  state.view=next;
  closeFilterMenus();
  setActiveNav(button.dataset.nav);
  $("#searchInput").value="";
  if(next==="similar"){
    $("#searchInput").value=state.similar.listSearch;
    $("#searchInput").placeholder="搜索相似组中的照片";
  }else{
    $("#searchInput").placeholder="搜索文件名或路径";
  }
  loadView();
}

function bindLibraryFilterEvents(){
  $$(".multi-filter-trigger").forEach(button=>button.onclick=event=>{
    event.stopPropagation();
    const owner=button.closest(".multi-filter");
    const panel=owner.querySelector(".multi-filter-panel");
    const opening=panel.classList.contains("hidden");
    closeFilterMenus();
    if(opening){
      panel.classList.remove("hidden");
      button.setAttribute("aria-expanded","true");
    }
  });
  $$(".multi-filter-panel").forEach(panel=>panel.onclick=event=>event.stopPropagation());
  $$('[data-filter-group]').forEach(input=>input.onchange=()=>{
    const values=state.filters[input.dataset.filterGroup];
    input.checked?values.add(input.value):values.delete(input.value);
    syncFilterControls();
    setActiveNav(libraryPresetName());
    loadView();
  });
  $$('[data-select-all]').forEach(button=>button.onclick=()=>{
    const group=button.dataset.selectAll;
    const all=group==="decisions"?DECISION_VALUES:AI_VALUES;
    state.filters[group]=new Set(all);
    syncFilterControls();
    setActiveNav(libraryPresetName());
    loadView();
  });
}

function bindGalleryEvents(){
  [$("#gallery"),$("#similarDetailGallery")].forEach(container=>container.addEventListener("click",handleGalleryClick));
  $("#nav").onclick=selectNavigationView;
  bindLibraryFilterEvents();
  let searchTimer;
  $("#searchInput").oninput=()=>{
    if(state.view==="similar"){
      if(state.similar.selectedId)state.similar.memberSearch=$("#searchInput").value.trim();
      else state.similar.listSearch=$("#searchInput").value.trim();
    }
    clearTimeout(searchTimer);
    searchTimer=setTimeout(loadView,250);
  };
  $("#profileSelect").onchange=event=>applyProfile(event.target.value);
  $("#exportBtn").onclick=async()=>{
    try{
      const result=await json("/api/export/save",{project_id:state.project.id});
      if(result.saved)toast(`CSV 已保存到：${result.path}`);
    }catch(error){
      toast(`导出失败：${error.message}`);
    }
  };
  $("#importBtn").onclick=async()=>{
    const file=await json("/api/choose-csv",{});
    if(!file.path)return;
    const result=await json("/api/import",{project_id:state.project.id,path:file.path});
    toast(`导入 ${result.imported} 条，缺失 ${result.missing} 条`);
    await refreshProject();
    loadView();
  };
  $("#quarantineBtn").onclick=quarantine;
  $("#clearDecisionsBtn").onclick=confirmClearDecisions;
  $("#markAiRemoveBtn").onclick=confirmAiRemoveSuggestions;
  bindViewerEvents();
  const libraryObserver=new IntersectionObserver(entries=>{
    if(entries.some(entry=>entry.isIntersecting))loadLibraryPage(false);
  },{rootMargin:"600px 0px"});
  libraryObserver.observe($("#librarySentinel"));
}
