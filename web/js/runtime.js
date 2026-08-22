const TOKEN=window.APP_TOKEN;
const DECISION_VALUES=["undecided","keep","remove"],AI_VALUES=["remove","review","no_suggestion"],LIBRARY_PAGE_SIZE=120;
const state={project:null,view:"library",activeNav:"library",items:[],profiles:[],settings:{},recentProjects:[],recentQuery:"",recentMenuId:"",recentGeneration:0,viewerIndex:0,viewerNeedsRefresh:false,viewerDirtyIds:new Set(),editor:null,poll:0,lastScan:null,theme:localStorage.getItem("Cullumi-theme")||"day",filters:{decisions:new Set(DECISION_VALUES),ai:new Set(AI_VALUES)},library:{offset:0,total:0,done:false,loading:false,generation:0},similar:{groups:[],selectedId:"",mode:"closed",listSearch:"",memberSearch:""},viewerTransform:{scale:1,x:0,y:0,dragging:false,moved:false,suppressClick:false},viewerMotion:{active:false,scrubbing:false},viewerClickTimer:null,updateChecked:false,updateChecking:false};
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
const stageName={starting:"准备扫描",discovering:"正在发现照片",analyzing:"正在解码与分析",hashing:"正在确认完全重复",grouping:"正在建立相似组",blink_detection:"正在检测眨眼",complete:"扫描完成",cancelled:"扫描已取消",error:"扫描出错"};
function applyTheme(theme,persist=false){state.theme=theme;document.documentElement.dataset.theme=theme;localStorage.setItem("Cullumi-theme",theme);const night=theme==="night",button=$("#themeBtn");button.title=night?"切换日间模式":"切换夜间模式";button.setAttribute("aria-label",button.title);if(persist)json("/api/settings",{theme}).catch(e=>toast(`主题保存失败：${e.message}`))}
applyTheme(state.theme);
