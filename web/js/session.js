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
  $("#recentList").innerHTML=projects.length?projects.map(project=>{
    const meta=project.stats_loaded?`${project.total||0} 张&nbsp; · &nbsp;已留 ${project.kept||0}`:"正在读取项目信息…";
    const status=!project.available?"目录当前不可用":project.load_error?"项目数据暂时无法读取":recentProjectTime(project.last_opened);
    return `<button class="recent" data-pid="${project.id}" title="${esc(project.load_error||project.root)}"><span class="recent-thumb">${project.thumbnail_url?`<img src="${esc(project.thumbnail_url)}" alt="">`:`<span class="recent-thumb-empty"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6Z"/></svg></span>`}</span><span class="recent-info"><b>${esc(recentProjectName(project))}</b><span class="recent-meta">${meta}</span><small>${esc(status)}</small></span><span class="recent-more" aria-label="项目操作" title="项目操作"><svg viewBox="0 0 18 4" aria-hidden="true"><circle cx="2" cy="2" r="2"/><circle cx="9" cy="2" r="2"/><circle cx="16" cy="2" r="2"/></svg></span></button>`;
  }).join(""):`<div class="empty recent-empty"><b>${state.recentProjects.length?"没有匹配的项目":"暂无最近筛选"}</b><span>${state.recentProjects.length?"请尝试其他项目名称":"选择一个照片文件夹即可开始"}</span></div>`;
  $$(".recent").forEach(item=>{item.onclick=event=>event.target.closest(".recent-more")?openRecentMenu(event,item.dataset.pid):openProject(item.dataset.pid);item.oncontextmenu=event=>openRecentMenu(event,item.dataset.pid)});
}

async function hydrateRecentProjects(generation){
  const queue=state.recentProjects.filter(project=>project.available&&!project.stats_loaded).map(project=>project.id);
  const worker=async()=>{
    while(queue.length&&generation===state.recentGeneration){
      const projectId=queue.shift();let payload;
      try{payload=await json(`/api/recent-project?project_id=${encodeURIComponent(projectId)}`)}
      catch(error){payload={...state.recentProjects.find(project=>project.id===projectId),stats_loaded:true,load_error:error.message}}
      if(generation!==state.recentGeneration)return;
      const index=state.recentProjects.findIndex(project=>project.id===projectId);
      if(index>=0){state.recentProjects[index]=payload;renderRecentProjects()}
    }
  };
  await Promise.all(Array.from({length:Math.min(3,queue.length)},worker));
}

async function boot(){
  const generation=++state.recentGeneration,b=await json("/api/bootstrap");if(generation!==state.recentGeneration)return;state.profiles=b.profiles;state.settings=b.settings;state.recentProjects=b.recent_projects;
  applyTheme(b.settings.theme||state.theme);
  $("#appVersion").textContent=`v${b.version}`;
  renderProfiles();$("#autoAdvance").checked=!!b.settings.auto_advance;$("#autoCheckUpdates").checked=!!b.settings.auto_check_updates;$("#defaultCache").value=b.settings.default_cache_root;
  renderRecentProjects();
  hydrateRecentProjects(generation);
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
function applyProjectCounts(counts){
  if(!state.project||!counts)return;
  Object.assign(state.project,counts);updateCounts(state.project);
}
async function refreshProject(){if(!state.project)return;state.project=await json(`/api/project?project_id=${state.project.id}`);updateCounts(state.project)}
async function startScan(){if(!state.project)return;await json("/api/scan",{project_id:state.project.id});pollProgress()}
const wait=milliseconds=>new Promise(resolve=>setTimeout(resolve,milliseconds));
async function pollProgress(){
  const generation=++state.poll,projectId=state.project.id;$("#progressPanel").classList.remove("hidden");
  try{
    while(generation===state.poll&&state.project?.id===projectId){
      const p=await json(`/api/progress?project_id=${projectId}`);if(generation!==state.poll||state.project?.id!==projectId)return;
      $("#progressTitle").textContent=stageName[p.stage]||p.stage;$("#progressDetail").textContent=p.file||`${p.current||0} / ${p.total||0}`;
      $("#progressBar").style.width=p.total?`${Math.round(100*(p.current||0)/p.total)}%`:(p.done?"100%":"5%");
      if(p.done){state.lastScan=p;if(p.error)toast(p.error);else if(!p.total&&p.video_count)toast(`未发现照片；发现 ${p.video_count} 个视频，当前版本不支持视频`);else if(!p.total)toast("未发现支持的照片文件");else if(p.unavailable_count)toast(`扫描完成，${p.unavailable_count} 张照片在扫描期间不可用，已安全跳过`);else toast(stageName[p.stage]||"扫描结束");await refreshProject();if(generation!==state.poll)return;await loadView();setTimeout(()=>{if(generation===state.poll)$("#progressPanel").classList.add("hidden")},2500);return}
      await wait(700);
    }
  }catch(error){if(generation===state.poll)toast(`读取扫描进度失败：${error.message}`)}
}
