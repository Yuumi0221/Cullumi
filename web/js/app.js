[$("#gallery"),$("#similarDetailGallery")].forEach(container=>container.addEventListener("click",handleGalleryClick));
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
$("#viewer").addEventListener("close",()=>{stopViewerMotion();syncViewerDecisions().catch(e=>toast(e.message))});
$("#viewerVideo").addEventListener("timeupdate",syncMotionTime);$("#viewerVideo").addEventListener("play",syncMotionTime);$("#viewerVideo").addEventListener("pause",syncMotionTime);$("#viewerVideo").addEventListener("ended",syncMotionTime);$("#viewerVideo").addEventListener("volumechange",syncMotionVolume);
$("#motionMute").onclick=toggleMotionMute;$("#motionPlay").onclick=toggleMotionPlayback;
$("#motionTimeline").oninput=e=>{$("#viewerVideo").pause();$("#viewerVideo").currentTime=Number(e.target.value)/1000;syncMotionTime()};
$("#motionSetCover").onclick=()=>saveMotionCover("motion");$("#motionResetCover").onclick=()=>saveMotionCover("still",0);
$("#viewerImage").addEventListener("click",e=>{if(state.viewerTransform.suppressClick)return;clearTimeout(state.viewerClickTimer);state.viewerClickTimer=setTimeout(()=>zoomViewer(1.5,e.clientX,e.clientY),220)});
$("#viewerImage").addEventListener("dblclick",e=>{e.preventDefault();clearTimeout(state.viewerClickTimer);resetViewerTransform()});
$("#viewerVideo").addEventListener("click",()=>{if(!state.viewerTransform.suppressClick)toggleMotionPlayback()});
[$("#viewerImage"),$("#viewerVideo")].forEach(media=>{media.addEventListener("wheel",e=>{e.preventDefault();zoomViewer(e.deltaY<0?1.18:1/1.18,e.clientX,e.clientY)},{passive:false});media.addEventListener("mousedown",e=>{if(e.button!==0||state.viewerTransform.scale<=1)return;e.preventDefault();const t=state.viewerTransform;Object.assign(t,{dragging:true,moved:false,startClientX:e.clientX,startClientY:e.clientY,startX:t.x,startY:t.y});applyViewerTransform()})});
window.addEventListener("mousemove",e=>{const t=state.viewerTransform;if(!t.dragging)return;const dx=e.clientX-t.startClientX,dy=e.clientY-t.startClientY;if(Math.abs(dx)+Math.abs(dy)>3)t.moved=true;t.x=t.startX+dx;t.y=t.startY+dy;clampViewerPan();applyViewerTransform()});
window.addEventListener("mouseup",()=>{const t=state.viewerTransform;if(!t.dragging)return;t.dragging=false;if(t.moved){t.suppressClick=true;setTimeout(()=>t.suppressClick=false,0)}applyViewerTransform()});
document.addEventListener("click",e=>{if(!e.target.closest("#recentMenu"))closeRecentMenu();if(!e.target.closest(".multi-filter"))closeFilterMenus()});
document.querySelector("main").addEventListener("scroll",closeRecentMenu,{passive:true});
document.addEventListener("keydown",e=>{if(e.key==="Escape"){closeRecentMenu();if($$(".multi-filter-panel:not(.hidden)").length){closeFilterMenus();e.preventDefault();return}if($$("dialog[open]").length)return;if(state.view==="similar"&&state.similar.selectedId){e.preventDefault();closeSimilarDetail()}return}if(!$("#viewer").open)return;if(e.code==="Space"&&state.viewerMotion.active){e.preventDefault();if(!e.repeat)toggleMotionPlayback();return}if(e.target.matches("input[type=range]"))return;const k=e.key.toLowerCase();if(["arrowleft","a"].includes(k))moveViewer(-1);else if(["arrowright","d"].includes(k))moveViewer(1);else if(["arrowup","w"].includes(k))setDecision(state.items[state.viewerIndex].id,"keep");else if(["arrowdown","s"].includes(k))setDecision(state.items[state.viewerIndex].id,"remove");else return;e.preventDefault()});
$("#similarCollapseBtn").onclick=()=>closeSimilarDetail();
$("#similarBackBtn").onclick=()=>closeSimilarDetail();
$("#similarExpandBtn").onclick=expandSimilarDetail;
$("#similarFolderPane").onclick=e=>{if(state.similar.mode==="side"&&!e.target.closest("[data-similar-group]"))closeSimilarDetail()};
$("#similarFolders").onclick=e=>{const button=e.target.closest("[data-similar-group]");if(button){e.stopPropagation();openSimilarGroup(button.dataset.similarGroup)}};
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
