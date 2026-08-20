const TOKEN=window.APP_TOKEN;
const DECISION_VALUES=["undecided","keep","remove"],AI_VALUES=["remove","review","no_suggestion"],LIBRARY_PAGE_SIZE=120;
const state={project:null,view:"library",activeNav:"library",items:[],profiles:[],settings:{},recentProjects:[],recentQuery:"",recentMenuId:"",viewerIndex:0,viewerNeedsRefresh:false,viewerDirtyIds:new Set(),editor:null,poll:null,lastScan:null,theme:localStorage.getItem("Cullumi-theme")||"day",filters:{decisions:new Set(DECISION_VALUES),ai:new Set(AI_VALUES)},library:{offset:0,total:0,done:false,loading:false,generation:0},similar:{groups:[],selectedId:"",mode:"closed",listSearch:"",memberSearch:""},viewerTransform:{scale:1,x:0,y:0,dragging:false,moved:false,suppressClick:false},viewerClickTimer:null,updateChecked:false,updateChecking:false};
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const api=async(path,body)=>{
  const opts=body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-App-Token":TOKEN},body:JSON.stringify(body)};
  const sep=path.includes("?")?"&":"?"; const res=await fetch(path+sep+"token="+encodeURIComponent(TOKEN),opts);
  if(!res.ok){let e={};try{e=await res.json()}catch{}throw Error(e.error||`请求失败 (${res.status})`)} return res;
};
const json=async(p,b)=>await (await api(p,b)).json();
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let toastTimer;
const toast=m=>{
  const t=$("#toast"),dialogs=$$("dialog[open]"),host=dialogs.at(-1)||document.body;
  if(t.parentElement!==host)host.appendChild(t);
  t.textContent=m;t.classList.add("show");clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>t.classList.remove("show"),2400);
};
const formatSize=n=>n<1024?`${n} B`:n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`;
const stageName={starting:"准备扫描",discovering:"正在发现照片",analyzing:"正在解码与分析",hashing:"正在确认完全重复",grouping:"正在建立相似组",complete:"扫描完成",cancelled:"扫描已取消",error:"扫描出错"};
function applyTheme(theme,persist=false){state.theme=theme;document.documentElement.dataset.theme=theme;localStorage.setItem("Cullumi-theme",theme);const night=theme==="night",button=$("#themeBtn");button.title=night?"切换日间模式":"切换夜间模式";button.setAttribute("aria-label",button.title);if(persist)json("/api/settings",{theme}).catch(e=>toast(`主题保存失败：${e.message}`))}
applyTheme(state.theme);

const recentProjectName=project=>project.root.split(/[\\/]/).pop()||project.root;
function recentProjectTime(value){
  const opened=new Date(value||0);if(Number.isNaN(opened.getTime()))return "最近使用";
  const now=new Date(),today=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  const day=new Date(opened.getFullYear(),opened.getMonth(),opened.getDate());
  const days=Math.max(0,Math.floor((today-day)/86400000));
  if(days===0)return `今天 ${String(opened.getHours()).padStart(2,"0")}:${String(opened.getMinutes()).padStart(2,"0")}`;
  if(days===1)return "昨天";if(days<7)return `${days} 天前`;if(days<14)return "上周";
  return `${opened.getMonth()+1} 月 ${opened.getDate()} 日`;
}
function renderRecentProjects(){
  const query=state.recentQuery.trim().toLocaleLowerCase();
  const projects=state.recentProjects.filter(project=>recentProjectName(project).toLocaleLowerCase().includes(query));
  $("#recentList").innerHTML=projects.length?projects.map(project=>`<button class="recent" data-pid="${project.id}" title="${esc(project.root)}"><span class="recent-thumb">${project.thumbnail_url?`<img src="${esc(project.thumbnail_url)}" alt="">`:`<span class="recent-thumb-empty"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6Z"/></svg></span>`}</span><span class="recent-info"><b>${esc(recentProjectName(project))}</b><span class="recent-meta">${project.total||0} 张&nbsp; · &nbsp;已留 ${project.kept||0}</span><small>${project.available?recentProjectTime(project.last_opened):"目录当前不可用"}</small></span><span class="recent-more" aria-label="项目操作" title="项目操作"><svg viewBox="0 0 18 4" aria-hidden="true"><circle cx="2" cy="2" r="2"/><circle cx="9" cy="2" r="2"/><circle cx="16" cy="2" r="2"/></svg></span></button>`).join(""):`<div class="empty recent-empty"><b>${state.recentProjects.length?"没有匹配的项目":"暂无最近筛选"}</b><span>${state.recentProjects.length?"请尝试其他项目名称":"选择一个照片文件夹即可开始"}</span></div>`;
  $$(".recent").forEach(item=>{item.onclick=event=>event.target.closest(".recent-more")?openRecentMenu(event,item.dataset.pid):openProject(item.dataset.pid);item.oncontextmenu=event=>openRecentMenu(event,item.dataset.pid)});
}

async function boot(){
  const b=await json("/api/bootstrap");state.profiles=b.profiles;state.settings=b.settings;state.recentProjects=b.recent_projects;
  applyTheme(b.settings.theme||state.theme);
  $("#appVersion").textContent=`v${b.version}`;
  renderProfiles();$("#autoAdvance").checked=!!b.settings.auto_advance;$("#autoCheckUpdates").checked=!!b.settings.auto_check_updates;$("#defaultCache").value=b.settings.default_cache_root;
  renderRecentProjects();
  if(b.startup_warning&&!$("#startupWarning").dataset.shown){$("#startupWarning").dataset.shown="1";$("#startupWarningBody").textContent=b.startup_warning;$("#startupWarning").showModal()}
  if(b.settings.auto_check_updates&&!state.updateChecked){state.updateChecked=true;setTimeout(()=>checkForUpdates(false),450)}
}
function renderProfiles(){
  const opts=state.profiles.map(p=>`<option value="${p.id}">${esc(p.name)}${p.builtin?" · 内置":""}</option>`).join("");
  $("#profileSelect").innerHTML=opts;$("#profileEditorSelect").innerHTML=opts;
  if(state.project)$("#profileSelect").value=state.project.profile_id;
}
async function chooseProject(){const r=await json("/api/choose-folder",{});if(!r.path)return;const p=await json("/api/project/open",{root:r.path});await showProject(p);await startScan()}
async function openProject(pid){try{await showProject(await json(`/api/project?project_id=${pid}`))}catch(e){toast(e.message)}}
async function showProject(p){
  state.project=p;state.view="library";state.activeNav="library";state.filters={decisions:new Set(DECISION_VALUES),ai:new Set(AI_VALUES)};state.similar={groups:[],selectedId:"",mode:"closed",listSearch:"",memberSearch:""};document.body.classList.add("project-open");$("#home").classList.add("hidden");$("#workspace").classList.remove("hidden");
  $("#projectName").textContent=p.root.split(/[\\/]/).pop();$("#projectPath").textContent=p.root;$("#projectCache").value=p.cache_root;
  $("#profileSelect").value=p.profile_id;$("#searchInput").value="";syncFilterControls();updateCounts(p);setActiveNav("library");await loadView();
}
function updateCounts(p){
  const c=p.library_counts||{};
  $("#libraryCount").textContent=c.readable??p.total??0;$("#aiCount").textContent=c.ai_pending??0;
  $("#undecidedCount").textContent=c.undecided??0;$("#keepCount").textContent=c.keep??p.decisions?.keep??0;
  $("#removeCount").textContent=c.remove??p.decisions?.remove??0;$("#unreadableCount").textContent=c.unreadable??p.counts?.unreadable??0;
  const aiRemovePending=c.ai_remove_pending??0;$("#aiRemovePendingCount").textContent=aiRemovePending;$("#markAiRemoveBtn").disabled=!aiRemovePending;
  $("#pairCount").textContent=p.similar_groups??p.pairs;
  $("#clearDecisionsBtn").disabled=!Object.values(p.decisions||{}).reduce((sum,count)=>sum+count,0);
}
async function refreshProject(){if(!state.project)return;state.project=await json(`/api/project?project_id=${state.project.id}`);updateCounts(state.project)}
async function startScan(){if(!state.project)return;await json("/api/scan",{project_id:state.project.id});pollProgress()}
function pollProgress(){
  clearInterval(state.poll);$("#progressPanel").classList.remove("hidden");
  state.poll=setInterval(async()=>{const p=await json(`/api/progress?project_id=${state.project.id}`);
    $("#progressTitle").textContent=stageName[p.stage]||p.stage;$("#progressDetail").textContent=p.file||`${p.current||0} / ${p.total||0}`;
    $("#progressBar").style.width=p.total?`${Math.round(100*(p.current||0)/p.total)}%`:(p.done?"100%":"5%");
    if(p.done){clearInterval(state.poll);state.lastScan=p;if(p.error)toast(p.error);else if(!p.total&&p.video_count)toast(`未发现照片；发现 ${p.video_count} 个视频，当前版本不支持视频`);else if(!p.total)toast("未发现支持的照片文件");else if(p.unavailable_count)toast(`扫描完成，${p.unavailable_count} 张照片在扫描期间不可用，已安全跳过`);else toast(stageName[p.stage]||"扫描结束");await refreshProject();await loadView();setTimeout(()=>$("#progressPanel").classList.add("hidden"),2500)}
  },700);
}
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
function similarFolder(group,compact=false){
  const coverImages=group.covers.map((photo,index)=>`<img class="folder-cover cover-${index}" loading="lazy" src="${photo.thumb_url}" alt="">`).reverse().join("");
  const name=group.recommended.relative_path.split("/").pop();
  return `<button class="similar-folder ${compact?"compact":""} ${group.id===state.similar.selectedId?"active":""}" data-similar-group="${group.id}"><span class="folder-stack">${coverImages}<i>${group.count} 张</i></span><span class="folder-caption"><b title="${esc(group.recommended.relative_path)}">${esc(name)}</b><small>${group.kind==="exact"?"完全重复":`${group.count} 张相似照片`}</small></span></button>`;
}
function renderSimilarFolders(){
  const selected=!!state.similar.selectedId;
  $("#similarFolders").innerHTML=state.similar.groups.map(group=>similarFolder(group,selected)).join("");
  $("#similarFolders").classList.toggle("compact",selected&&state.similar.mode==="side");
  $("#similarFolderPane").classList.toggle("hidden",selected&&state.similar.mode==="expanded");
  $$("[data-similar-group]").forEach(button=>button.onclick=e=>{e.stopPropagation();openSimilarGroup(button.dataset.similarGroup)});
}
function applySimilarMode(){
  const selected=!!state.similar.selectedId,expanded=state.similar.mode==="expanded",visible=state.view==="similar"&&selected;
  $("#similarBrowser").classList.toggle("detail-open",selected);
  $("#similarBrowser").classList.toggle("detail-expanded",selected&&expanded);
  $("#similarDetail").classList.toggle("hidden",!selected);
  $("#similarViewActions").classList.toggle("hidden",!visible);
  $("#similarCollapseBtn").classList.toggle("hidden",expanded);
  $("#similarExpandBtn").classList.toggle("hidden",expanded);
  $("#similarBackBtn").classList.toggle("hidden",!expanded);
  $("#similarFolderPane").classList.toggle("hidden",selected&&expanded);
  document.body.classList.toggle("similar-detail-open",state.view==="similar"&&selected);
  document.body.classList.toggle("similar-side-open",state.view==="similar"&&selected&&!expanded);
}
async function loadSimilarView(){
  const listSearch=encodeURIComponent(state.similar.listSearch);
  const list=await json(`/api/similar-groups?project_id=${state.project.id}&search=${listSearch}`);
  state.similar.groups=list.items;
  if(state.similar.selectedId&&!list.items.some(group=>group.id===state.similar.selectedId)){
    closeSimilarDetail(false);
    toast("原相似组已发生变化，已返回相似组列表");
  }
  renderSimilarFolders();applySimilarMode();
  if(state.similar.selectedId)await loadSimilarGroupMembers();
  else{
    state.items=[];
    $("#viewSubtitle").textContent=`${list.total} 组相似照片${list.items.some(group=>group.face_safe)?" · 人物照片请检查表情":""}`;
    $("#empty").classList.toggle("hidden",!!list.items.length);
  }
}
async function loadSimilarGroupMembers(){
  const groupId=state.similar.selectedId;
  const search=encodeURIComponent(state.similar.memberSearch);
  const detail=await json(`/api/similar-group?project_id=${state.project.id}&group_id=${encodeURIComponent(groupId)}&search=${search}`);
  const decorated=detail.members.map(photo=>{
    const recommended=photo.id===detail.recommended_id;
    return {...photo,_viewerBadge:recommended?"推荐保留":"可考虑移除",_viewerKind:recommended?"recommended":"candidate-remove"};
  });
  state.items=decorated;
  $("#viewSubtitle").textContent=`当前组 ${detail.count} 张${detail.face_safe?" · 人物照片请检查表情":""}${state.similar.memberSearch?` · 显示 ${decorated.length} 张`:""}`;
  $("#similarDetailGallery").innerHTML=decorated.map((photo,index)=>{
    const recommended=photo.id===detail.recommended_id;
    const extra=recommended?"":detail.kind==="exact"?"完全重复":`相似度 ${Math.round((photo.group_similarity||0)*100)}%`;
    return photoCard(photo,index,recommended?"推荐保留":"可考虑移除",recommended?"recommended":"candidate-remove",extra);
  }).join("");
  $("#empty").classList.toggle("hidden",!!decorated.length);
  bindGallery();renderSimilarFolders();applySimilarMode();
}
async function openSimilarGroup(groupId){
  state.similar.selectedId=groupId;
  state.similar.memberSearch="";
  state.similar.mode=window.innerWidth<=850?"expanded":"side";
  $("#searchInput").value="";
  $("#searchInput").placeholder="搜索当前组照片";
  renderSimilarFolders();applySimilarMode();
  try{await loadSimilarGroupMembers()}catch(e){closeSimilarDetail();toast(e.message)}
}
function closeSimilarDetail(restoreSearch=true){
  state.similar.selectedId="";
  state.similar.mode="closed";
  state.similar.memberSearch="";
  state.items=[];
  if(restoreSearch){
    $("#searchInput").value=state.similar.listSearch;
    $("#searchInput").placeholder="搜索相似组中的照片";
  }
  $("#similarDetailGallery").innerHTML="";
  renderSimilarFolders();applySimilarMode();
  $("#viewSubtitle").textContent=`${state.similar.groups.length} 组相似照片${state.similar.groups.some(group=>group.face_safe)?" · 人物照片请检查表情":""}`;
  $("#empty").classList.toggle("hidden",!!state.similar.groups.length);
}
function expandSimilarDetail(){
  if(!state.similar.selectedId)return;
  state.similar.mode="expanded";
  renderSimilarFolders();applySimilarMode();
}
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
function getPath(o,path){return path.split(".").reduce((a,k)=>a?.[k],o)}
function setPath(o,path,v){const ks=path.split(".");let a=o;ks.slice(0,-1).forEach(k=>a=a[k]);a[ks.at(-1)]=v}
function editorLoad(id){
  const p=structuredClone(state.profiles.find(x=>x.id===id));state.editor=p;$("#profileName").value=p.name;$("#profileName").disabled=p.builtin;$("#saveProfile").disabled=p.builtin;$("#deleteProfile").disabled=p.builtin;
  $$("[data-p]").forEach(el=>{const v=getPath(p,el.dataset.p);if(el.type==="checkbox")el.checked=!!v;else el.value=v});
  clearProfileValidation();
}
function editorRead(){const p=state.editor;p.name=$("#profileName").value.trim();$$("[data-p]").forEach(el=>setPath(p,el.dataset.p,el.type==="checkbox"?el.checked:(el.type==="number"?Number(el.value):el.value)));return p}
function profileSaveStatus(message,failed=false){const el=$("#profileSaveStatus");el.textContent=message;el.className=`profile-save-status ${failed?"failed":"success"}`}
function clearProfileValidation(){[$("#profileName"),...$$('.form-grid input[type="number"][data-p]')].forEach(el=>{el.classList.remove("input-invalid");el.closest("label")?.classList.remove("field-invalid")});$("#profileSaveStatus").className="profile-save-status hidden"}
function validateProfileInputs(showBottom=true){const fields=[$("#profileName"),...$$('.form-grid input[type="number"][data-p]')],missing=fields.filter(el=>!String(el.value).trim());fields.forEach(el=>{const invalid=missing.includes(el);el.classList.toggle("input-invalid",invalid);el.closest("label")?.classList.toggle("field-invalid",invalid)});if(missing.length){if(showBottom)profileSaveStatus("还有项目没有输入完整，请填写标红的项目。",true);missing[0].focus();return false}return true}
async function saveProfile(){if(!validateProfileInputs(true))return;const button=$("#saveProfile");button.disabled=true;profileSaveStatus("正在保存…");try{const p=await json("/api/profile/save",{profile:editorRead()});const i=state.profiles.findIndex(x=>x.id===p.id);if(i>=0)state.profiles[i]=p;else state.profiles.push(p);renderProfiles();$("#profileEditorSelect").value=p.id;editorLoad(p.id);profileSaveStatus("✓ 自定义模式已保存");toast("自定义模式已保存")}catch(e){profileSaveStatus(`保存失败：${e.message}`,true);toast(`保存失败：${e.message}`)}finally{button.disabled=!!state.editor?.builtin}}
async function cloneProfile(){const source=state.profiles.find(x=>x.id===$("#profileEditorSelect").value);const p=structuredClone(source);p.base_mode=source.builtin?source.id:(source.base_mode||"balanced");p.id="";p.name=p.name+" 副本";p.builtin=false;p.quality.blur_review_percentile??=5;p.quality.blur_remove_percentile??=1;delete p.created_at;delete p.updated_at;state.editor=p;clearProfileValidation();$("#profileName").disabled=false;$("#saveProfile").disabled=false;$("#deleteProfile").disabled=true;$("#profileName").value=p.name;$$("[data-p]").forEach(el=>{const v=getPath(p,el.dataset.p);el.type==="checkbox"?el.checked=!!v:el.value=v});toast("请命名并保存新模式")}
async function applyProfile(id){try{state.project=await json("/api/profile/apply",{project_id:state.project.id,profile_id:id});updateCounts(state.project);toast("已切换分析模式");loadView()}catch(e){toast(e.message)}}
async function estimate(){if(!validateProfileInputs(false)){$("#estimate").textContent="预估失败：还有项目没有输入完整，请填写标红的项目。";return}const button=$("#estimateBtn");button.disabled=true;$("#estimate").textContent="正在按当前参数完整预估…";try{const d=await json("/api/profile/estimate",{project_id:state.project.id,profile:editorRead()});$("#estimate").textContent=`预计：建议移除 ${d.counts.remove||0} 张，人工复看 ${d.counts.review||0} 张，相似组 ${d.estimated_groups||0} 组（${d.estimated_pairs||0} 条关系）。`}catch(e){$("#estimate").textContent=`预估失败：${e.message}`;toast(`预估失败：${e.message}`)}finally{button.disabled=false}}
function closeFilterMenus(){
  $$(".multi-filter-panel").forEach(panel=>panel.classList.add("hidden"));
  $$(".multi-filter-trigger").forEach(button=>button.setAttribute("aria-expanded","false"));
}
function closeDialogs(e){const d=e.target.closest("dialog");if(d)d.close()}
function closeNoticeOnBackdrop(event){
  const dialog=event.currentTarget,box=dialog.getBoundingClientRect();
  const outside=event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom;
  if(outside)dialog.close();
}
function closeRecentMenu(){state.recentMenuId="";$("#recentMenu").classList.add("hidden")}
function openRecentMenu(event,projectId){
  event.preventDefault();event.stopPropagation();state.recentMenuId=projectId;
  const project=state.recentProjects.find(x=>x.id===projectId);const menu=$("#recentMenu");
  $("#recentOpenFolder").disabled=!project?.available;menu.classList.remove("hidden");
  const left=Math.min(event.clientX,window.innerWidth-menu.offsetWidth-8);
  const top=Math.min(event.clientY,window.innerHeight-menu.offsetHeight-8);
  menu.style.left=`${Math.max(8,left)}px`;menu.style.top=`${Math.max(8,top)}px`;
}
async function openRecentFolder(){const id=state.recentMenuId;closeRecentMenu();if(!id)return;try{await json("/api/project/open-folder",{project_id:id})}catch(e){toast(e.message)}}
async function openCurrentProjectFolder(){if(!state.project)return;try{await json("/api/project/open-folder",{project_id:state.project.id})}catch(e){toast(e.message)}}
async function checkForUpdates(manual=true){
  if(state.updateChecking)return;state.updateChecking=true;const button=$("#checkUpdateBtn"),status=$("#updateStatus");button.disabled=true;if(manual)status.textContent="正在连接 GitHub…";
  try{
    const update=await json("/api/update/check",{});
    if(update.update_available){status.textContent=`发现 v${update.latest_version}`;showUpdatePrompt(update)}
    else if(update.no_release){if(manual){status.textContent="发布页暂时没有可用版本";toast("暂时没有可用的发布版本")}}
    else if(manual){status.textContent=`当前 v${update.current_version} 已是最新版本`;toast("当前已是最新版本")}
  }catch(e){if(manual){status.textContent=`检查失败：${e.message}`;toast(`检查更新失败：${e.message}`)}else console.warn("自动检查更新失败",e)}
  finally{state.updateChecking=false;button.disabled=false}
}
function showUpdatePrompt(update){
  $("#updateTitle").textContent=`发现新版本 v${update.latest_version}`;
  $("#updateBody").innerHTML=update.download_available
    ?`<p>当前版本为 v${esc(update.current_version)}，是否将 <b>${esc(update.asset_name)}</b> 下载到系统 Downloads 文件夹？</p><p id="updateDownloadStatus" class="update-download-status">照片和项目数据不会受到影响。</p>`
    :`<p>当前版本为 v${esc(update.current_version)}，新版本已经发布，但发布页没有可直接下载的 Windows 附件。</p><p id="updateDownloadStatus" class="update-download-status">可以前往 Releases 页面查看详情。</p>`;
  const button=$("#updateDownload");button.disabled=false;button.textContent=update.download_available?"下载更新":"查看发布页";button.onclick=update.download_available?downloadUpdate:async()=>{await json("/api/update/open",{});$("#updateDialog").close()};
  $("#updateDialog").showModal();
}
async function downloadUpdate(){
  const button=$("#updateDownload"),status=$("#updateDownloadStatus");button.disabled=true;button.textContent="正在下载…";status.textContent="正在下载更新，请不要关闭软件。";
  try{const result=await json("/api/update/download",{});$("#updateTitle").textContent=`v${result.version} 已下载`;$("#updateBody").innerHTML=`<p>更新包已保存到：</p><p class="download-path">${esc(result.path)}</p><p>关闭当前软件后，解压并运行新版本即可。</p>`;button.disabled=false;button.textContent="完成";button.onclick=()=>$("#updateDialog").close();toast("更新包下载完成")}
  catch(e){status.textContent=`下载失败：${e.message}`;button.disabled=false;button.textContent="重试下载";toast(`下载失败：${e.message}`)}
}
function confirmRemoveRecent(){
  const id=state.recentMenuId;const project=state.recentProjects.find(x=>x.id===id);closeRecentMenu();if(!project)return;
  $("#confirmTitle").textContent="从最近项目中移除？";
  $("#confirmBody").innerHTML=`<p>“${esc(project.root.split(/[\\/]/).pop())}”将从首页列表移除。真实照片不会被删除或移动。</p><label class="toggle confirm-option"><input id="deleteProjectCache" type="checkbox"><span>同时删除项目数据库和缩略图</span></label><p class="confirm-note">不勾选时，数据库和缩略图会保留，今后重新打开该照片目录可继续使用。</p>`;
  $("#confirmOk").textContent="确认移除";
  $("#confirmOk").onclick=async()=>{const deleteCache=$("#deleteProjectCache").checked;try{await json("/api/project/remove-recent",{project_id:id,delete_cache:deleteCache});$("#confirm").close();toast(deleteCache?"项目数据库和缩略图已删除":"已从最近项目中移除");await boot()}catch(e){toast(e.message)}};
  $("#confirm").showModal();
}
function projectsUsingProfile(profileId){
  const projects=[],seen=new Set();
  [state.project,...state.recentProjects].filter(Boolean).forEach(project=>{if(project.id===state.project?.id&&project!==state.project)return;if(project.profile_id===profileId&&!seen.has(project.id)){seen.add(project.id);projects.push(project)}});
  return projects;
}
function showProfileInUseWarning(profile,projects=[]){
  const current=projects.some(project=>project.id===state.project?.id);
  let message=`“${profile.name}”仍被某个项目使用，暂时不能删除。请先将使用该模式的项目切换到其他分析模式，再删除此模式。`;
  if(current)message=`“${profile.name}”正在被当前项目使用，暂时不能删除。请先在窗口顶部切换到其他分析模式，再删除此模式。`;
  else if(projects.length){const names=projects.map(recentProjectName).join("”、“");message=`“${profile.name}”仍被项目“${names}”使用，暂时不能删除。请先打开该项目并切换到其他分析模式，再删除此模式。`}
  $("#profileInUseWarningBody").textContent=message;$("#profileInUseWarning").showModal();
}
function confirmDeleteProfile(){const profile=state.editor;if(!profile||profile.builtin)return;const projects=projectsUsingProfile(profile.id);if(projects.length){showProfileInUseWarning(profile,projects);return}$("#confirmTitle").textContent="删除自定义模式？";$("#confirmBody").textContent=`“${profile.name}”将从本机配置中删除，此操作无法撤销。`;$("#confirmOk").textContent="确认删除";$("#confirmOk").onclick=async()=>{try{await json("/api/profile/delete",{profile_id:profile.id});$("#confirm").close();state.profiles=state.profiles.filter(x=>x.id!==profile.id);renderProfiles();editorLoad(state.profiles[0].id);toast("自定义模式已删除")}catch(e){if(e.message.includes("被项目使用")||e.message.includes("切换项目模式")){$("#confirm").close();showProfileInUseWarning(profile)}else toast(e.message)}};$("#confirm").showModal()}
function confirmClearDecisions(){const count=Object.values(state.project?.decisions||{}).reduce((a,b)=>a+b,0);if(!count){toast("没有需要清空的选择");return}$("#confirmTitle").textContent="清空所有选择？";$("#confirmBody").textContent=`将清除当前项目中 ${count} 张照片的“保留/移除”选择，照片文件不会被移动或删除。`;$("#confirmOk").textContent="确认清空";$("#confirmOk").onclick=async()=>{try{const r=await json("/api/decision/clear",{project_id:state.project.id});$("#confirm").close();toast(`已清空 ${r.cleared} 张照片的选择`);await refreshProject();loadView()}catch(e){toast(e.message)}};$("#confirm").showModal()}
function confirmAiRemoveSuggestions(){
  const count=state.project?.library_counts?.ai_remove_pending??0;if(!count){toast("没有未决定的 AI 建议移除照片");return}
  $("#confirmTitle").textContent=`标记 ${count} 张建议移除照片？`;
  $("#confirmBody").textContent="这些照片会统一标记为“移除”。照片文件不会立即移动，之后仍需点击“隔离已标记移除”确认处理。";
  const button=$("#confirmOk");button.textContent="全部标记移除";button.onclick=async()=>{button.disabled=true;try{const r=await json("/api/decision/ai-remove",{project_id:state.project.id});$("#confirm").close();toast(`已将 ${r.marked} 张照片标记为移除`);await refreshProject();await loadView()}catch(e){toast(e.message)}finally{button.disabled=false}};
  $("#confirm").showModal();
}

$("#chooseBtn").onclick=chooseProject;$("#recentSearch").oninput=event=>{state.recentQuery=event.target.value;renderRecentProjects()};$("#scanBtn").onclick=startScan;$("#cancelBtn").onclick=()=>json("/api/scan/cancel",{project_id:state.project.id});
$("#homeBtn").onclick=()=>{document.body.classList.remove("project-open","similar-view-open","similar-detail-open","similar-side-open");$("#workspace").classList.add("hidden");$("#home").classList.remove("hidden");$("#searchInput").value="";clearInterval(state.poll);boot().catch(e=>toast(e.message))};
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
