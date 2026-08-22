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
const LIVE_PHOTO_ICON=`<svg class="live-photo-icon" viewBox="0 0 24 24" aria-hidden="true"><circle class="live-photo-ring" cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="1.2"/><g class="live-photo-dots"><circle class="live-photo-dot" cx="12" cy="4.2" r=".62"/><circle class="live-photo-dot" cx="14.99" cy="4.81" r=".62"/><circle class="live-photo-dot" cx="17.52" cy="6.48" r=".62"/><circle class="live-photo-dot" cx="19.19" cy="9.01" r=".62"/><circle class="live-photo-dot" cx="19.8" cy="12" r=".62"/><circle class="live-photo-dot" cx="19.19" cy="14.99" r=".62"/><circle class="live-photo-dot" cx="17.52" cy="17.52" r=".62"/><circle class="live-photo-dot" cx="14.99" cy="19.19" r=".62"/><circle class="live-photo-dot" cx="12" cy="19.8" r=".62"/><circle class="live-photo-dot" cx="9.01" cy="19.19" r=".62"/><circle class="live-photo-dot" cx="6.48" cy="17.52" r=".62"/><circle class="live-photo-dot" cx="4.81" cy="14.99" r=".62"/><circle class="live-photo-dot" cx="4.2" cy="12" r=".62"/><circle class="live-photo-dot" cx="4.81" cy="9.01" r=".62"/><circle class="live-photo-dot" cx="6.48" cy="6.48" r=".62"/><circle class="live-photo-dot" cx="9.01" cy="4.81" r=".62"/></g></svg>`;
const MOTION_PLAY_ICON=`<svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="/static/assets/icons.svg?v=2#motion-play"></use></svg>`;
const MOTION_PAUSE_ICON=`<svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="/static/assets/icons.svg?v=2#motion-pause"></use></svg>`;
const MOTION_MUTED_ICON=`<svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="/static/assets/icons.svg?v=1#motion-muted"></use></svg>`;
const MOTION_SOUND_ICON=`<svg viewBox="0 0 1024 1024" aria-hidden="true"><use href="/static/assets/icons.svg?v=1#motion-sound"></use></svg>`;
function photoCard(p,index,customBadge="",customKind="",extraInfo=""){
  const badge=customBadge||(p.suggestion==="remove"?"建议移除":p.suggestion==="review"?"人工复查":p.suggestion==="unreadable"?"无法读取":"");
  const badgeKind=customKind||(p.suggestion==="remove"?"remove":p.suggestion==="review"?"review":p.suggestion==="unreadable"?"unreadable":"");
  const decisionClass=p.decision==="keep"?"decision-keep":p.decision==="remove"?"decision-remove":"";
  const badgeAttribute=customBadge?"data-context-badge":"data-analysis-badge";
  return `<article class="photo-card ${decisionClass}" data-photo-id="${p.id}"><div class="thumb" data-open-id="${p.id}"><img loading="lazy" src="${p.thumb_url}" alt="">${p.media_type==="motion_photo"?`<span class="live-mark card-live-mark" aria-label="动态照片">${LIVE_PHOTO_ICON}</span>`:""}${badge?`<span class="badge badge-${badgeKind}" ${badgeAttribute}>${esc(badge)}</span>`:""}</div><div class="card-info"><b title="${esc(p.relative_path)}">${esc(p.relative_path.split("/").pop())}</b><small>${esc(p.reason||`${p.width||0}×${p.height||0} · ${formatSize(p.size||0)}`)}</small>${extraInfo?`<span class="similarity-score">${esc(extraInfo)}</span>`:""}</div><div class="card-actions"><button class="keep" data-decision="keep" data-id="${p.id}">保留</button><button class="danger" data-decision="remove" data-id="${p.id}">移除</button></div></article>`;
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
function viewerSuggestion(p){if(p._viewerBadge)return {text:p._viewerBadge,kind:p._viewerKind};if(p.suggestion==="remove")return {text:"建议移除",kind:"remove"};if(p.suggestion==="review")return {text:"人工复查",kind:"review"};return {text:"",kind:""}}
function syncCardAnalysis(p){const suggestion=viewerSuggestion({...p,_viewerBadge:"",_viewerKind:""});$$(`[data-photo-id="${p.id}"]`).forEach(card=>{const thumb=card.querySelector(".thumb"),contextBadge=thumb?.querySelector("[data-context-badge]");let badge=thumb?.querySelector("[data-analysis-badge]");if(suggestion.text&&!contextBadge){if(!badge){badge=document.createElement("span");badge.dataset.analysisBadge="";thumb.appendChild(badge)}badge.className=`badge badge-${suggestion.kind}`;badge.textContent=suggestion.text}else badge?.remove();const image=card.querySelector("img"),detail=card.querySelector("small");if(image)image.src=p.thumb_url;if(detail)detail.textContent=p.reason||`${p.width||0}×${p.height||0} · ${formatSize(p.size||0)}`})}
function updateViewerDecision(p){$("#viewerKeep").classList.toggle("active",p.decision==="keep");$("#viewerRemove").classList.toggle("active",p.decision==="remove")}
function viewerTransformTarget(){return state.viewerMotion.active?$("#viewerVideo"):$("#viewerImage")}
function applyViewerTransform(){const t=state.viewerTransform,target=viewerTransformTarget();[$("#viewerImage"),$("#viewerVideo")].forEach(media=>{if(media!==target){media.style.transform="";media.classList.remove("zoomed","dragging")}});target.style.transform=`translate3d(${t.x}px,${t.y}px,0) scale(${t.scale})`;target.classList.toggle("zoomed",t.scale>1);target.classList.toggle("dragging",t.dragging)}
function clampViewerPan(){const t=state.viewerTransform,target=viewerTransformTarget(),maxX=Math.max(0,target.offsetWidth*(t.scale-1)/2),maxY=Math.max(0,target.offsetHeight*(t.scale-1)/2);t.x=Math.max(-maxX,Math.min(maxX,t.x));t.y=Math.max(-maxY,Math.min(maxY,t.y))}
function resetViewerTransform(){Object.assign(state.viewerTransform,{scale:1,x:0,y:0,dragging:false,moved:false,suppressClick:false});clearTimeout(state.viewerClickTimer);applyViewerTransform()}
function zoomViewer(factor,clientX,clientY){const t=state.viewerTransform,figure=viewerTransformTarget().parentElement,rect=figure.getBoundingClientRect(),old=t.scale,next=Math.max(1,Math.min(8,old*factor));if(next===old)return;const pointX=(clientX??rect.left+rect.width/2)-(rect.left+rect.width/2),pointY=(clientY??rect.top+rect.height/2)-(rect.top+rect.height/2),ratio=next/old;t.x=pointX-(pointX-t.x)*ratio;t.y=pointY-(pointY-t.y)*ratio;t.scale=next;if(next===1){t.x=0;t.y=0}clampViewerPan();applyViewerTransform()}
function motionClock(ms){const seconds=Math.max(0,Math.floor(ms/1000));return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,"0")}`}
function lastMotionFrameTime(p){const duration=p?.motion?.duration_ms||0,fps=p?.motion?.fps||0,frameDuration=fps>0?Math.max(1,Math.ceil(1000/fps)):1;return Math.max(0,duration-frameDuration)}
function motionMarkerPosition(time,lastFrame){const percent=lastFrame?time/lastFrame*100:0,edgeOffset=6-percent*0.12;return {percent,position:`calc(${percent}% + ${edgeOffset}px)`}}
function syncMotionCoverMarkers(p){const wrap=$("#motionTimelineWrap"),lastFrame=lastMotionFrameTime(p),motionCover=p?.motion?.cover_source==="motion",stillTime=Math.min(lastFrame,Math.max(0,p?.motion?.still_time_ms||0)),coverTime=motionCover?Math.min(lastFrame,Math.max(0,p.motion.cover_time_ms||0)):stillTime,current=motionMarkerPosition(coverTime,lastFrame),original=motionMarkerPosition(stillTime,lastFrame);wrap.style.setProperty("--motion-cover-percent",`${current.percent}%`);wrap.style.setProperty("--motion-cover-position",current.position);wrap.style.setProperty("--motion-original-position",original.position);$("#motionCoverMarker").title=`当前封面 · ${motionClock(coverTime)}`;$("#motionOriginalMarker").title=`原始封面 · ${motionClock(stillTime)}`;$("#motionOriginalMarker").classList.toggle("hidden",!motionCover||Math.abs(coverTime-stillTime)<1)}
function stopViewerMotion(){const video=$("#viewerVideo");video.pause();video.removeAttribute("src");video.load();video.classList.add("hidden");$("#motionControls").classList.add("hidden");$("#viewer").classList.remove("viewer-motion-open");state.viewerMotion={active:false,scrubbing:false}}
function syncMotionTime(){const p=state.items[state.viewerIndex],duration=p?.motion?.duration_ms||0,current=Math.round(($("#viewerVideo").currentTime||0)*1000),timeline=$("#motionTimeline"),play=$("#motionPlay"),paused=$("#viewerVideo").paused;timeline.value=String(Math.min(lastMotionFrameTime(p),current));timeline.style.setProperty("--motion-progress",`${duration?Math.min(100,current/duration*100):0}%`);$("#motionTime").textContent=`${motionClock(current)} / ${motionClock(duration)}`;play.innerHTML=paused?MOTION_PLAY_ICON:MOTION_PAUSE_ICON;play.setAttribute("aria-label",paused?"播放":"暂停");play.title=paused?"播放":"暂停"}
function syncMotionVolume(){const video=$("#viewerVideo"),button=$("#motionMute"),muted=video.muted;button.innerHTML=muted?MOTION_MUTED_ICON:MOTION_SOUND_ICON;button.setAttribute("aria-label",muted?"播放声音":"静音");button.title=muted?"播放声音":"静音"}
function toggleMotionPlayback(){const video=$("#viewerVideo");if(video.paused){if(video.ended)video.currentTime=0;video.play()}else video.pause()}
function toggleMotionMute(){const video=$("#viewerVideo");video.muted=!video.muted;syncMotionVolume()}
function motionCoverTime(p){return Math.min(lastMotionFrameTime(p),Math.max(0,p.motion.cover_source==="motion"?p.motion.cover_time_ms:p.motion.still_time_ms||0))}
function freezeMotionAtCover(p){const video=$("#viewerVideo"),apply=()=>{if(state.items[state.viewerIndex]?.id!==p.id||!state.viewerMotion.active)return;video.pause();video.currentTime=motionCoverTime(p)/1000;syncMotionTime()};if(video.readyState>=1)apply();else video.addEventListener("loadedmetadata",apply,{once:true})}
async function locateMotionStillTime(p){if((p?.motion?.still_time_ms??-1)>=0)return;try{const result=await json("/api/motion/locate",{project_id:state.project.id,photo_id:p.id});p.motion.still_time_ms=result.still_time_ms;if(state.items[state.viewerIndex]?.id===p.id){$("#motionTimeline").value=String(motionCoverTime(p));syncMotionCoverMarkers(p);freezeMotionAtCover(p)}}catch{p.motion.still_time_ms=0}}
function setupMotionViewer(p){const video=$("#viewerVideo"),lastFrame=lastMotionFrameTime(p),coverTime=motionCoverTime(p);state.viewerMotion={active:true,scrubbing:false};$("#viewer").classList.add("viewer-motion-open");$("#motionControls").classList.remove("hidden");$("#motionTimeline").max=String(lastFrame);$("#motionTimeline").value=String(coverTime);syncMotionCoverMarkers(p);video.poster=p.photo_url;video.src=p.motion.video_url;video.classList.remove("hidden");$("#viewerImage").classList.add("hidden");resetViewerTransform();syncMotionTime();syncMotionVolume();freezeMotionAtCover(p);locateMotionStillTime(p)}
function chooseMotionSourceWriteback(){const mode=state.settings.motion_cover_writeback||"ask";if(mode==="never")return Promise.resolve(false);if(mode==="always")return Promise.resolve(true);return new Promise(resolve=>{const dialog=$("#motionWritebackConfirm"),checkbox=$("#motionWritebackDontAsk"),yes=$("#motionWritebackYes"),no=$("#motionWritebackNo");let settled=false;checkbox.checked=false;yes.disabled=false;no.disabled=false;const finish=async writeSource=>{if(settled)return;settled=true;yes.disabled=true;no.disabled=true;if(checkbox.checked){const next=writeSource?"always":"never";try{const saved=await json("/api/settings",{motion_cover_writeback:next});state.settings.motion_cover_writeback=saved.settings.motion_cover_writeback||next;$("#motionCoverWriteback").value=state.settings.motion_cover_writeback}catch(error){toast(`保存原图修改设置失败：${error.message}`)}}dialog.close();resolve(writeSource)};yes.onclick=()=>finish(true);no.onclick=()=>finish(false);dialog.oncancel=event=>{event.preventDefault();finish(false)};dialog.showModal()})}
async function saveMotionCover(source="motion",timeMs=null){const p=state.items[state.viewerIndex];if(!p?.motion)return;const button=source==="still"?$("#motionResetCover"):$("#motionSetCover");button.disabled=true;try{const writeSource=source==="motion"?await chooseMotionSourceWriteback():false,requested=timeMs??Math.round($("#viewerVideo").currentTime*1000),selectedTime=source==="motion"?Math.min(lastMotionFrameTime(p),Math.max(0,requested)):0,result=await json("/api/motion/cover",{project_id:state.project.id,photo_id:p.id,source,time_ms:selectedTime,write_source:writeSource});state.items[state.viewerIndex]=result.photo;state.viewerNeedsRefresh=true;state.viewerDirtyIds.add(p.id);applyProjectCounts(result.project_counts);syncCardAnalysis(result.photo);openViewer(state.viewerIndex);toast(source==="still"?"已恢复原始封面":result.source_written?"封面分析已更新，原图已备份并修改":"封面和照片分析已更新")}catch(error){toast(`保存封面失败：${error.message}`)}finally{button.disabled=false}}
function openViewer(i){if(!state.items.length)return;stopViewerMotion();state.viewerIndex=(i+state.items.length)%state.items.length;const p=state.items[state.viewerIndex],suggestion=viewerSuggestion(p),badge=$("#viewerBadge"),analysisBadge=$("#viewerAnalysisBadge"),img=$("#viewerImage");resetViewerTransform();img.classList.remove("hidden");img.src=p.photo_url;$("#viewerName").textContent=p.relative_path.split("/").pop();$("#viewerMeta").textContent=`${p.width||0} × ${p.height||0} · ${formatSize(p.size||0)}${Number.isFinite(p.quality_score)?` · ${p.quality_score} 分`:""}${p.reason?" · "+p.reason:""}${p.motion?.error?" · 动态部分不可用":""}`;if(p.media_type==="motion_photo"){badge.innerHTML=LIVE_PHOTO_ICON;badge.setAttribute("aria-label","动态照片");analysisBadge.textContent=suggestion.text;analysisBadge.className=`viewer-badge ${suggestion.kind?`badge-${suggestion.kind}`:"hidden"}`}else{badge.textContent=suggestion.text;badge.removeAttribute("aria-label");analysisBadge.className="viewer-badge hidden"}badge.className=`viewer-badge ${p.media_type==="motion_photo"?"viewer-live-mark":suggestion.kind?`badge-${suggestion.kind}`:"hidden"}`;updateViewerDecision(p);$("#viewerIndex").textContent=`${state.viewerIndex+1} / ${state.items.length}`;if(!$("#viewer").open)$("#viewer").showModal();if(p.motion&&!p.motion.error)setupMotionViewer(p)}
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

function bindViewerEvents(){
  $("#viewerPrev").onclick=()=>moveViewer(-1);
  $("#viewerNext").onclick=()=>moveViewer(1);
  $("#viewerKeep").onclick=()=>setDecision(state.items[state.viewerIndex].id,"keep");
  $("#viewerRemove").onclick=()=>setDecision(state.items[state.viewerIndex].id,"remove");
  $("#viewer").addEventListener("close",()=>{
    stopViewerMotion();
    syncViewerDecisions().catch(error=>toast(error.message));
  });
  ["timeupdate","play","pause","ended"].forEach(name=>$("#viewerVideo").addEventListener(name,syncMotionTime));
  $("#viewerVideo").addEventListener("volumechange",syncMotionVolume);
  $("#motionMute").onclick=toggleMotionMute;
  $("#motionPlay").onclick=toggleMotionPlayback;
  $("#motionTimeline").oninput=event=>{
    $("#viewerVideo").pause();
    $("#viewerVideo").currentTime=Number(event.target.value)/1000;
    syncMotionTime();
  };
  $("#motionSetCover").onclick=()=>saveMotionCover("motion");
  $("#motionResetCover").onclick=()=>saveMotionCover("still",0);
  $("#viewerImage").addEventListener("click",event=>{
    if(state.viewerTransform.suppressClick)return;
    clearTimeout(state.viewerClickTimer);
    state.viewerClickTimer=setTimeout(()=>zoomViewer(1.5,event.clientX,event.clientY),220);
  });
  $("#viewerImage").addEventListener("dblclick",event=>{
    event.preventDefault();
    clearTimeout(state.viewerClickTimer);
    resetViewerTransform();
  });
  $("#viewerVideo").addEventListener("click",()=>{
    if(!state.viewerTransform.suppressClick)toggleMotionPlayback();
  });
  [$("#viewerImage"),$("#viewerVideo")].forEach(media=>{
    media.addEventListener("wheel",event=>{
      event.preventDefault();
      zoomViewer(event.deltaY<0?1.18:1/1.18,event.clientX,event.clientY);
    },{passive:false});
    media.addEventListener("mousedown",event=>{
      if(event.button!==0||state.viewerTransform.scale<=1)return;
      event.preventDefault();
      const transform=state.viewerTransform;
      Object.assign(transform,{dragging:true,moved:false,startClientX:event.clientX,startClientY:event.clientY,startX:transform.x,startY:transform.y});
      applyViewerTransform();
    });
  });
  window.addEventListener("mousemove",event=>{
    const transform=state.viewerTransform;
    if(!transform.dragging)return;
    const dx=event.clientX-transform.startClientX;
    const dy=event.clientY-transform.startClientY;
    if(Math.abs(dx)+Math.abs(dy)>3)transform.moved=true;
    transform.x=transform.startX+dx;
    transform.y=transform.startY+dy;
    clampViewerPan();
    applyViewerTransform();
  });
  window.addEventListener("mouseup",()=>{
    const transform=state.viewerTransform;
    if(!transform.dragging)return;
    transform.dragging=false;
    if(transform.moved){
      transform.suppressClick=true;
      setTimeout(()=>transform.suppressClick=false,0);
    }
    applyViewerTransform();
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
