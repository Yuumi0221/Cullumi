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

