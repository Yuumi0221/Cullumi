
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
    sentinel.classList.toggle("hidden",state.library.done&&!data.total);bindGallery();
  }catch(error){if(generation===state.library.generation)toast(error.message)}
  finally{if(generation===state.library.generation)state.library.loading=false}
}
async function loadView(){
  if(!state.project)return;const search=encodeURIComponent($("#searchInput").value.trim());
  document.body.classList.toggle("similar-view-open",state.view==="similar");
  applySimilarMode();
  const libraryTitle={library:"照片库",ai:"AI 建议",undecided:"待决定",keep:"已保留",remove:"已移除"}[state.activeNav]||"照片库";
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
function photoCard(p,index,customBadge="",customKind="",extraInfo=""){
  const badge=customBadge||(p.suggestion==="remove"?"建议移除":p.suggestion==="review"?"人工复查":p.suggestion==="unreadable"?"无法读取":"");
  const badgeKind=customKind||(p.suggestion==="remove"?"remove":p.suggestion==="review"?"review":p.suggestion==="unreadable"?"unreadable":"");
  const decisionClass=p.decision==="keep"?"decision-keep":p.decision==="remove"?"decision-remove":"";
  return `<article class="photo-card ${decisionClass}" data-photo-id="${p.id}"><div class="thumb" data-open-id="${p.id}"><img loading="lazy" src="${p.thumb_url}" alt="">${badge?`<span class="badge badge-${badgeKind}">${esc(badge)}</span>`:""}</div><div class="card-info"><b title="${esc(p.relative_path)}">${esc(p.relative_path.split("/").pop())}</b><small>${esc(p.reason||`${p.width||0}×${p.height||0} · ${formatSize(p.size||0)}`)}</small>${extraInfo?`<span class="similarity-score">${esc(extraInfo)}</span>`:""}</div><div class="card-actions"><button class="keep" data-decision="keep" data-id="${p.id}">保留</button><button class="danger" data-decision="remove" data-id="${p.id}">移除</button></div></article>`;
}
function renderPhotos(items,total){$("#viewSubtitle").textContent=`显示 ${items.length} / ${total}`;$("#gallery").innerHTML=items.map((photo,index)=>photoCard(photo,index)).join("");const empty=!items.length;$("#empty").classList.toggle("hidden",!empty);if(empty&&state.lastScan?.total===0){$("#emptyTitle").textContent="没有发现支持的照片";const p=state.lastScan;const ext=Object.entries(p.unsupported_extensions||{}).sort((a,b)=>b[1]-a[1]).map(([x,n])=>`${x} ${n} 个`).join("、");$("#emptyText").textContent=p.video_count?`此文件夹有 ${p.video_count} 个视频，但Cullumi暂不支持视频。${ext?" 文件统计："+ext:""}`:`发现 ${p.unsupported_count||0} 个不支持的文件。${ext}`;}else if(empty){$("#emptyTitle").textContent="这里还没有内容";$("#emptyText").textContent="扫描完成后会显示结果。"}bindGallery()}
function bindGallery(){
  $$("[data-open-id]").forEach(x=>x.onclick=()=>{const index=state.items.findIndex(photo=>photo.id===+x.dataset.openId);if(index>=0)openViewer(index)});
  $$("[data-decision]").forEach(x=>x.onclick=e=>{e.stopPropagation();setDecision(+x.dataset.id,x.dataset.decision,false)})
}
function renderBatches(items){
  $("#viewSubtitle").textContent=`${items.length} 个批次`;$("#gallery").innerHTML=items.map(x=>`<article class="photo-card"><div class="card-info"><b>${esc(x.created_at)}</b><small>${x.count} 张 · ${formatSize(x.total_size)}${x.restored_at?" · 已恢复":""}</small></div>${x.restored_at?"":`<div class="card-actions"><button data-restore="${x.id}">恢复此批次</button></div>`}</article>`).join("");$("#empty").classList.toggle("hidden",!!items.length);$$("[data-restore]").forEach(b=>b.onclick=()=>restore(b.dataset.restore))
}
function viewerSuggestion(p){if(p._viewerBadge)return {text:p._viewerBadge,kind:p._viewerKind};if(p.suggestion==="remove")return {text:"建议移除",kind:"remove"};if(p.suggestion==="review")return {text:"人工复查",kind:"review"};return {text:"",kind:""}}
function updateViewerDecision(p){$("#viewerKeep").classList.toggle("active",p.decision==="keep");$("#viewerRemove").classList.toggle("active",p.decision==="remove")}
function applyViewerTransform(){const t=state.viewerTransform,img=$("#viewerImage");img.style.transform=`translate3d(${t.x}px,${t.y}px,0) scale(${t.scale})`;img.classList.toggle("zoomed",t.scale>1);img.classList.toggle("dragging",t.dragging)}
function clampViewerPan(){const t=state.viewerTransform,img=$("#viewerImage"),maxX=Math.max(0,img.offsetWidth*(t.scale-1)/2),maxY=Math.max(0,img.offsetHeight*(t.scale-1)/2);t.x=Math.max(-maxX,Math.min(maxX,t.x));t.y=Math.max(-maxY,Math.min(maxY,t.y))}
function resetViewerTransform(){Object.assign(state.viewerTransform,{scale:1,x:0,y:0,dragging:false,moved:false,suppressClick:false});clearTimeout(state.viewerClickTimer);applyViewerTransform()}
function zoomViewer(factor,clientX,clientY){const t=state.viewerTransform,figure=$("#viewerImage").parentElement,rect=figure.getBoundingClientRect(),old=t.scale,next=Math.max(1,Math.min(8,old*factor));if(next===old)return;const pointX=(clientX??rect.left+rect.width/2)-(rect.left+rect.width/2),pointY=(clientY??rect.top+rect.height/2)-(rect.top+rect.height/2),ratio=next/old;t.x=pointX-(pointX-t.x)*ratio;t.y=pointY-(pointY-t.y)*ratio;t.scale=next;if(next===1){t.x=0;t.y=0}clampViewerPan();applyViewerTransform()}
function openViewer(i){if(!state.items.length)return;state.viewerIndex=(i+state.items.length)%state.items.length;const p=state.items[state.viewerIndex],suggestion=viewerSuggestion(p),badge=$("#viewerBadge");resetViewerTransform();$("#viewerImage").src=p.photo_url;$("#viewerName").textContent=p.relative_path.split("/").pop();$("#viewerMeta").textContent=`${p.width||0} × ${p.height||0} · ${formatSize(p.size||0)}${p.reason?" · "+p.reason:""}`;badge.textContent=suggestion.text;badge.className=`viewer-badge ${suggestion.kind?`badge-${suggestion.kind}`:"hidden"}`;updateViewerDecision(p);$("#viewerIndex").textContent=`${state.viewerIndex+1} / ${state.items.length}`;if(!$("#viewer").open)$("#viewer").showModal()}
const moveViewer=d=>openViewer(state.viewerIndex+d);
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
  await json("/api/decision",{project_id:state.project.id,photo_id:id,decision});
  const p=state.items.find(x=>x.id===id);if(p)p.decision=decision;
  if(fromViewer){state.viewerNeedsRefresh=true;state.viewerDirtyIds.add(id)}
  toast(decision==="keep"?"已标记保留":"已标记移除");await refreshProject();
  if(fromViewer&&!$("#viewer").open){await syncViewerDecisions();return}
  if(fromViewer&&state.settings.auto_advance)moveViewer(1);
  else if(fromViewer&&p)updateViewerDecision(p);
  else if(!fromViewer){if(state.view==="library")reconcileLibraryDecision(id);else updateCardDecision(id,decision)}
}
async function quarantine(){
  const d=await json(`/api/quarantine/preview?project_id=${state.project.id}`);if(!d.count){toast("没有已标记移除的照片");return}
  $("#confirmTitle").textContent=`确认隔离 ${d.count} 张照片？`;$("#confirmBody").innerHTML=`<p>总大小 ${formatSize(d.total_size)}。照片将移入项目内的可恢复隔离区，不会直接删除。</p><div class="confirm-list scroll-fade-region">${d.items.map(x=>esc(x.relative_path)).join("<br>")}</div>`;
  $("#confirmOk").textContent="确认隔离";
  $("#confirmOk").onclick=async()=>{$("#confirm").close();const r=await json("/api/quarantine/apply",{project_id:state.project.id});toast(`已隔离 ${r.moved} 张，跳过 ${r.skipped} 张`);await refreshProject();loadView()};$("#confirm").showModal()
}
async function restore(id){const r=await json("/api/quarantine/restore",{project_id:state.project.id,batch_id:id});toast(`恢复 ${r.restored} 张，文件名冲突 ${r.conflicts} 张`);await refreshProject();loadView()}
$("#chooseBtn").onclick=chooseProject;$("#recentSearch").oninput=event=>{state.recentQuery=event.target.value;renderRecentProjects()};$("#scanBtn").onclick=startScan;$("#cancelBtn").onclick=()=>json("/api/scan/cancel",{project_id:state.project.id});
$("#homeBtn").onclick=()=>{document.body.classList.remove("project-open","similar-view-open","similar-detail-open","similar-side-open");$("#workspace").classList.add("hidden");$("#home").classList.remove("hidden");$("#searchInput").value="";state.poll+=1;boot().catch(e=>toast(e.message))};
$("#settingsBtn").onclick=()=>{$("#settings").showModal();if(state.project)$("#projectCache").value=state.project.cache_root;$("#profileEditorSelect").value=state.project?.profile_id||state.profiles[0]?.id;editorLoad($("#profileEditorSelect").value)};
$("#githubBtn").onclick=async()=>{try{await json("/api/open-github",{})}catch(e){toast(e.message)}};
$("#themeBtn").onclick=()=>applyTheme(state.theme==="night"?"day":"night",true);
$("#projectBox").ondblclick=openCurrentProjectFolder;$("#projectBox").onkeydown=event=>{if(event.key!=="Enter")return;event.preventDefault();openCurrentProjectFolder()};
$("#recentOpenFolder").onclick=openRecentFolder;$("#recentRemove").onclick=confirmRemoveRecent;
$$("[data-close]").forEach(x=>x.onclick=closeDialogs);
$$("dialog[data-backdrop-close]").forEach(dialog=>dialog.addEventListener("click",closeNoticeOnBackdrop));
$("#nav").onclick=e=>{
  const button=e.target.closest("[data-nav]");if(!button)return;
  if(button.dataset.preset){applyLibraryPreset(button.dataset.preset);return}
  const next=button.dataset.view;if(!next)return;
  if(state.view==="similar"&&next!=="similar")closeSimilarDetail(false);
  state.view=next;closeFilterMenus();setActiveNav(button.dataset.nav);$("#searchInput").value="";
  if(next==="similar"){$("#searchInput").value=state.similar.listSearch;$("#searchInput").placeholder="搜索相似组中的照片"}
  else $("#searchInput").placeholder="搜索文件名或路径";
  loadView();
};
$$(".multi-filter-trigger").forEach(button=>button.onclick=e=>{
  e.stopPropagation();const owner=button.closest(".multi-filter"),panel=owner.querySelector(".multi-filter-panel"),opening=panel.classList.contains("hidden");
  closeFilterMenus();if(opening){panel.classList.remove("hidden");button.setAttribute("aria-expanded","true")}
});
$$(".multi-filter-panel").forEach(panel=>panel.onclick=e=>e.stopPropagation());
$$("[data-filter-group]").forEach(input=>input.onchange=()=>{
  const group=input.dataset.filterGroup,values=state.filters[group];
  input.checked?values.add(input.value):values.delete(input.value);
  syncFilterControls();setActiveNav(libraryPresetName());loadView();
});
$$("[data-select-all]").forEach(button=>button.onclick=()=>{
  const group=button.dataset.selectAll,all=group==="decisions"?DECISION_VALUES:AI_VALUES;
  state.filters[group]=new Set(all);syncFilterControls();setActiveNav(libraryPresetName());loadView();
});
let searchTimer;$("#searchInput").oninput=()=>{
  if(state.view==="similar"){if(state.similar.selectedId)state.similar.memberSearch=$("#searchInput").value.trim();else state.similar.listSearch=$("#searchInput").value.trim()}
  clearTimeout(searchTimer);searchTimer=setTimeout(loadView,250);
};
$("#profileSelect").onchange=e=>applyProfile(e.target.value);$("#viewerPrev").onclick=()=>moveViewer(-1);$("#viewerNext").onclick=()=>moveViewer(1);
$("#viewerKeep").onclick=()=>setDecision(state.items[state.viewerIndex].id,"keep");$("#viewerRemove").onclick=()=>setDecision(state.items[state.viewerIndex].id,"remove");
$("#viewer").addEventListener("close",()=>syncViewerDecisions().catch(e=>toast(e.message)));
$("#viewerImage").addEventListener("click",e=>{if(state.viewerTransform.suppressClick)return;clearTimeout(state.viewerClickTimer);state.viewerClickTimer=setTimeout(()=>zoomViewer(1.5,e.clientX,e.clientY),220)});
$("#viewerImage").addEventListener("dblclick",e=>{e.preventDefault();clearTimeout(state.viewerClickTimer);resetViewerTransform()});
$("#viewerImage").addEventListener("wheel",e=>{e.preventDefault();zoomViewer(e.deltaY<0?1.18:1/1.18,e.clientX,e.clientY)},{passive:false});
$("#viewerImage").addEventListener("mousedown",e=>{if(e.button!==0||state.viewerTransform.scale<=1)return;e.preventDefault();const t=state.viewerTransform;Object.assign(t,{dragging:true,moved:false,startClientX:e.clientX,startClientY:e.clientY,startX:t.x,startY:t.y});applyViewerTransform()});
window.addEventListener("mousemove",e=>{const t=state.viewerTransform;if(!t.dragging)return;const dx=e.clientX-t.startClientX,dy=e.clientY-t.startClientY;if(Math.abs(dx)+Math.abs(dy)>3)t.moved=true;t.x=t.startX+dx;t.y=t.startY+dy;clampViewerPan();applyViewerTransform()});
window.addEventListener("mouseup",()=>{const t=state.viewerTransform;if(!t.dragging)return;t.dragging=false;if(t.moved){t.suppressClick=true;setTimeout(()=>t.suppressClick=false,0)}applyViewerTransform()});
document.addEventListener("click",e=>{if(!e.target.closest("#recentMenu"))closeRecentMenu();if(!e.target.closest(".multi-filter"))closeFilterMenus()});
document.querySelector("main").addEventListener("scroll",closeRecentMenu,{passive:true});
document.addEventListener("keydown",e=>{if(e.key==="Escape"){closeRecentMenu();if($$(".multi-filter-panel:not(.hidden)").length){closeFilterMenus();e.preventDefault();return}if($$("dialog[open]").length)return;if(state.view==="similar"&&state.similar.selectedId){e.preventDefault();closeSimilarDetail()}return}if(!$("#viewer").open)return;const k=e.key.toLowerCase();if(["arrowleft","a"].includes(k))moveViewer(-1);else if(["arrowright","d"].includes(k))moveViewer(1);else if(["arrowup","w"].includes(k))setDecision(state.items[state.viewerIndex].id,"keep");else if(["arrowdown","s"].includes(k))setDecision(state.items[state.viewerIndex].id,"remove");else return;e.preventDefault()});
$("#similarCollapseBtn").onclick=()=>closeSimilarDetail();
$("#similarBackBtn").onclick=()=>closeSimilarDetail();
$("#similarExpandBtn").onclick=expandSimilarDetail;
$("#similarFolderPane").onclick=e=>{if(state.similar.mode==="side"&&!e.target.closest("[data-similar-group]"))closeSimilarDetail()};
window.addEventListener("resize",()=>{if(state.view==="similar"&&state.similar.selectedId&&window.innerWidth<=850&&state.similar.mode==="side")expandSimilarDetail()});
$("#exportBtn").onclick=async()=>{try{const r=await json("/api/export/save",{project_id:state.project.id});if(r.saved)toast(`CSV 已保存到：${r.path}`)}catch(e){toast(`导出失败：${e.message}`)}};
$("#importBtn").onclick=async()=>{const f=await json("/api/choose-csv",{});if(!f.path)return;const r=await json("/api/import",{project_id:state.project.id,path:f.path});toast(`导入 ${r.imported} 条，缺失 ${r.missing} 条`);await refreshProject();loadView()};
$("#quarantineBtn").onclick=quarantine;
$("#clearDecisionsBtn").onclick=confirmClearDecisions;
$("#markAiRemoveBtn").onclick=confirmAiRemoveSuggestions;
$$("[data-setting]").forEach(b=>b.onclick=()=>{$$("[data-setting]").forEach(x=>x.classList.remove("active"));b.classList.add("active");$("#generalSettings").classList.toggle("hidden",b.dataset.setting!=="general");$("#profileSettings").classList.toggle("hidden",b.dataset.setting!=="profiles")});
$("#autoAdvance").onchange=async e=>{state.settings.auto_advance=e.target.checked;await json("/api/settings",{auto_advance:e.target.checked})};
$("#autoCheckUpdates").onchange=async e=>{state.settings.auto_check_updates=e.target.checked;await json("/api/settings",{auto_check_updates:e.target.checked})};
$("#checkUpdateBtn").onclick=()=>checkForUpdates(true);
$("#defaultCacheBtn").onclick=async()=>{
  const button=$("#defaultCacheBtn");
  try{
    const selected=await json("/api/choose-cache",{});if(!selected.path)return;
    button.disabled=true;
    const saved=await json("/api/settings",{default_cache_root:selected.path});
    const cacheRoot=saved.settings.default_cache_root;
    state.settings.default_cache_root=cacheRoot;$("#defaultCache").value=cacheRoot;
    toast("默认位置已保存");
  }catch(e){toast(`保存失败：${e.message}`)}finally{button.disabled=false}
};
$("#projectCacheBtn").onclick=async()=>{
  if(!state.project)return;
  const button=$("#projectCacheBtn");
  try{
    const selected=await json("/api/choose-cache",{});if(!selected.path)return;
    button.disabled=true;
    const migration=await json("/api/project/cache",{project_id:state.project.id,cache_root:selected.path});
    state.project.cache_root=migration.cache_root;
    $("#projectCache").value=migration.cache_root;
    if(migration.old_cache){
      $("#oldCaches").innerHTML=`<p>旧缓存已保留：${esc(migration.old_cache)}</p><button id="cleanOld">确认清理旧缓存</button>`;
      $("#cleanOld").onclick=async()=>{try{await json("/api/project/cache/cleanup",{project_id:state.project.id,path:migration.old_cache});$("#oldCaches").innerHTML="";toast("旧缓存已清理")}catch(e){toast(`清理失败：${e.message}`)}};
    }
    toast(migration.changed?"迁移完成，旧缓存仍保留":"当前项目已使用此存储位置");
  }catch(e){toast(`迁移失败：${e.message}`)}finally{button.disabled=false}
};
$("#profileEditorSelect").onchange=e=>editorLoad(e.target.value);$("#cloneProfile").onclick=cloneProfile;$("#saveProfile").onclick=saveProfile;$("#estimateBtn").onclick=estimate;
$("#deleteProfile").onclick=confirmDeleteProfile;
$$(".form-grid [data-p]").forEach(el=>{const b=document.createElement("button");b.type="button";b.className="field-reset";b.textContent="↺";b.title="恢复基础模式默认值";el.parentElement.insertBefore(b,el);b.onclick=()=>{const base=state.profiles.find(x=>x.id===(state.editor?.base_mode||"balanced"))||state.profiles.find(x=>x.id==="balanced");const v=getPath(base,el.dataset.p);if(v!==undefined){setPath(state.editor,el.dataset.p,v);el.value=v}}});
const numberRanges={"quality.min_megapixels_review":[0,500],"quality.min_megapixels_remove":[0,500],"quality.min_size_kb_review":[0,10000000],"quality.min_size_kb_remove":[0,10000000],"similarity.time_window_minutes":[0,10080],"similarity.sequence_gap":[0,10000],"similarity.min_group_size":[2,1000]};
$$('.form-grid input[type="number"][data-p]').forEach(el=>{const range=numberRanges[el.dataset.p]||[el.min,el.max];if(range[0]!==""&&range[0]!==undefined)el.min=range[0];if(range[1]!==""&&range[1]!==undefined)el.max=range[1];el.placeholder=`请输入 ${el.min}–${el.max}`;el.addEventListener("input",()=>{if(String(el.value).trim()){el.classList.remove("input-invalid");el.closest("label")?.classList.remove("field-invalid")}})});
$("#profileName").addEventListener("input",()=>{if($("#profileName").value.trim()){$("#profileName").classList.remove("input-invalid");$("#profileName").closest("label")?.classList.remove("field-invalid")}});
const libraryObserver=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting))loadLibraryPage(false)},{rootMargin:"600px 0px"});
libraryObserver.observe($("#librarySentinel"));
boot().catch(e=>toast(e.message));
