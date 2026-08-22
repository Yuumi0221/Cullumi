function handleGlobalKeydown(event){
  if(event.key==="Escape"){
    closeRecentMenu();
    if($$(".multi-filter-panel:not(.hidden)").length){
      closeFilterMenus();
      event.preventDefault();
      return;
    }
    if($$("dialog[open]").length)return;
    if(state.view==="similar"&&state.similar.selectedId){
      event.preventDefault();
      closeSimilarDetail();
    }
    return;
  }
  if(!$("#viewer").open)return;
  if(event.code==="Space"&&state.viewerMotion.active){
    event.preventDefault();
    if(!event.repeat)toggleMotionPlayback();
    return;
  }
  if(event.target.matches("input[type=range]"))return;
  const key=event.key.toLowerCase();
  if(["arrowleft","a"].includes(key))moveViewer(-1);
  else if(["arrowright","d"].includes(key))moveViewer(1);
  else if(["arrowup","w"].includes(key))setDecision(state.items[state.viewerIndex].id,"keep");
  else if(["arrowdown","s"].includes(key))setDecision(state.items[state.viewerIndex].id,"remove");
  else return;
  event.preventDefault();
}

function bindGlobalEvents(){
  $("#githubBtn").onclick=async()=>{
    try{
      await json("/api/open-github",{});
    }catch(error){
      toast(error.message);
    }
  };
  $("#themeBtn").onclick=()=>applyTheme(state.theme==="night"?"day":"night",true);
  $$('[data-close]').forEach(button=>button.onclick=closeDialogs);
  $$('dialog[data-backdrop-close]').forEach(dialog=>dialog.addEventListener("click",closeNoticeOnBackdrop));
  document.addEventListener("click",event=>{
    if(!event.target.closest("#recentMenu"))closeRecentMenu();
    if(!event.target.closest(".multi-filter"))closeFilterMenus();
  });
  document.addEventListener("keydown",handleGlobalKeydown);
}

function startApplication(){
  bindSessionEvents();
  bindSimilarEvents();
  bindSettingsEvents();
  bindGalleryEvents();
  bindGlobalEvents();
  boot().catch(error=>toast(error.message));
}

startApplication();
