const TOKEN=window.APP_TOKEN;
const state={project:null,view:"quality",qualityFilter:"",items:[],profiles:[],settings:{},viewerIndex:0,editor:null,poll:null,lastScan:null};
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const api=async(path,body)=>{
  const opts=body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-App-Token":TOKEN},body:JSON.stringify(body)};
  const sep=path.includes("?")?"&":"?"; const res=await fetch(path+sep+"token="+encodeURIComponent(TOKEN),opts);
  if(!res.ok){let e={};try{e=await res.json()}catch{}throw Error(e.error||`请求失败 (${res.status})`)} return res;
};
const json=async(p,b)=>await (await api(p,b)).json();
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const toast=m=>{const t=$("#toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2400)};
const formatSize=n=>n<1024?`${n} B`:n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`;
const stageName={starting:"准备扫描",discovering:"正在发现照片",analyzing:"正在解码与分析",hashing:"正在确认完全重复",grouping:"正在建立相似组",complete:"扫描完成",cancelled:"扫描已取消",error:"扫描出错"};

async function boot(){
  const b=await json("/api/bootstrap");state.profiles=b.profiles;state.settings=b.settings;
  renderProfiles();$("#autoAdvance").checked=!!b.settings.auto_advance;$("#defaultCache").value=b.settings.default_cache_root;
  $("#recentList").innerHTML=b.recent_projects.length?b.recent_projects.map(p=>`<button class="recent" data-pid="${p.id}"><b>${esc(p.root.split(/[\\/]/).pop())}</b><span>${esc(p.root)}</span><small>${p.available?"可打开":"存储目录当前不可用"}</small></button>`).join(""):`<div class="empty"><b>暂无最近项目</b><span>选择一个照片文件夹即可开始。</span></div>`;
  $$(".recent").forEach(x=>x.onclick=()=>openProject(x.dataset.pid));
}
function renderProfiles(){
  const opts=state.profiles.map(p=>`<option value="${p.id}">${esc(p.name)}${p.builtin?" · 内置":""}</option>`).join("");
  $("#profileSelect").innerHTML=opts;$("#profileEditorSelect").innerHTML=opts;
  if(state.project)$("#profileSelect").value=state.project.profile_id;
}
async function chooseProject(){const r=await json("/api/choose-folder",{});if(!r.path)return;const p=await json("/api/project/open",{root:r.path});await showProject(p);await startScan()}
async function openProject(pid){try{await showProject(await json(`/api/project?project_id=${pid}`))}catch(e){toast(e.message)}}
async function showProject(p){
  state.project=p;$("#home").classList.add("hidden");$("#workspace").classList.remove("hidden");
  $("#projectName").textContent=p.root.split(/[\\/]/).pop();$("#projectPath").textContent=p.root;$("#projectCache").value=p.cache_root;
  $("#profileSelect").value=p.profile_id;updateCounts(p);await loadView();
}
function updateCounts(p){
  $("#allCount").textContent=p.total;$("#qualityCount").textContent=(p.counts.remove||0)+(p.counts.review||0);
  $("#unreadableCount").textContent=p.counts.unreadable||0;$("#pairCount").textContent=p.pairs;
  $("#decidedCount").textContent=Object.values(p.decisions||{}).reduce((a,b)=>a+b,0);
}
async function refreshProject(){if(!state.project)return;state.project=await json(`/api/project?project_id=${state.project.id}`);updateCounts(state.project)}
async function startScan(){if(!state.project)return;await json("/api/scan",{project_id:state.project.id});pollProgress()}
function pollProgress(){
  clearInterval(state.poll);$("#progressPanel").classList.remove("hidden");
  state.poll=setInterval(async()=>{const p=await json(`/api/progress?project_id=${state.project.id}`);
    $("#progressTitle").textContent=stageName[p.stage]||p.stage;$("#progressDetail").textContent=p.file||`${p.current||0} / ${p.total||0}`;
    $("#progressBar").style.width=p.total?`${Math.round(100*(p.current||0)/p.total)}%`:(p.done?"100%":"5%");
    if(p.done){clearInterval(state.poll);state.lastScan=p;if(p.error)toast(p.error);else if(!p.total&&p.video_count)toast(`未发现照片；发现 ${p.video_count} 个视频，当前版本不支持视频`);else if(!p.total)toast("未发现支持的照片文件");else toast(stageName[p.stage]||"扫描结束");await refreshProject();await loadView();setTimeout(()=>$("#progressPanel").classList.add("hidden"),2500)}
  },700);
}
async function loadView(){
  if(!state.project)return;const search=encodeURIComponent($("#searchInput").value.trim());
  $("#viewTitle").textContent={quality:"质量候选",similar:"相似连拍",all:"全部照片",unreadable:"无法读取",decided:"已决定",quarantine:"隔离历史"}[state.view];
  $("#qualityFilters").classList.toggle("hidden",state.view!=="quality");
  try{
    if(state.view==="similar"){const d=await json(`/api/pairs?project_id=${state.project.id}&search=${search}`);state.items=d.items;renderPairs(d.items)}
    else if(state.view==="quarantine"){const d=await json(`/api/quarantine/batches?project_id=${state.project.id}`);renderBatches(d.items)}
    else{const suggestion=state.view==="quality"&&state.qualityFilter?`&suggestion=${state.qualityFilter}`:"";const d=await json(`/api/photos?project_id=${state.project.id}&category=${state.view}&search=${search}&limit=500${suggestion}`);state.items=d.items;renderPhotos(d.items,d.total)}
  }catch(e){toast(e.message)}
}
const decisionLabel=d=>d==="keep"?"已保留":d==="remove"?"待移除":"";
function photoCard(p,index){
  const badge=p.suggestion==="remove"?"建议移除":p.suggestion==="review"?"人工复看":p.suggestion==="unreadable"?"无法读取":"";
  return `<article class="photo-card"><div class="thumb" data-open="${index}"><img loading="lazy" src="${p.thumb_url}" alt=""><span class="badge">${esc(badge)}</span>${p.decision?`<span class="badge decision">${decisionLabel(p.decision)}</span>`:""}</div><div class="card-info"><b title="${esc(p.relative_path)}">${esc(p.relative_path.split("/").pop())}</b><small>${esc(p.reason||`${p.width||0}×${p.height||0} · ${formatSize(p.size||0)}`)}</small></div><div class="card-actions"><button class="keep" data-decision="keep" data-id="${p.id}">保留</button><button class="danger" data-decision="remove" data-id="${p.id}">移除</button></div></article>`;
}
function renderPhotos(items,total){$("#viewSubtitle").textContent=`显示 ${items.length} / ${total}`;$("#gallery").innerHTML=items.map(photoCard).join("");const empty=!items.length;$("#empty").classList.toggle("hidden",!empty);if(empty&&state.lastScan?.total===0){$("#emptyTitle").textContent="没有发现支持的照片";const p=state.lastScan;const ext=Object.entries(p.unsupported_extensions||{}).sort((a,b)=>b[1]-a[1]).map(([x,n])=>`${x} ${n} 个`).join("、");$("#emptyText").textContent=p.video_count?`此文件夹有 ${p.video_count} 个视频，但照片筛选器暂不支持视频。${ext?" 文件统计："+ext:""}`:`发现 ${p.unsupported_count||0} 个不支持的文件。${ext}`;}else if(empty){$("#emptyTitle").textContent="这里还没有内容";$("#emptyText").textContent="扫描完成后会显示结果。"}bindGallery()}
function renderPairs(items){
  $("#viewSubtitle").textContent=`${items.length} 组比较`;$("#gallery").innerHTML=items.map((x,i)=>`<article class="pair-card"><div class="pair-images">${["a","b"].map(k=>{const p=x[k];return `<div class="pair-side ${p.id===x.recommended_id?"recommended":""}">${photoCard(p,i*2+(k==="b"?1:0)).replace('class="photo-card"','class="pair-photo"')}</div>`}).join("")}</div><div class="pair-label">${x.kind==="exact"?"完全重复":"相似度 "+Math.round(x.score*100)+"%"} · 绿色边框为推荐保留${x.face_safe?" · 人物照片请检查表情":""}</div></article>`).join("");
  state.items=items.flatMap(x=>[x.a,x.b]);$("#empty").classList.toggle("hidden",!!items.length);bindGallery()
}
function bindGallery(){
  $$("[data-open]").forEach(x=>x.onclick=()=>openViewer(+x.dataset.open));
  $$("[data-decision]").forEach(x=>x.onclick=e=>{e.stopPropagation();setDecision(+x.dataset.id,x.dataset.decision,false)})
}
function renderBatches(items){
  $("#viewSubtitle").textContent=`${items.length} 个批次`;$("#gallery").innerHTML=items.map(x=>`<article class="photo-card"><div class="card-info"><b>${esc(x.created_at)}</b><small>${x.count} 张 · ${formatSize(x.total_size)}${x.restored_at?" · 已恢复":""}</small></div><div class="card-actions"><button data-restore="${x.id}" ${x.restored_at?"disabled":""}>恢复此批次</button></div></article>`).join("");$("#empty").classList.toggle("hidden",!!items.length);$$("[data-restore]").forEach(b=>b.onclick=()=>restore(b.dataset.restore))
}
function openViewer(i){if(!state.items.length)return;state.viewerIndex=(i+state.items.length)%state.items.length;const p=state.items[state.viewerIndex];$("#viewerImage").src=p.photo_url;$("#viewerName").textContent=p.relative_path;$("#viewerMeta").textContent=`${p.width||0} × ${p.height||0} · ${formatSize(p.size||0)}${p.reason?" · "+p.reason:""}`;$("#viewerIndex").textContent=`${state.viewerIndex+1} / ${state.items.length}`;if(!$("#viewer").open)$("#viewer").showModal()}
const moveViewer=d=>openViewer(state.viewerIndex+d);
async function setDecision(id,decision,fromViewer=true){await json("/api/decision",{project_id:state.project.id,photo_id:id,decision});const p=state.items.find(x=>x.id===id);if(p)p.decision=decision;toast(decision==="keep"?"已标记保留":"已标记移除");await refreshProject();if(fromViewer&&state.settings.auto_advance)moveViewer(1);else if(!fromViewer)loadView()}
async function quarantine(){
  const d=await json(`/api/quarantine/preview?project_id=${state.project.id}`);if(!d.count){toast("没有已标记移除的照片");return}
  $("#confirmTitle").textContent=`确认隔离 ${d.count} 张照片？`;$("#confirmBody").innerHTML=`<p>总大小 ${formatSize(d.total_size)}。照片将移入项目内的可恢复隔离区，不会直接删除。</p><div class="confirm-list">${d.items.map(x=>esc(x.relative_path)).join("<br>")}</div>`;
  $("#confirmOk").onclick=async()=>{$("#confirm").close();const r=await json("/api/quarantine/apply",{project_id:state.project.id});toast(`已隔离 ${r.moved} 张，跳过 ${r.skipped} 张`);await refreshProject();loadView()};$("#confirm").showModal()
}
async function restore(id){const r=await json("/api/quarantine/restore",{project_id:state.project.id,batch_id:id});toast(`恢复 ${r.restored} 张，文件名冲突 ${r.conflicts} 张`);loadView()}
function getPath(o,path){return path.split(".").reduce((a,k)=>a?.[k],o)}
function setPath(o,path,v){const ks=path.split(".");let a=o;ks.slice(0,-1).forEach(k=>a=a[k]);a[ks.at(-1)]=v}
function editorLoad(id){
  const p=structuredClone(state.profiles.find(x=>x.id===id));state.editor=p;$("#profileName").value=p.name;$("#profileName").disabled=p.builtin;$("#saveProfile").disabled=p.builtin;$("#deleteProfile").disabled=p.builtin;
  $$("[data-p]").forEach(el=>{const v=getPath(p,el.dataset.p);if(el.type==="checkbox")el.checked=!!v;else el.value=v});
}
function editorRead(){const p=state.editor;p.name=$("#profileName").value.trim();$$("[data-p]").forEach(el=>setPath(p,el.dataset.p,el.type==="checkbox"?el.checked:(el.type==="number"?Number(el.value):el.value)));return p}
async function saveProfile(){try{const p=await json("/api/profile/save",{profile:editorRead()});const i=state.profiles.findIndex(x=>x.id===p.id);if(i>=0)state.profiles[i]=p;else state.profiles.push(p);renderProfiles();$("#profileEditorSelect").value=p.id;editorLoad(p.id);toast("自定义模式已保存")}catch(e){toast(e.message)}}
async function cloneProfile(){const source=state.profiles.find(x=>x.id===$("#profileEditorSelect").value);const p=structuredClone(source);p.base_mode=source.builtin?source.id:(source.base_mode||"balanced");p.id="";p.name=p.name+" 副本";p.builtin=false;p.quality.blur_review_percentile??=5;p.quality.blur_remove_percentile??=1;delete p.created_at;delete p.updated_at;state.editor=p;$("#profileName").disabled=false;$("#saveProfile").disabled=false;$("#deleteProfile").disabled=true;$("#profileName").value=p.name;$$("[data-p]").forEach(el=>{const v=getPath(p,el.dataset.p);el.type==="checkbox"?el.checked=!!v:el.value=v});toast("请命名并保存新模式")}
async function applyProfile(id){try{state.project=await json("/api/profile/apply",{project_id:state.project.id,profile_id:id});updateCounts(state.project);toast("已切换分析模式");loadView()}catch(e){toast(e.message)}}
async function estimate(){try{const d=await json("/api/profile/estimate",{project_id:state.project.id,profile:editorRead()});$("#estimate").textContent=`预计：建议移除 ${d.counts.remove||0} 张，人工复看 ${d.counts.review||0} 张；当前相似关系 ${d.current_pairs} 组，保存应用后将重建相似关系。`}catch(e){toast(e.message)}}
function closeDialogs(e){const d=e.target.closest("dialog");if(d)d.close()}

$("#chooseBtn").onclick=chooseProject;$("#scanBtn").onclick=startScan;$("#cancelBtn").onclick=()=>json("/api/scan/cancel",{project_id:state.project.id});
$("#homeBtn").onclick=()=>{$("#workspace").classList.add("hidden");$("#home").classList.remove("hidden");$("#searchInput").value="";clearInterval(state.poll);boot().catch(e=>toast(e.message))};
$("#settingsBtn").onclick=()=>{$("#settings").showModal();if(state.project)$("#projectCache").value=state.project.cache_root;$("#profileEditorSelect").value=state.project?.profile_id||state.profiles[0]?.id;editorLoad($("#profileEditorSelect").value)};
$$("[data-close]").forEach(x=>x.onclick=closeDialogs);
$("#nav").onclick=e=>{const b=e.target.closest("[data-view]");if(!b)return;$$("[data-view]").forEach(x=>x.classList.remove("active"));b.classList.add("active");state.view=b.dataset.view;loadView()};
$$("[data-quality-filter]").forEach(b=>b.onclick=()=>{const value=b.dataset.qualityFilter;state.qualityFilter=state.qualityFilter===value?"":value;$$("[data-quality-filter]").forEach(x=>x.classList.toggle("active",x.dataset.qualityFilter===state.qualityFilter));loadView()});
let searchTimer;$("#searchInput").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadView,250)};
$("#profileSelect").onchange=e=>applyProfile(e.target.value);$("#viewerPrev").onclick=()=>moveViewer(-1);$("#viewerNext").onclick=()=>moveViewer(1);
$("#viewerKeep").onclick=()=>setDecision(state.items[state.viewerIndex].id,"keep");$("#viewerRemove").onclick=()=>setDecision(state.items[state.viewerIndex].id,"remove");
document.addEventListener("keydown",e=>{if(!$("#viewer").open)return;const k=e.key.toLowerCase();if(["arrowleft","a"].includes(k))moveViewer(-1);else if(["arrowright","d"].includes(k))moveViewer(1);else if(["arrowup","w"].includes(k))setDecision(state.items[state.viewerIndex].id,"keep");else if(["arrowdown","s"].includes(k))setDecision(state.items[state.viewerIndex].id,"remove");else return;e.preventDefault()});
$("#exportBtn").onclick=()=>window.open(`/api/export?project_id=${state.project.id}&token=${encodeURIComponent(TOKEN)}`);
$("#importBtn").onclick=async()=>{const f=await json("/api/choose-csv",{});if(!f.path)return;const r=await json("/api/import",{project_id:state.project.id,path:f.path});toast(`导入 ${r.imported} 条，缺失 ${r.missing} 条`);refreshProject();loadView()};
$("#quarantineBtn").onclick=quarantine;
$$("[data-setting]").forEach(b=>b.onclick=()=>{$$("[data-setting]").forEach(x=>x.classList.remove("active"));b.classList.add("active");$("#generalSettings").classList.toggle("hidden",b.dataset.setting!=="general");$("#profileSettings").classList.toggle("hidden",b.dataset.setting!=="profiles")});
$("#autoAdvance").onchange=async e=>{state.settings.auto_advance=e.target.checked;await json("/api/settings",{auto_advance:e.target.checked})};
$("#defaultCacheBtn").onclick=async()=>{const r=await json("/api/choose-cache",{});if(r.path){$("#defaultCache").value=r.path;state.settings.default_cache_root=r.path;await json("/api/settings",{default_cache_root:r.path});toast("默认位置已保存")}};
$("#projectCacheBtn").onclick=async()=>{if(!state.project)return;const r=await json("/api/choose-cache",{});if(!r.path)return;const m=await json("/api/project/cache",{project_id:state.project.id,cache_root:r.path});$("#projectCache").value=r.path;if(m.old_cache){$("#oldCaches").innerHTML=`<p>旧缓存已保留：${esc(m.old_cache)}</p><button id="cleanOld">确认清理旧缓存</button>`;$("#cleanOld").onclick=async()=>{await json("/api/project/cache/cleanup",{project_id:state.project.id,path:m.old_cache});$("#oldCaches").innerHTML="";toast("旧缓存已清理")}}toast("迁移完成，旧缓存仍保留")};
$("#profileEditorSelect").onchange=e=>editorLoad(e.target.value);$("#cloneProfile").onclick=cloneProfile;$("#saveProfile").onclick=saveProfile;$("#estimateBtn").onclick=estimate;
$("#deleteProfile").onclick=async()=>{try{await json("/api/profile/delete",{profile_id:state.editor.id});state.profiles=state.profiles.filter(x=>x.id!==state.editor.id);renderProfiles();editorLoad(state.profiles[0].id);toast("已删除")}catch(e){toast(e.message)}};
$$(".form-grid [data-p]").forEach(el=>{const b=document.createElement("button");b.type="button";b.className="field-reset";b.textContent="↺";b.title="恢复基础模式默认值";el.parentElement.insertBefore(b,el);b.onclick=()=>{const base=state.profiles.find(x=>x.id===(state.editor?.base_mode||"balanced"))||state.profiles.find(x=>x.id==="balanced");const v=getPath(base,el.dataset.p);if(v!==undefined){setPath(state.editor,el.dataset.p,v);el.value=v}}});
boot().catch(e=>toast(e.message));
