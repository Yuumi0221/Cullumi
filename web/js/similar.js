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
  renderSimilarFolders();applySimilarMode();
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

function bindSimilarEvents(){
  $("#similarCollapseBtn").onclick=()=>closeSimilarDetail();
  $("#similarBackBtn").onclick=()=>closeSimilarDetail();
  $("#similarExpandBtn").onclick=expandSimilarDetail;
  $("#similarFolderPane").onclick=event=>{
    if(state.similar.mode==="side"&&!event.target.closest("[data-similar-group]"))closeSimilarDetail();
  };
  $("#similarFolders").onclick=event=>{
    const button=event.target.closest("[data-similar-group]");
    if(!button)return;
    event.stopPropagation();
    openSimilarGroup(button.dataset.similarGroup);
  };
  window.addEventListener("resize",()=>{
    if(state.view==="similar"&&state.similar.selectedId&&window.innerWidth<=850&&state.similar.mode==="side")expandSimilarDetail();
  });
}
