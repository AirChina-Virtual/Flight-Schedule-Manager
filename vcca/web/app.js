/* vCCA web app — vanilla JS, no external dependencies. Hash routes:
   #/login #/overview #/jobs[/<id>] #/results[/<run>] #/data #/config #/help
   API: relative URLs (works behind a reverse proxy sub-path); writes carry X-VCCA-CSRF. */
(function(){
"use strict";
var S={user:null,csrf:null,season:null,seasons:[],current:null,maxUpload:512,pageToken:0,timers:[],dirty:false,reports:{}};
var NAV=[["overview","概览"],["jobs","运行"],["results","结果"],["data","数据"],["config","配置"],["help","说明"]];
var ROOT=document.getElementById("app");

/* ---------- helpers ---------- */
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];});}
function $(sel,root){return (root||document).querySelector(sel);}
function $$(sel,root){return Array.prototype.slice.call((root||document).querySelectorAll(sel));}
function pad(n){return (n<10?"0":"")+n;}
function tzLabel(d){var o=-d.getTimezoneOffset(),s=o>=0?"+":"-";o=Math.abs(o);return "UTC"+s+Math.floor(o/60)+(o%60?":"+pad(o%60):"");}
function fmtTime(ts,withTz){if(!ts)return "—";var d=new Date(ts*1000);
 return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+" "+pad(d.getHours())+":"+pad(d.getMinutes())+(withTz===false?"":" ("+tzLabel(d)+")");}
function fmtSize(n){if(n==null)return "—";var u=["B","KB","MB","GB"],i=0;while(n>=1024&&i<3){n/=1024;i++;}return (i?n.toFixed(1):n)+u[i];}
function fmtDur(s){s=Math.round(s||0);if(s<60)return s+" 秒";var m=Math.floor(s/60);if(m<60)return m+" 分 "+pad(s%60)+" 秒";return Math.floor(m/60)+" 时 "+pad(m%60)+" 分";}
function runLabel(name){return name+" UTC";}
function toast(msg,err){var t=$("#toast");if(!t)return;var d=document.createElement("div");if(err)d.className="err";d.textContent=msg;t.appendChild(d);setTimeout(function(){d.remove();},err?7000:3500);}
function setTimer(fn,ms){var id=setInterval(fn,ms);S.timers.push(id);return id;}
function clearTimers(){S.timers.forEach(clearInterval);S.timers=[];}
function q(obj){return Object.keys(obj).filter(function(k){return obj[k]!=null&&obj[k]!=="";}).map(function(k){return encodeURIComponent(k)+"="+encodeURIComponent(obj[k]);}).join("&");}
function sq(extra){var o={season:S.season};for(var k in extra)o[k]=extra[k];return q(o);}
function fileUrl(run,name){return "files/"+encodeURIComponent(S.season)+"/"+encodeURIComponent(run)+"/"+encodeURIComponent(name);}
function errList(list){return list&&list.length?"<ul>"+list.map(function(e){return "<li>"+esc(e)+"</li>";}).join("")+"</ul>":"";}

function api(method,url,body,opts){
 opts=opts||{};var headers={},init={method:method,credentials:"same-origin",headers:headers};
 if(body!==undefined){headers["Content-Type"]="application/json";init.body=JSON.stringify(body);}
 if(method!=="GET"&&S.csrf)headers["X-VCCA-CSRF"]=S.csrf;
 return fetch(url,init).then(function(r){return r.text().then(function(t){
  var d;try{d=t?JSON.parse(t):{};}catch(e){d={error:t.slice(0,300)};}
  if(r.status===401&&!opts.noRedirect){S.user=null;S.csrf=null;if(location.hash!=="#/login"){S.after=location.hash;location.hash="#/login";}}
  if(!r.ok){var e=new Error(d.error||("HTTP "+r.status));e.status=r.status;e.data=d;throw e;}
  return d;});});
}

/* ---------- theme ---------- */
function themeBtnText(){return "主题："+VccaReport.THEME_ZH[VccaReport.getTheme()];}
function cycleTheme(){var T=VccaReport.THEMES,t=VccaReport.getTheme();VccaReport.setTheme(T[(T.indexOf(t)+1)%3]);var b=$("#themeBtn");if(b)b.textContent=themeBtnText();}

/* ---------- shell ---------- */
function shell(){
 ROOT.innerHTML='<header class="top"><a class="brand" href="#/overview">vCCA 航季计划</a><nav class="nav" id="nav"></nav><span class="sp"></span>'+
  '<select id="seasonSel" title="航季"></select><a id="jobBadge" class="jobbadge hidden" href="#/jobs"></a>'+
  '<button class="btn" id="themeBtn"></button><span class="user" id="userName"></span><button class="btn" id="logoutBtn">退出</button></header>'+
  '<main id="page" class="page"></main><div id="toast"></div>';
 $("#nav").innerHTML=NAV.map(function(n){return '<a href="#/'+n[0]+'" data-nav="'+n[0]+'">'+n[1]+"</a>";}).join("");
 $("#themeBtn").textContent=themeBtnText();$("#themeBtn").onclick=cycleTheme;
 $("#userName").textContent=S.user;
 $("#logoutBtn").onclick=function(){api("POST","api/logout").catch(function(){}).then(function(){S.user=null;S.csrf=null;location.hash="#/login";});};
 var sel=$("#seasonSel");
 sel.innerHTML=S.seasons.map(function(s){return "<option value=\""+esc(s.name)+"\""+(s.name===S.season?" selected":"")+">"+esc(s.label||s.name)+" ["+esc(s.name)+"]</option>";}).join("");
 sel.onchange=function(){if(!confirmLeave()){sel.value=S.season;return;}S.season=sel.value;try{localStorage.setItem("vcca-season",S.season);}catch(e){}S.reports={};route(true);};
 pollBadge();if(!S.badgeTimer)S.badgeTimer=setInterval(pollBadge,4000);
}
function pollBadge(){if(!S.user)return;api("GET","api/jobs?limit=1",undefined,{noRedirect:true}).then(function(d){S.current=d.current;var b=$("#jobBadge");if(!b)return;
 if(d.current){var p=d.current.progress;b.textContent="运行中："+d.current.cmd+(p?" "+p.done+"/"+p.total:"");b.href="#/jobs/"+d.current.id;b.classList.remove("hidden");}else b.classList.add("hidden");}).catch(function(){});}
function loadSeasons(){return api("GET","api/seasons").then(function(d){S.seasons=d.seasons;var saved=null;try{saved=localStorage.getItem("vcca-season");}catch(e){}
 S.season=S.seasons.some(function(s){return s.name===saved;})?saved:(S.seasons.some(function(s){return s.name===S.season;})?S.season:d.default);});}

function confirmLeave(){if(S.dirty&&!confirm("配置有未保存的修改，确定离开？"))return false;S.dirty=false;return true;}
window.addEventListener("beforeunload",function(e){if(S.dirty){e.preventDefault();e.returnValue="";}});

/* ---------- router ---------- */
var lastHash=null;
function route(force){
 var h=location.hash||"#/overview";
 if(!force&&h===lastHash)return;
 if(S.dirty&&lastHash&&lastHash.indexOf("#/config")===0&&h.indexOf("#/config")!==0&&!confirmLeave()){history.replaceState(null,"",lastHash);return;}
 lastHash=h;
 var parts=h.replace(/^#\/?/,"").split("/"),page=parts[0]||"overview",arg=decodeURIComponent(parts.slice(1).join("/")||"");
 clearTimers();S.pageToken++;
 if(page==="login"||!S.user){return showLogin();}
 if(!$("#page"))shell();
 $$("#nav a").forEach(function(a){a.classList.toggle("on",a.getAttribute("data-nav")===page);});
 var P=$("#page");P.className="page";P.innerHTML='<div class="muted"><span class="spinner"></span>加载中…</div>';
 var fn={overview:pageOverview,jobs:pageJobs,results:pageResults,data:pageData,config:pageConfig,help:pageHelp}[page]||pageOverview;
 try{fn(P,arg,S.pageToken);}catch(e){P.innerHTML='<div class="msg err">'+esc(e.message)+"</div>";}
}
window.addEventListener("hashchange",function(){route(false);});
function alive(tok){return tok===S.pageToken;}
function fail(P,tok){return function(e){if(alive(tok)&&e.status!==401)P.innerHTML='<div class="msg err">'+esc(e.message)+errList(e.data&&e.data.errors)+"</div>";};}

/* ---------- login ---------- */
function showLogin(){
 ROOT.innerHTML='<div class="login"><div class="panel"><h1>vCCA 航季计划 · 登录</h1><form id="lf"><div class="field"><span>用户名</span><input type="text" id="lu" autocomplete="username" required></div>'+
  '<div class="field"><span>密码</span><input type="password" id="lp" autocomplete="current-password" required></div><div id="lerr"></div><button class="btn primary" type="submit">登录</button></form>'+
  '<p class="muted" style="font-size:12px">账号由管理员用 <code>vcca_plan.py web-user set &lt;名字&gt;</code> 创建。</p></div><div class="row end" style="margin-top:8px"><button class="btn" id="themeBtn"></button></div></div><div id="toast"></div>';
 $("#themeBtn").textContent=themeBtnText();$("#themeBtn").onclick=cycleTheme;
 $("#lu").focus();
 $("#lf").onsubmit=function(e){e.preventDefault();var btn=$("#lf button[type=submit]");btn.disabled=true;
  api("POST","api/login",{username:$("#lu").value,password:$("#lp").value},{noRedirect:true}).then(function(d){
   S.user=d.user;S.csrf=d.csrf;return loadSeasons();}).then(function(){var to=S.after&&S.after!=="#/login"?S.after:"#/overview";S.after=null;ROOT.innerHTML="";lastHash=null;
   if(location.hash===to)route(true);else location.hash=to;})
  .catch(function(err){btn.disabled=false;$("#lerr").innerHTML='<div class="msg err">'+esc(err.message)+"</div>";});};
}

/* ---------- jobs: start helper ---------- */
var CMDS=[
 {cmd:"status",name:"状态检查",desc:"配置、计划文件、VAMSYS、账号、缓存新鲜度",opts:[]},
 {cmd:"fetch",name:"增量抓取 FR24",desc:"按 P0/P1/P2 有效期抓取（访问 FR24）",net:true,opts:[["dry_run","bool","只预览（不发请求）"],["deep","bool","全机队注册号扫描 (--deep)"],["search","bool","刷新搜索索引 (--search)"],["flights","list","只抓这些班号，如 CA1234,CA5678"],["regs","list","只抓这些注册号，如 B-2091"],["boards","list","机场看板 ICAO，如 ZBAA"]]},
 {cmd:"build",name:"计算决策",desc:"→ output/<时间>/decisions.json",opts:[]},
 {cmd:"export",name:"导出",desc:"decisions.json → xlsx / CSV / report.html / summary",opts:[["run","run","运行目录（留空 = 最新）"]]},
 {cmd:"run",name:"全流程",desc:"fetch + build + export（访问 FR24）",net:true,opts:[["deep","bool","全机队扫描 (--deep)"]]},
 {cmd:"test",name:"运行测试",desc:"规则与格式测试",opts:[["verbose","bool","详细输出 (-v)"]]}
];
function startJob(cmd,opts){
 var c=CMDS.filter(function(x){return x.cmd===cmd;})[0];
 if(c&&c.net&&!(opts&&opts.dry_run)&&!confirm(c.name+" 会访问 FR24（可能需要几十分钟），确定开始？"))return Promise.resolve(null);
 return api("POST","api/jobs",{cmd:cmd,season:S.season,opts:opts||{}}).then(function(d){toast("已开始："+cmd);pollBadge();location.hash="#/jobs/"+d.job.id;return d.job;})
  .catch(function(e){if(e.status===409&&e.data&&e.data.job){toast(e.message,true);location.hash="#/jobs/"+e.data.job.id;}else toast(e.message,true);});
}

/* ---------- overview ---------- */
function pageOverview(P,arg,tok){
 Promise.all([api("GET","api/status?"+sq()),api("GET","api/runs?"+sq())]).then(function(r){if(!alive(tok))return;
  var d=r[0],runs=r[1].runs,h=['<div class="row"><h1 style="flex:1">概览 · '+esc(S.season)+'</h1><button class="btn" data-act="refresh">刷新</button></div>'];
  if(!d.ok){h.push('<div class="msg err"><b>配置错误</b>'+errList(d.errors)+'</div><p><a href="#/config">去配置页修改</a></p>');P.innerHTML=h.join("");bindRefresh();return;}
  var c=d.config,pl=d.plan,v=d.vamsys,cr=d.credentials,ca=d.cache,st=ca.stats;
  function tile(t,val,sub,cls){return '<div class="tile '+(cls||"")+'"><div class="t">'+esc(t)+'</div><div class="v">'+val+'</div><div class="s">'+(sub||"")+"</div></div>";}
  h.push('<div class="tiles">');
  h.push(tile("航季",esc(c.season_label),esc(c.start_date)+" ~ "+esc(c.end_date)+"（"+(c.dates_source==="config"?"配置":"由FD23/FD24推导")+"）· 计划时区 UTC"+(c.plan_tz_offset_min>=0?"+":"")+(c.plan_tz_offset_min/60)));
  h.push(tile("生效时间",esc(c.effective_time)+" UTC",esc(c.effective_time_mode)+" · 航司 "+esc(c.target_ops.join(","))+"（货运 "+esc(c.cargo_ops.join(","))+"）"));
  var nf=pl.files.filter(function(f){return f.exists;}).length;
  h.push(tile("CAAC 计划",pl.rows!=null?esc(pl.rows)+" 行":(nf+"/"+pl.files.length+" 个文件"),pl.rows!=null?"航段 "+pl.legs+" · 班号 "+pl.numbers+" · 航段组 "+pl.groups+" · 去回程缺失段 "+pl.gaps:(pl.error?esc(pl.error):"计划文件缺失"),pl.ok&&!pl.error?"":"bad"));
  h.push(tile("VAMSYS",v.ok?"revision "+esc(v.revision):(v.exists?"读取失败":"不存在"),v.ok?"航线 "+v.counts.routes+"（商业 "+v.counts.commercial_routes+"）· "+fmtTime(v.mtime)+(v.unrated.length?" · <b>未定级机场 "+v.unrated.length+"</b>":""):esc(v.error||v.path),v.ok?(v.unrated.length?"warn":""):"bad"));
  h.push(tile("FR24 账号",cr.source?"已找到":"未找到",esc(cr.source||"fetch 无法登录"),cr.source?"ok":"bad"));
  h.push(tile("FR24 缓存",st?esc(st.rows)+" 行":(ca.db.exists?"—":"未创建"),st?"最后抓取 "+esc(st.last_fetch_fmt||"无")+" · "+Object.keys(st.targets_by_priority).map(function(k){return k+" "+st.targets_by_priority[k];}).join(" · ")+" · 过期 "+st.stale_targets:esc(ca.stats_error||""),st&&st.stale_targets?"warn":""));
  h.push(tile("运行记录",esc(d.runs.count)+" 次",d.runs.latest?"最新 "+esc(d.runs.latest)+(d.runs.latest_has_decisions?"（含 decisions.json）":""):"无"));
  h.push("</div>");
  if(c.warnings.length)h.push('<div class="msg warn"><b>配置警告</b>'+errList(c.warnings)+"</div>");
  if(v.ok&&v.unrated.length)h.push('<div class="msg warn">未定级机场 '+v.unrated.length+"："+esc(v.unrated.join(", "))+'（在 <a href="#/config">rules/airport_grades.toml</a> 定级）</div>');
  h.push('<h2>快捷操作</h2><div class="row"><button class="btn" data-job="status">状态检查</button><button class="btn" data-job="fetch" data-opts=\'{"dry_run":true}\'>抓取预览</button>'+
   '<button class="btn" data-job="fetch">增量抓取</button><button class="btn" data-job="build">计算</button><button class="btn" data-job="export">导出最新</button><button class="btn primary" data-job="run">全流程 run</button></div>');
  var latest=runs.filter(function(x){return x.exported;})[0];
  if(latest){h.push('<h2>最新结果 · '+esc(latest.name)+' UTC <a class="btn" href="#/results/'+encodeURIComponent(latest.name)+'">查看结果</a></h2><div class="cols2"><div class="tbox"><table class="tbl"><thead><tr><th>分类</th><th>数量</th></tr></thead><tbody>');
   Object.keys(latest.counts||{}).forEach(function(k){h.push("<tr><td class=\"mono\">"+esc(k)+"</td><td>"+latest.counts[k]+"</td></tr>");});
   h.push('</tbody></table></div><div class="panel"><div class="kv2"><div>计算时间</div><div>'+esc(latest.generated_at||"—")+' UTC</div><div>导出时间</div><div>'+esc(latest.exported_at||"—")+' UTC</div><div>待人工复核</div><div>'+esc(latest.needs_review)+'</div><div>去向核账</div><div>'+(latest.closure_ok?'<span class="tag ok">通过</span>':'<span class="tag bad">不平</span>')+"</div></div><h3>CSV（按导入顺序）</h3><ol>"+
    latest.csv.map(function(n){return '<li><a href="'+fileUrl(latest.name,n)+'" download>'+esc(n)+"</a></li>";}).join("")+"</ol>"+(latest.csv.length?"":'<div class="muted">无需导入的变更</div>')+"</div></div>");}
  h.push('<h2>明细</h2><div class="cols2"><div class="panel"><h3>CAAC 计划文件</h3><div class="tbox"><table class="tbl"><tbody>'+pl.files.map(function(f){return "<tr><td>"+(f.exists?'<span class="tag ok">OK</span>':'<span class="tag bad">缺失</span>')+"</td><td class=\"wrap\">"+esc(f.name)+"</td><td>"+(f.exists?fmtSize(f.size):"")+"</td><td>"+(f.exists?fmtTime(f.mtime,false):"")+"</td></tr>";}).join("")+"</tbody></table></div>"+
   (pl.rows!=null?'<div class="kv2" style="margin-top:8px"><div>各文件行数</div><div>'+Object.keys(pl.rows_by_file).map(function(k){return esc(k)+" "+pl.rows_by_file[k];}).join("<br>")+"</div><div>航段（按航司）</div><div>"+pl.legs_by_op.map(function(x){return esc(x[0])+" "+x[1];}).join(", ")+"</div><div>单日行航段</div><div>"+pl.single_day+"</div><div>FD23/FD24 众数</div><div>"+esc(pl.fd23)+"/"+esc(pl.fd24)+"</div></div>":"")+"</div>");
  h.push('<div class="panel"><h3>VAMSYS</h3>'+(v.ok?'<div class="kv2"><div>文件</div><div>'+esc(v.path)+" ("+fmtSize(v.size)+", "+fmtTime(v.mtime)+")</div><div>工作区</div><div>"+esc(v.workspace_name)+" · revision "+v.revision+" · StorageRevision "+v.storage_revision+"</div><div>机场/机队/飞机/航路</div><div>"+[v.counts.airports,v.counts.fleets,v.counts.aircraft,v.counts.routings].join(" / ")+"</div><div>航线</div><div>"+v.counts.routes+"（商业 "+v.counts.commercial_routes+"，特殊 "+v.counts.special_routes+"，已设 End Date "+v.counts.routes_with_end_date+"）</div><div>按呼号前缀</div><div>"+Object.keys(v.counts.routes_by_op).map(function(k){return k+" "+v.counts.routes_by_op[k];}).join(", ")+"</div></div>":'<div class="msg err">'+esc(v.error||("数据库不存在: "+v.path))+"</div>")+"</div>");
  h.push('<div class="panel"><h3>FR24 账号（只检查是否存在，不显示内容）</h3><div class="tbox"><table class="tbl"><tbody>'+cr.checked.map(function(x){return "<tr><td>"+(x.user&&x.password?'<span class="tag ok">OK</span>':(x.user||x.password?'<span class="tag bad">不全</span>':'<span class="tag">无</span>'))+'</td><td class="wrap">'+esc(x.path)+"</td><td>账号 "+(x.user?"有":"无")+"，密码 "+(x.password?"有":"无")+"</td></tr>";}).join("")+"</tbody></table></div><p>将使用："+esc(CRED_SRC[cr.source]||cr.source||"（未找到账号）")+' · <a href="#/config">在配置页填写 / 修改</a></p></div>');
  h.push('<div class="panel"><h3>FR24 缓存</h3><div class="kv2"><div>目录</div><div>'+esc(ca.dir)+"</div><div>fr24_cache.db</div><div>"+(ca.db.exists?fmtSize(ca.db.size)+"，"+fmtTime(ca.db.mtime):"尚未创建")+"</div>"+(st?"<div>行数</div><div>"+st.rows+"</div><div>最后抓取</div><div>"+esc(st.last_fetch_fmt||"无")+"</div><div>目标</div><div>"+esc(JSON.stringify(st.targets_by_priority))+"</div><div>过期目标</div><div>"+st.stale_targets+"</div>":"")+"</div></div></div>");
  h.push('<p class="muted">时间按浏览器时区显示（'+tzLabel(new Date())+'）；运行文件夹名与报告内时间为 UTC。</p>');
  P.innerHTML=h.join("");bindRefresh();
  $$("[data-job]",P).forEach(function(b){b.onclick=function(){startJob(b.getAttribute("data-job"),JSON.parse(b.getAttribute("data-opts")||"{}"));};});
 }).catch(fail(P,tok));
 function bindRefresh(){var b=$("[data-act=refresh]",P);if(b)b.onclick=function(){P.innerHTML='<div class="muted"><span class="spinner"></span>刷新中…</div>';api("GET","api/status?"+sq({refresh:1})).then(function(){route(true);}).catch(fail(P,tok));};}
}

/* ---------- jobs ---------- */
function pageJobs(P,arg,tok){
 P.innerHTML='<h1>运行</h1><div class="jobs"><div><div class="cmdlist" id="cmds"></div></div><div><div id="jobview"></div><h2>任务历史</h2><div class="tbox" id="hist"></div></div></div>';
 var sel=S.selCmd||"status";
 function drawCmds(){
  $("#cmds").innerHTML=CMDS.map(function(c){var on=c.cmd===sel;
   return '<div class="cmd'+(on?" on":"")+'" data-cmd="'+c.cmd+'"><div class="nm">'+esc(c.name)+' <span class="muted mono">'+c.cmd+'</span></div><div class="ds">'+esc(c.desc)+"</div>"+
    (on?'<div class="opts">'+c.opts.map(function(o){return o[1]==="bool"?'<label class="chk"><input type="checkbox" data-o="'+o[0]+'"'+(o[0]==="dry_run"?" checked":"")+"> "+esc(o[2])+"</label>":
     '<label class="field"><span>'+esc(o[2])+'</span><input type="text" data-o="'+o[0]+'" data-k="'+o[1]+'" autocomplete="off"></label>';}).join("")+
     '<div class="row"><button class="btn primary" data-start>开始</button><span class="muted">航季 '+esc(S.season)+"</span></div></div>":"")+"</div>";}).join("");
  $$("#cmds .cmd .nm").forEach(function(n){n.onclick=function(){sel=S.selCmd=n.parentNode.getAttribute("data-cmd");drawCmds();};});
  var sb=$("#cmds [data-start]");if(sb)sb.onclick=function(){var o={};$$("#cmds .cmd.on [data-o]").forEach(function(i){var k=i.getAttribute("data-o");if(i.type==="checkbox")o[k]=i.checked;else if(i.value.trim())o[k]=i.value.trim();});startJob(sel,o);};
 }
 drawCmds();
 function drawHist(){api("GET","api/jobs?limit=30").then(function(d){if(!alive(tok))return;
  $("#hist").innerHTML=d.jobs.length?'<table class="tbl"><thead><tr><th>开始</th><th>命令</th><th>航季</th><th>状态</th><th>用时</th><th>发起人</th><th>结果</th></tr></thead><tbody>'+d.jobs.map(function(j){
   return '<tr class="click" data-id="'+esc(j.id)+'"><td>'+fmtTime(j.started,false)+'</td><td class="mono">'+esc((j.args||[]).filter(function(a,i,arr){return !(a==="--season"||arr[i-1]==="--season");}).join(" "))+"</td><td>"+esc(j.season||"")+'</td><td><span class="st '+j.status+'">'+stZh(j.status)+(j.rc!=null&&j.status!=="done"?" ("+j.rc+")":"")+"</span></td><td>"+fmtDur(j.elapsed_s)+"</td><td>"+esc(j.user)+"</td><td>"+(j.run_dir?'<a href="#/results/'+encodeURIComponent(j.run_dir)+'">'+esc(j.run_dir)+"</a>":"")+"</td></tr>";}).join("")+"</tbody></table>":'<div class="muted" style="padding:10px">还没有任务</div>';
  $$("#hist tr[data-id]").forEach(function(tr){tr.onclick=function(e){if(e.target.closest("a"))return;location.hash="#/jobs/"+tr.getAttribute("data-id");};});
  if(!arg&&d.current&&!$("#jobview").getAttribute("data-id"))viewJob(d.current.id);
  if(!arg&&!d.current&&!$("#jobview").innerHTML)$("#jobview").innerHTML='<div class="panel muted">选择左侧命令开始任务；任务日志会显示在这里。</div>';
 }).catch(fail($("#hist"),tok));}
 drawHist();
 if(arg)viewJob(arg);
 function viewJob(id){
  var V=$("#jobview"),since=0,autoscroll=true,polling=false,done=false;V.setAttribute("data-id",id);
  V.innerHTML='<div class="panel"><div class="row"><b id="jtitle" class="mono" style="flex:1"></b><span id="jst"></span><button class="btn danger hidden" id="jcancel">中断</button><a class="btn hidden" id="jres">查看结果</a></div>'+
   '<div class="muted" id="jmeta"></div><div id="jprog" class="hidden"><div class="prog"><div></div></div><div class="muted" id="jprogt"></div></div>'+
   '<div class="row" style="margin:6px 0"><label class="chk"><input type="checkbox" id="jauto" checked> 自动滚动</label><a class="btn" id="jdl">下载日志</a></div><pre class="log" id="jlog"></pre></div>';
  var L=$("#jlog");$("#jauto").onchange=function(){autoscroll=this.checked;};
  $("#jdl").onclick=function(){var b=new Blob([L.textContent],{type:"text/plain;charset=utf-8"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="job-"+id+".log";a.click();setTimeout(function(){URL.revokeObjectURL(a.href);},1000);};
  $("#jcancel").onclick=function(){if(!confirm("中断这个任务？fetch 会保存已抓取的进度。"))return;api("POST","api/jobs/"+encodeURIComponent(id)+"/cancel").then(function(){toast("已请求中断");}).catch(function(e){toast(e.message,true);});};
  function poll(){if(polling||done||!alive(tok))return;polling=true;
   api("GET","api/jobs/"+encodeURIComponent(id)+"?since="+since).then(function(d){polling=false;if(!alive(tok))return;var j=d.job;
    if(d.text){L.appendChild(document.createTextNode(d.text));if(autoscroll)L.scrollTop=L.scrollHeight;}since=d.next;
    $("#jtitle").textContent="vcca_plan.py "+(j.args||[]).join(" ");
    $("#jst").innerHTML='<span class="st '+j.status+'">'+stZh(j.status)+(j.rc!=null?" · 退出码 "+j.rc:"")+"</span>";
    $("#jmeta").textContent="开始 "+fmtTime(j.started)+" · 用时 "+fmtDur(j.elapsed_s)+" · 发起人 "+(j.user||"—")+(j.note?" · "+j.note:"");
    var running=j.status==="running";$("#jcancel").classList.toggle("hidden",!running||j.cancel_requested);
    if(j.progress&&running){var p=j.progress;$("#jprog").classList.remove("hidden");$("#jprog .prog>div").style.width=(100*p.done/Math.max(1,p.total)).toFixed(1)+"%";
     $("#jprogt").textContent=p.done+" / "+p.total+" · 已用 "+p.elapsed+" · 预计剩余 "+p.eta;}else $("#jprog").classList.add("hidden");
    if(j.run_dir){var a=$("#jres");a.href="#/results/"+encodeURIComponent(j.run_dir);a.classList.toggle("hidden",running);}
    if(!running&&d.text===""){done=true;drawHist();pollBadge();}
   }).catch(function(e){polling=false;if(e.status===404){done=true;V.innerHTML='<div class="msg err">没有这个任务</div>';}});}
  poll();setTimer(poll,1000);
 }
}
function stZh(s){return {running:"运行中",done:"完成",failed:"失败",cancelled:"已中断",interrupted:"中断（服务重启）"}[s]||s;}

/* ---------- results ---------- */
function pageResults(P,arg,tok){if(arg)return pageRun(P,arg,tok);
 api("GET","api/runs?"+sq()).then(function(d){if(!alive(tok))return;
  var h=['<h1>结果 · '+esc(S.season)+'</h1><p class="muted">运行文件夹名是计算时的 UTC 时间。点一行打开结果。</p>'];
  if(!d.runs.length){h.push('<div class="panel muted">还没有运行记录。到 <a href="#/jobs">运行</a> 页执行 build / run。</div>');P.innerHTML=h.join("");return;}
  h.push('<div class="tbox"><table class="tbl"><thead><tr><th>运行 (UTC)</th><th>导出时间 (UTC)</th><th>新增</th><th>调整</th><th>停用</th><th>一致</th><th>CSV 行</th><th>待复核</th><th>核账</th><th>文件</th></tr></thead><tbody>');
  d.runs.forEach(function(r){var c=r.counts||{},sum=function(pre){return Object.keys(c).filter(function(k){return k.indexOf(pre)===0;}).reduce(function(a,k){return a+c[k];},0);};
   var csvN=r.csv_counts?Object.keys(r.csv_counts).reduce(function(a,k){return a+r.csv_counts[k];},0):"";
   h.push('<tr class="click" data-run="'+esc(r.name)+'"><td class="mono"><b>'+esc(r.name)+"</b></td><td>"+esc(r.exported_at||(r.has_decisions?"未导出":"—"))+"</td><td>"+(r.counts?sum("CREATE"):"")+"</td><td>"+(r.counts?(c.UPDATE||0):"")+"</td><td>"+(r.counts?sum("RETIRE"):"")+"</td><td>"+(r.counts?sum("KEEP"):"")+"</td><td>"+csvN+"</td><td>"+(r.needs_review!=null?r.needs_review:"")+"</td><td>"+
    (r.closure_ok==null?"":r.closure_ok?'<span class="tag ok">通过</span>':'<span class="tag bad">不平</span>')+"</td><td>"+r.files+(r.has_decisions?"":' <span class="tag">无 decisions.json</span>')+"</td></tr>");});
  h.push("</tbody></table></div>");P.innerHTML=h.join("");
  $$("tr[data-run]",P).forEach(function(tr){tr.onclick=function(){location.hash="#/results/"+encodeURIComponent(tr.getAttribute("data-run"));};});
 }).catch(fail(P,tok));
}
function pageRun(P,run,tok){
 P.className="page wide";
 var st=S.reports[run]||(S.reports[run]={prev:"auto",tab:null});
 api("GET","api/runs/"+encodeURIComponent(run)+"?"+sq()).then(function(info){if(!alive(tok))return;
  var csv=info.files.filter(function(f){return f.kind==="csv";}),other=info.files.filter(function(f){return f.kind!=="csv";}),s=info.summary||{};
  var h=['<div class="row" style="margin-bottom:8px"><a href="#/results">← 全部运行</a><h1 style="margin:0;flex:1">'+esc(run)+' <span class="muted" style="font-size:13px;font-weight:400">UTC · '+esc(S.season)+"</span></h1></div>",'<div class="runpage"><aside class="side panel">'];
  h.push("<h3>FlightOpsDesk 导入顺序</h3>"+(csv.length?'<ul class="flist">'+csv.map(function(f,i){return '<li><span><span class="ord">'+(f.name.indexOf("待复核")>=0?"?":(i+1)+".")+'</span><a href="'+fileUrl(run,f.name)+'" download>'+esc(f.name)+'</a></span><span class="n">'+fmtSize(f.size)+"</span></li>";}).join("")+"</ul>"+
   (csv.some(function(f){return f.name.indexOf("待复核")>=0;})?'<div class="muted" style="font-size:12px">"?" = 待复核，确认后再并入主 CSV</div>':""):'<div class="muted">无 CSV</div>'));
  h.push('<h3>其他文件</h3><ul class="flist">'+other.map(function(f){return '<li><a href="'+fileUrl(run,f.name)+'" download>'+esc(f.name)+'</a><span class="n">'+fmtSize(f.size)+"</span></li>";}).join("")+"</ul>");
  h.push('<div class="row" style="margin-top:10px"><a class="btn" href="api/runs/'+encodeURIComponent(run)+"/zip?"+sq()+'" download>下载全部 (zip)</a>'+(info.files.some(function(f){return f.name==="decisions.json";})?'<button class="btn" id="reexport">重新导出</button>':"")+"</div>");
  if(s.warnings&&s.warnings.length)h.push('<div class="msg warn">'+errList(s.warnings)+"</div>");
  if(info.log_tail)h.push('<details style="margin-top:10px"><summary>run.log</summary><pre class="preview">'+esc(info.log_tail)+"</pre></details>");
  h.push('</aside><div id="rep"><div class="muted"><span class="spinner"></span>加载结果…</div></div></div>');
  P.innerHTML=h.join("");
  var rb=$("#reexport");if(rb)rb.onclick=function(){if(confirm("重新导出 "+run+"？（覆盖该目录的 xlsx / CSV / report.html）"))startJob("export",{run:run});};
  if(!info.files.some(function(f){return f.name==="decisions.json";})){$("#rep").innerHTML='<div class="msg warn">这次运行没有 decisions.json，无法浏览结果。</div>';return;}
  loadReport();
  function loadReport(){
   var R=$("#rep");R.innerHTML='<div class="muted"><span class="spinner"></span>加载结果…</div>';
   api("GET","api/runs/"+encodeURIComponent(run)+"/report?"+sq({prev:st.prev})).then(function(payload){if(!alive(tok))return;
    if(S.mounted)try{S.mounted.destroy();}catch(e){}
    S.mounted=VccaReport.mount(R,payload,{header:false,tab:st.tab||"all",onTab:function(t){st.tab=t;},
     fileUrl:function(n){return fileUrl(run,n);},diffRuns:info.earlier_runs,
     onDiffChange:function(p){st.prev=p;st.tab="diff";loadReport();}});
   }).catch(fail(R,tok));}
 }).catch(fail(P,tok));
}

/* ---------- data ---------- */
function upload(url,file,onProgress){return new Promise(function(resolve,reject){
 var x=new XMLHttpRequest();x.open("POST",url);x.setRequestHeader("X-VCCA-CSRF",S.csrf||"");x.setRequestHeader("X-Filename",encodeURIComponent(file.name));
 x.setRequestHeader("Content-Type","application/octet-stream");
 x.upload.onprogress=function(e){if(e.lengthComputable&&onProgress)onProgress(e.loaded/e.total);};
 x.onload=function(){var d;try{d=JSON.parse(x.responseText);}catch(e){d={error:x.responseText};}if(x.status>=200&&x.status<300)resolve(d);else{var er=new Error(d.error||("HTTP "+x.status));er.status=x.status;er.data=d;reject(er);}};
 x.onerror=function(){reject(new Error("网络错误"));};x.send(file);});}
function newSeasonHtml(){return ('<div class="panel" style="margin-top:12px"><h2 style="margin-top:0">新建航季</h2><p class="muted">在 $VCCA_HOME/seasons 下新建航季文件夹并写一个精简的 season.toml（只有 [season]；全站设置在 settings.toml），或复制所选航季的 season.toml；之后上传计划文件，并在配置页修改 [season] 的 name / 日期。</p><div class="row"><input type="text" id="nsname" placeholder="文件夹名，如 2027-1"><label>season.toml <select class="in" id="nsfrom"><option value="">新建（模板）</option>'+
   S.seasons.map(function(s){return '<option value="'+esc(s.name)+'">复制自 '+esc(s.name)+"</option>";}).join("")+'</select></label><button class="btn" id="nscreate">新建</button></div><div id="rns"></div></div>');}
function bindNewSeason(){
  $("#nscreate").onclick=function(){var n=$("#nsname").value.trim();if(!n){toast("填写文件夹名",true);return;}
   api("POST","api/seasons",{name:n,copy_from:$("#nsfrom").value}).then(function(r){$("#rns").innerHTML='<div class="msg ok">已新建 '+esc(r.season)+"。"+esc(r.next)+"</div>";
    return loadSeasons().then(function(){S.season=r.season;try{localStorage.setItem("vcca-season",S.season);}catch(e){}ROOT.innerHTML="";route(true);});
   }).catch(function(e){$("#rns").innerHTML='<div class="msg err">'+esc(e.message)+"</div>";});};
}
function pageData(P,arg,tok){
 if(!S.season){P.innerHTML='<h1>数据</h1><div class="msg warn">$VCCA_HOME/seasons 下还没有航季。先新建一个。</div>'+newSeasonHtml();bindNewSeason();return;}
 api("GET","api/data?"+sq()).then(function(d){if(!alive(tok))return;var v=d.vamsys,c=d.cache;
  var h=['<h1>数据 · '+esc(S.season)+"</h1>"];
  if(S.flash){h.push(S.flash);S.flash=null;}
  if(d.busy)h.push('<div class="msg warn">有任务正在运行，完成后才能上传或切换工作区。</div>');
  var wsSel=v.workspaces.length?'<h3>工作区</h3><div class="row"><select class="in" id="wsel">'+v.workspaces.map(function(w){return '<option value="'+esc(w.id)+'"'+(w.id===(v.workspace_id&&v.workspaces.some(function(x){return x.id===v.workspace_id;})?v.workspace_id:v.selected)?" selected":"")+">"+esc(w.name)+" · 航线 "+w.routes+" · rev "+esc(w.revision)+"</option>";}).join("")+
   '</select><button class="btn primary" id="wsave">保存到 settings.toml</button></div><p class="muted mono" id="wid"></p><div id="rws"></div>':"";
  h.push('<div class="cols2"><div class="panel"><h2 style="margin-top:0">VAMSYS 数据库</h2><div class="kv2"><div>文件</div><div class="mono">'+esc(v.path)+"</div><div>状态</div><div>"+(v.exists?fmtSize(v.size)+" · "+fmtTime(v.mtime):'<span class="tag bad">不存在</span>')+"</div>"+
   (v.revision!=null?"<div>revision</div><div>"+v.revision+"（StorageRevision "+esc(v.storage_revision)+"）</div>":"")+"<div>当前工作区</div><div class=\"mono\">"+(v.workspace_id?esc(v.workspace_id)+(v.workspace_source==="season"?' <span class="tag">航季覆盖</span>':""):'<span class="tag bad">未设置</span>')+"</div>"+(v.error?"<div>读取</div><div>"+esc(v.error)+"</div>":"")+"</div>"+wsSel+
   '<h3>上传新的 workspaces.db</h3><p class="muted">上传后只保留本工具用到的三张表（workspaces、workspace_revisions、resource_rows；不保存 secrets 等其他内容）并压缩、校验，再把旧文件改名备份为 <span class="mono">workspaces.&lt;时间&gt;.db</span>（保留最近 5 份）并替换。上限 '+d.max_upload_mb+' MB。</p>'+
   '<div class="row"><input type="file" id="fdb" accept=".db,.sqlite,application/octet-stream"><button class="btn primary" id="updb">上传</button></div><div class="prog hidden" id="pdb"><div></div></div><div id="rdb"></div>'+
   (v.backups.length?"<h3>备份</h3><ul>"+v.backups.map(function(b){return '<li class="mono">'+esc(b.name)+' <span class="muted">'+fmtSize(b.size)+"</span></li>";}).join("")+"</ul>":"")+"</div>");
  h.push('<div class="panel"><h2 style="margin-top:0">CAAC 计划文件</h2><p class="muted mono">'+esc(d.season_dir)+'</p><div class="tbox"><table class="tbl"><thead><tr><th>文件</th><th>plan_files</th><th>大小</th><th>修改时间</th></tr></thead><tbody>'+
   d.plan_files.map(function(f){return '<tr><td class="wrap">'+esc(f.name)+"</td><td>"+(f.configured?'<span class="tag ok">已配置</span>':'<span class="tag">未配置</span>')+"</td><td>"+(f.exists?fmtSize(f.size):'<span class="tag bad">缺失</span>')+"</td><td>"+(f.exists?fmtTime(f.mtime,false):"")+"</td></tr>";}).join("")+"</tbody></table></div>"+
   '<h3>上传计划 .xls</h3><p class="muted">Excel 97-2003 格式（CAAC 的"（三字码）.xls"）。同名文件会先备份到 <span class="mono">.plan_history/</span>。</p><div class="row"><input type="file" id="fxls" accept=".xls,application/vnd.ms-excel"><label class="chk"><input type="checkbox" id="fadd" checked> 加入 season.toml 的 plan_files</label><button class="btn primary" id="upxls">上传</button></div><div class="prog hidden" id="pxls"><div></div></div><div id="rxls"></div></div></div>');
  h.push('<div class="panel" style="margin-top:12px"><h2 style="margin-top:0">FR24 缓存</h2><div class="kv2"><div>文件</div><div class="mono">'+esc(c.path)+"</div><div>状态</div><div>"+(c.exists?fmtSize(c.size)+" · "+fmtTime(c.mtime):'<span class="tag">尚未创建（首次 fetch 时创建）</span>')+"</div></div>"+
   '<h3>导入 fr24_cache.db</h3><p class="muted">用另一台机器上的缓存替换（例如迁移服务器）。会先检查是 fr24_cache.db 的结构，再把当前文件备份为 <span class="mono">fr24_cache.&lt;时间&gt;.db</span>（保留最近 3 份）并替换。</p>'+
   '<div class="row"><input type="file" id="fcache" accept=".db,application/octet-stream"><button class="btn primary" id="upcache">导入</button></div><div class="prog hidden" id="pcache"><div></div></div><div id="rcache"></div>'+
   (c.backups.length?"<h3>备份</h3><ul>"+c.backups.map(function(b){return '<li class="mono">'+esc(b.name)+' <span class="muted">'+fmtSize(b.size)+"</span></li>";}).join("")+"</ul>":"")+"</div>");
  h.push(newSeasonHtml());
  h.push('<p class="muted">上传或新建后，建议到 <a href="#/jobs">运行</a> 页执行 status 检查，再 build / run。</p>');
  P.innerHTML=h.join("");
  var SKIP=["ok","next","config","backup","path","name","workspaces","selected","configured","needs_selection","dropped_tables"];
  function doUp(inp,btn,bar,res,url,name){$(btn).onclick=function(){var f=$(inp).files[0];if(!f){toast("先选择文件",true);return;}
   if(f.size>d.max_upload_mb*1048576){toast("文件超过上限",true);return;}
   if(!confirm("上传 "+f.name+"（"+fmtSize(f.size)+"）并替换"+name+"？"))return;
   $(btn).disabled=true;$(bar).classList.remove("hidden");$(res).innerHTML='<div class="muted"><span class="spinner"></span>上传并校验中…</div>';
   upload(url(),f,function(p){$(bar+">div").style.width=(p*100).toFixed(0)+"%";}).then(function(r){
    $(res).innerHTML='<div class="msg ok">已更新'+(r.backup?"（旧文件备份为 "+esc(r.backup)+"）":"")+"<br>"+esc(Object.keys(r).filter(function(k){return SKIP.indexOf(k)<0;}).map(function(k){return k+": "+(Array.isArray(r[k])?r[k].join(","):r[k]);}).join(" · "))+
     (r.dropped_tables&&r.dropped_tables.length?"<br>未保存的表："+esc(r.dropped_tables.join(", ")):"")+
     (r.config&&r.config.ok===false?'</div><div class="msg warn">plan_files 未更新：'+errList(r.config.errors):"")+"<br><b>"+esc(r.next||"")+"</b></div>";
    toast("上传完成");S.flash=$(res).innerHTML;if(alive(tok))route(true);
   }).catch(function(e){$(btn).disabled=false;$(bar).classList.add("hidden");$(res).innerHTML='<div class="msg err">'+esc(e.message)+errList(e.data&&e.data.errors)+"</div>";});};}
  doUp("#fdb","#updb","#pdb","#rdb",function(){return "api/upload/vamsys?"+sq();}," VAMSYS 数据库");
  doUp("#fxls","#upxls","#pxls","#rxls",function(){return "api/upload/plan?"+sq({add:$("#fadd").checked?"1":""});},"/新增计划文件");
  doUp("#fcache","#upcache","#pcache","#rcache",function(){return "api/upload/fr24cache?"+sq();}," FR24 缓存");
  if($("#wsel")){var showId=function(){$("#wid").textContent=$("#wsel").value;};showId();$("#wsel").onchange=showId;
   if(v.workspace_id!==$("#wsel").value)$("#rws").innerHTML='<div class="msg warn">'+(v.workspace_id?"当前配置的工作区不在数据库里。":"还没有选择工作区。")+"确认下拉框里的工作区后点保存。</div>";
   $("#wsave").onclick=function(){if(d.busy){toast("有任务正在运行",true);return;}$("#wsave").disabled=true;
    api("POST","api/vamsys/workspace",{season:S.season,workspace_id:$("#wsel").value}).then(function(r){
     $("#rws").innerHTML='<div class="msg ok">'+(r.saved===false?"settings.toml 已经是这个工作区":"已写入 settings.toml [vamsys] workspace_id"+(r.backup?"（旧版本备份为 "+esc(r.backup)+"）":""))+"</div>"+(r.warnings&&r.warnings.length?'<div class="msg warn">'+errList(r.warnings)+"</div>":"");
     toast("工作区已保存");S.flash=$("#rws").innerHTML;if(alive(tok))route(true);
    }).catch(function(e){$("#wsave").disabled=false;$("#rws").innerHTML='<div class="msg err">'+esc(e.message)+errList(e.data&&e.data.errors)+"</div>";});};}
  bindNewSeason();
 }).catch(fail(P,tok));
}

/* ---------- config ---------- */
/* Settings form: [section, key, label, type, hint]. Types: text date int num codes ints pair pairs efftime ci ws. */
var CFG_FORM=[
 {t:"本航季",note:"只对当前航季生效（写入 season.toml）",f:[
  ["season","name","名称","text","留空 = 用文件夹名"],
  ["season","start_date","开始日期","date","留空 = 由计划文件 FD23 众数推导"],
  ["season","end_date","结束日期","date","留空 = 由计划文件 FD24 众数推导"],
  ["season","plan_tz_offset_min","计划时刻时区（分钟）","int","CAAC 计划为北京时间 = 480"]]},
 {t:"导出",f:[
  ["export","effective_time","生效时间","efftime","停用航线的 End Date、新建航线的 Start Date"],
  ["export","default_cost_index","默认 Cost Index","ci","新建航线使用；每个目标航司都要填"]]},
 {t:"VAMSYS",f:[
  ["vamsys","workspace_id","工作区","ws",""],
  ["vamsys","collection","Collection","int",""]]},
 {t:"航司",f:[
  ["operators","target","目标航司","codes","ICAO 三字码，逗号分隔"],
  ["operators","cargo","货运航司","codes","必须是目标航司之一"],
  ["operators","number_prefix","航班号前缀","text","呼号 CCA1234 → 航班号 CA1234"]]},
 {t:"FR24 抓取",f:[
  ["fetch","rate_per_sec","每秒请求数","num",""],
  ["fetch","ttl_uncertain_h","不确定航班重抓间隔（小时）","int","P1 优先级"],
  ["fetch","ttl_stable_h","稳定航班重抓间隔（小时）","int","P2 优先级"],
  ["fetch","deep_scan_every_days","全机队注册号扫描间隔（天）","int",""],
  ["fetch","reg_drop_after_days","注册号多少天未出现即移出","int",""],
  ["fetch","max_consecutive_failures","连续失败多少次停止抓取","int",""]]},
 {t:"阈值",fold:true,note:"成对的值是 起飞/落地 容差（分钟）。改之前可先看结果页里每条阈值影响了多少航线。",f:[
  ["thresholds","recent_window_days","近期窗口（天）","int",""],
  ["thresholds","time_tolerance_min","时刻视为一致（分钟）","int",""],
  ["thresholds","bank_merge","时段聚类容差","pairs","满足任意一组即合并"],
  ["thresholds","plan_attach","计划子段归入 FR24 时段","pair",""],
  ["thresholds","plan_calibrate","计划时段校准到最近 FR24 时段","pair",""],
  ["thresholds","vamsys_bank_match","现行 ID 吻合 FR24 时段","pair",""],
  ["thresholds","micro_split_merge","单时段微拆分合并容差","pair",""],
  ["thresholds","extra_slot_separation","离所有计划时段多远算缺失时段","pair",""],
  ["thresholds","time_override_min_acts","FR24 覆盖计划时刻所需实飞次数","int",""],
  ["thresholds","weekday_add_min_weeks","增补星期所需周数","int",""],
  ["thresholds","weekday_drop_min_weeks","删除星期所需周数","int",""],
  ["thresholds","weekday_drop_min_coverage","删除星期：其他计划星期最低执行率","num","0–1"],
  ["thresholds","fr24_ac_min_share","机型计入宽/窄体的最低占比","num","0–1；0 = 任意 2 班都算"],
  ["thresholds","single_day_promote_acts","单日行升格所需实飞次数","int",""],
  ["thresholds","carrier_change_min_acts","判定换承运所需近期实飞次数","int",""],
  ["thresholds","fr24_add_min_acts","计划外补漏新增所需实飞次数","int",""],
  ["thresholds","fr24_add_min_weeks","计划外补漏新增所需周数","int",""],
  ["thresholds","rotation_gap_add_min_acts","轮换缺口补漏所需实飞次数","int",""],
  ["thresholds","extra_slot_min_acts","缺失时段所需实飞次数","int",""],
  ["thresholds","bank_supersede_days","换季中途调时（天）","int",""],
  ["thresholds","slot_pair_day_penalty","配对星期差异罚分","int",""],
  ["thresholds","dst_shift_min","整点偏移候选（分钟）","ints","夏令时等整小时偏移，逗号分隔"],
  ["thresholds","bank_weight_actual","时段权重：实飞","int",""],
  ["thresholds","bank_weight_recent","时段权重：近期","int",""],
  ["thresholds","real_time_round_min","实际时刻取整（分钟）","int",""],
  ["thresholds","never_flown_lookback_days","判定\"从未飞过\"的回看天数","int",""]]}
];
var EFFTIME_AUTO="next_bjt_midnight";
function canon(v){if(Array.isArray(v))return "["+v.map(canon).join(",")+"]";
 if(v&&typeof v==="object")return "{"+Object.keys(v).sort().map(function(k){return JSON.stringify(k)+":"+canon(v[k]);}).join(",")+"}";
 return JSON.stringify(v);}
function isEmptyVal(v){return v===""||v==null||(typeof v==="object"&&Object.keys(v).length===0);}
function cfgShow(v,ty){
 if(v===""||v==null)return "（空）";
 if(ty==="pair")return v[0]+"/"+v[1];
 if(ty==="pairs")return v.map(function(p){return p[0]+"/"+p[1];}).join(", ");
 if(ty==="efftime")return v===EFFTIME_AUTO?"次日北京时间 0 点":v+" UTC";
 if(Array.isArray(v))return v.join(", ");
 if(typeof v==="object"){var k=Object.keys(v);return k.length?k.map(function(x){return x+" "+v[x];}).join(" · "):"（空）";}
 return String(v);}

function pageConfig(P,arg,tok){
 api("GET","api/config?"+sq()).then(function(d){if(!alive(tok))return;
  var files=d.files;
  var cur=files.filter(function(f){return f.key===S.cfgKey;})[0];
  if(!cur){S.cfgKey="form";pageCfgForm(P,d);}else pageCfgFile(P,d,cur);
 }).catch(fail(P,tok));
}
function cfgHead(d){var h='<div id="cfgpage"><h1>配置 · '+esc(S.season)+'</h1><div class="ftabs" id="ftabs"></div>';
 if(S.flash){h+=S.flash;S.flash=null;}
 if(d.busy)h+='<div class="msg warn">有任务正在运行，暂时不能保存。</div>';
 return h;}
function cfgTabs(files,key){
 $("#ftabs").innerHTML='<button data-k="form"'+(key==="form"?' class="on"':"")+">设置</button>"+'<span class="ftsep">TOML 文件</span>'+
  files.map(function(f){return '<button data-k="'+f.key+'" title="'+esc(f.path)+'"'+(f.key===key?' class="on"':"")+">"+esc(f.label)+"</button>";}).join("");
 $$("#ftabs button").forEach(function(b){b.onclick=function(){if(b.classList.contains("on")||!confirmLeave())return;S.cfgKey=b.getAttribute("data-k");route(true);};});
}

/* ----- settings form ----- */
function pageCfgForm(P,d){
 var e=d.effective||{rows:[],warnings:[]},rows={},F=[];
 e.rows.forEach(function(r){rows[r.section+"."+r.key]=r;});
 var mt={};d.files.forEach(function(f){mt[f.key]=f.mtime_ns;});
 CFG_FORM.forEach(function(g){g.f.forEach(function(f){var r=rows[f[0]+"."+f[1]];if(!r)return;
  f.i=F.length;f.cur=r.value;f.def=r.default;f.fb=r.fallback!==undefined?r.fallback:r.default;f.src=r.source;f.reset=false;F.push(f);});});
 function field(sect,key){return F.filter(function(f){return f[0]===sect&&f[1]===key;})[0];}
 var h=cfgHead(d);
 if(e.warnings&&e.warnings.length)h+='<div class="msg warn"><b>警告</b>'+errList(e.warnings)+"</div>";
 h+='<p class="muted">每项最终的值 = 代码默认 → 全站 settings.toml → 本航季 season.toml（后者覆盖前者）。改动会写回它当前所在的文件；改回默认值即从文件里删掉该键。'+
  '计划文件、机场别名/名称、机场等级和机队规则请在上方的 TOML 文件标签里改。</p>';
 CFG_FORM.forEach(function(g,gi){var fs=g.f.filter(function(f){return f.i!=null;});if(!fs.length)return;
  var nChg=fs.filter(function(f){return f.src!=="default";}).length;
  var head='<h2>'+esc(g.t)+(g.note?' <span class="muted fnote">'+esc(g.note)+"</span>":"")+"</h2>";
  var body=fs.map(rowHtml).join("");
  if(g.fold)h+='<details class="panel fgroup" data-g="'+gi+'"'+(S.cfgOpen&&S.cfgOpen[gi]?" open":"")+'><summary><b>'+esc(g.t)+'</b> <span class="muted">'+fs.length+" 项"+(nChg?"，"+nChg+" 项不是默认值":"")+"</span></summary>"+
   (g.note?'<p class="muted fnote">'+esc(g.note)+"</p>":"")+body+"</details>";
  else h+='<section class="panel fgroup">'+head+body+"</section>";
  if(g.t==="FR24 抓取")h+='<section class="panel fgroup" id="fr24acct"></section>';});
 h+='<details class="panel fgroup" id="effall"><summary><b>全部生效值</b> <span class="muted">只读，包括表单里没有的项</span></summary>'+effTable(e)+"</details>";
 h+='<div id="fres"></div><div class="savebar"><span id="fcount" class="muted">没有修改</span><span class="sp"></span><button class="btn" id="fundo" hidden>撤销</button><button class="btn primary" id="fsave" disabled>保存</button></div></div>';
 P.innerHTML=h;cfgTabs(d.files,"form");
 if(d.fr24)drawFr24Account($("#fr24acct"),d.fr24,d.busy);
 $$("details[data-g]",P).forEach(function(x){x.ontoggle=function(){S.cfgOpen=S.cfgOpen||{};S.cfgOpen[x.getAttribute("data-g")]=x.open;};});
 if($("#effonly"))$("#effonly").onchange=function(){var only=this.checked;$$("#efftbl tr.same").forEach(function(tr){tr.style.display=only?"none":"";});};

 function ciOps(){var t=field("operators","target"),ops=t?(t.cur||[]).slice():[];
  if(t&&$('[data-in="'+t.i+'"]',P)){var r=readField(t);if(!r.err)ops=r.v;}
  var ci=field("export","default_cost_index"),have=ci?Object.keys(ci.cur||{}):[];if(!ops.length)return have;
  return have.filter(function(k){return ops.indexOf(k)>=0;}).concat(ops.filter(function(k){return have.indexOf(k)<0;}));}   /* keep the file's key order */
 function inputHtml(f,v){var ty=f[3],id='data-in="'+f.i+'"';
  switch(ty){
  case "text":return '<input type="text" '+id+' value="'+esc(v)+'">';
  case "date":return '<input type="date" '+id+' value="'+esc(v)+'">';
  case "int":case "num":return '<input type="number" '+id+' step="'+(ty==="int"?"1":"any")+'" value="'+esc(v)+'">';
  case "codes":case "ints":return '<input type="text" '+id+' value="'+esc((v||[]).join(", "))+'">';
  case "pairs":return '<input type="text" '+id+' value="'+esc(cfgShow(v,ty))+'" placeholder="45/60, 20/75">';
  case "pair":return '<span class="pair"><span class="muted">起飞 ±</span><input type="number" step="1" min="0" '+id+' value="'+esc(v[0])+'"><span class="muted">落地 ±</span><input type="number" step="1" min="0" '+id+' value="'+esc(v[1])+'"></span>';
  case "efftime":var auto=v===EFFTIME_AUTO;return '<span class="pair"><select class="in" '+id+'><option value="auto"'+(auto?" selected":"")+'>次日北京时间 0 点（自动）</option><option value="fixed"'+(auto?"":" selected")+'>固定时间（UTC）</option></select>'+
   '<input type="text" '+id+' placeholder="YYYY-MM-DD HH:MM:SS" value="'+(auto?"":esc(v))+'"'+(auto?" hidden":"")+"></span>";
  case "ci":return '<span class="pair">'+ciOps().map(function(op){return '<label class="ci"><b>'+esc(op)+'</b><input type="number" step="1" min="0" '+id+' data-op="'+esc(op)+'" value="'+(v&&v[op]!=null?esc(v[op]):"")+'"></label>';}).join("")+"</span>";
  case "ws":return '<span class="mono">'+esc(v||"未设置")+'</span> <a href="#/data">到数据页选择</a>';}
  return "";}
 function srcHtml(f){
  var tag=f.reset?'<span class="tag chg">保存后恢复</span>':f.src==="default"?'<span class="muted">默认</span>':'<span class="tag'+(f.src==="season"?" rv":" ok")+'">'+(f.src==="season"?"本航季":"全站")+"</span>";
  var ovr=f.src==="season"&&f[0]!=="season",can=f.src!=="default"&&!f.reset&&f[3]!=="ws"&&(f[0]==="season"||!isEmptyVal(f.fb));
  var rb=can?'<button class="btn sm" data-reset="'+f.i+'" title="'+(ovr?"删除本航季的覆盖，改用全站值 "+esc(cfgShow(f.fb,f[3])):"从文件里删掉这一项"+(isEmptyVal(f.def)?"":"，使用代码默认值 "+esc(cfgShow(f.def,f[3]))))+'">'+
   (ovr?"用全站值":isEmptyVal(f.def)?"清空":"恢复默认")+"</button>":"";
  return tag+rb;}
 function rowHtml(f){
  var hint=[];if(f[4])hint.push(esc(f[4]));if(f.src!=="default"&&!isEmptyVal(f.def)&&canon(f.cur)!==canon(f.def))hint.push("默认 "+esc(cfgShow(f.def,f[3])));
  return '<div class="frow" data-row="'+f.i+'"><div class="flab">'+esc(f[2])+"<code>"+esc(f[1])+'</code></div><div class="fin"><div class="fctl">'+inputHtml(f,f.reset?f.fb:f.cur)+"</div>"+
   (hint.length?'<div class="fhint">'+hint.join(" · ")+"</div>":"")+'<div class="ferr"></div></div><div class="fsrc">'+srcHtml(f)+"</div></div>";}
 function readField(f){var els=$$('[data-in="'+f.i+'"]',P),ty=f[3],s=els[0]?els[0].value.trim():"",i,o;
  function bad(m){return {err:m};}
  function isInt(x){return x!==""&&/^-?\d+$/.test(x);}
  switch(ty){
  case "text":case "date":return {v:s};
  case "int":if(!isInt(s))return bad("应为整数");return {v:parseInt(s,10)};
  case "num":if(s===""||!isFinite(Number(s)))return bad("应为数字");return {v:Number(s)};
  case "codes":return {v:s.split(/[\s,，]+/).filter(Boolean).map(function(x){return x.toUpperCase();})};
  case "ints":o=[];s.split(/[\s,，]+/).filter(Boolean).forEach(function(x){o.push(isInt(x)?parseInt(x,10):NaN);});
   return o.some(isNaN)?bad("应为逗号分隔的整数"):{v:o};
  case "pairs":o=[];var parts=s.split(/[,，;；]+/).map(function(x){return x.trim();}).filter(Boolean);
   for(i=0;i<parts.length;i++){var m=/^(\d+)\s*\/\s*(\d+)$/.exec(parts[i]);if(!m)return bad("格式：起飞/落地，多组用逗号分隔，如 45/60, 20/75");o.push([+m[1],+m[2]]);}
   return o.length?{v:o}:bad("至少要有一组");
  case "pair":var a=els[0].value.trim(),b=els[1].value.trim();if(!isInt(a)||!isInt(b))return bad("起飞和落地都应为整数");return {v:[parseInt(a,10),parseInt(b,10)]};
  case "efftime":if(els[0].value==="auto")return {v:EFFTIME_AUTO};var t=els[1].value.trim();
   if(!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$/.test(t))return bad("格式 YYYY-MM-DD HH:MM:SS（UTC）");return {v:t.length===16?t+":00":t};
  case "ci":o={};for(i=0;i<els.length;i++){var op=els[i].getAttribute("data-op"),x=els[i].value.trim();if(!isInt(x)||+x<0)return bad(op+" 应为非负整数");o[op]=parseInt(x,10);}return {v:o};
  case "ws":return {v:f.cur};}
  return {v:f.cur};}
 function collect(){var ch=[],errs=0;
  F.forEach(function(f){var row=$('[data-row="'+f.i+'"]',P),r=readField(f),c=null;
   row.querySelector(".ferr").textContent=r.err||"";row.classList.toggle("bad",!!r.err);
   if(r.err)errs++;
   else if(f.reset)c={section:f[0],key:f[1],value:null};
   else if(canon(r.v)!==canon(f.cur))c={section:f[0],key:f[1],value:r.v};
   row.classList.toggle("edited",!!c||!!r.err);if(c)ch.push(c);});
  return {changes:ch,errs:errs};}
 function update(){var r=collect(),n=r.changes.length;S.dirty=n>0||r.errs>0;
  $("#fcount").innerHTML=r.errs?'<span class="errc">'+r.errs+" 项格式不对</span>":n?'<span class="dirty">● '+n+" 项未保存</span>":"没有修改";
  $("#fundo").hidden=!S.dirty;$("#fsave").disabled=!n||r.errs>0||d.busy;
  $$("details[data-g]",P).forEach(function(x){if(x.querySelector(".frow.edited"))x.open=true;});}
 function rerow(f){var row=$('[data-row="'+f.i+'"]',P);row.outerHTML=rowHtml(f);}
 var W=$("#cfgpage");W.oninput=onEdit;W.onchange=onEdit;
 function onEdit(ev){var el=ev.target,i=el.getAttribute&&el.getAttribute("data-in");if(i==null)return;var f=F[+i];
  if(f[3]==="efftime"&&el.tagName==="SELECT"){var t=$$('[data-in="'+f.i+'"]',P)[1];t.hidden=el.value==="auto";if(!t.hidden&&!t.value)t.focus();}
  if(f.reset){f.reset=false;$('[data-row="'+f.i+'"] .fsrc',P).innerHTML=srcHtml(f);}
  if(f[0]==="operators"&&f[1]==="target"&&ev.type==="change"){var ci=field("export","default_cost_index");if(ci){var keep=readField(ci);
   var row=$('[data-row="'+ci.i+'"] .fctl',P);row.innerHTML=inputHtml(ci,keep.err?ci.cur:keep.v);}}
  update();}
 W.onclick=(function(ev){var b=ev.target.closest&&ev.target.closest("[data-reset]");if(!b)return;var f=F[+b.getAttribute("data-reset")];f.reset=true;rerow(f);update();});
 $("#fundo").onclick=function(){S.dirty=false;route(true);};
 $("#fsave").onclick=function(){var r=collect();if(!r.changes.length||r.errs)return;var btn=this;btn.disabled=true;$("#fres").innerHTML="";
  api("POST","api/config/form",{season:S.season,changes:r.changes,mtimes:mt}).then(function(res){S.dirty=false;
   var sm=res.summary;S.flash='<div class="msg ok">已保存 '+r.changes.length+" 项"+(res.files&&res.files.length?"（写入 "+esc(res.files.join("、"))+"，旧版本已自动备份）":"")+
    (sm?" · "+esc(sm.season)+" "+esc(sm.start)+" ~ "+esc(sm.end)+" · 生效 "+esc(sm.effective_time)+" UTC":"")+"</div>"+
    (res.warnings&&res.warnings.length?'<div class="msg warn"><b>警告</b>'+errList(res.warnings)+"</div>":"");
   toast("已保存");route(true);})
  .catch(function(e){var x=e.data||{};btn.disabled=false;
   $("#fres").innerHTML='<div class="msg err"><b>'+({config:"配置校验失败",conflict:"保存冲突",syntax:"TOML 语法错误"}[x.stage]||"保存失败")+"</b>"+(x.errors&&x.errors.length?errList(x.errors):" "+esc(e.message))+
    "没有写入任何文件。"+(x.stage==="conflict"?' <button class="btn sm" id="freload">重新加载（放弃修改）</button>':"")+"</div>";
   if($("#freload"))$("#freload").onclick=function(){S.dirty=false;route(true);};
   $("#fres").scrollIntoView({block:"nearest"});});};
 update();
}
/* FR24 account: saved on its own (not part of the TOML form); the password is write-only */
var CRED_SRC={WEB:"网页填写的账号",ENV:"环境变量（/etc/vcca/vcca.env）"};
function drawFr24Account(box,a,busy){
 var envOk=a.env.user&&a.env.password,web=!!(a.user&&a.has_password);
 var using=a.source?'<span class="tag ok">'+esc(CRED_SRC[a.source]||a.source)+"</span>":'<span class="tag bad">没有账号，fetch 无法登录</span>';
 box.innerHTML='<h2>FR24 账户 <span class="muted fnote">单独保存，立即生效；密码保存后不再显示</span></h2>'+
  '<div class="frow"><div class="flab">账号<code>FR24 登录邮箱</code></div><div class="fin"><div class="fctl"><input type="text" id="fau" autocomplete="off" spellcheck="false" value="'+esc(a.user)+'"></div></div><div class="fsrc">'+(web?'<span class="tag ok">已保存</span>':'<span class="muted">未填写</span>')+"</div></div>"+
  '<div class="frow"><div class="flab">密码</div><div class="fin"><div class="fctl"><input type="password" id="fap" autocomplete="new-password" placeholder="'+(web?"已保存，留空 = 不修改":"")+'"></div>'+
  '<div class="fhint">保存在服务器 $VCCA_HOME/state/fr24_account.json（只有服务账号可读）。保存后会清掉缓存的 FR24 登录，下次抓取用新账号登录。</div><div class="ferr" id="faerr"></div></div><div class="fsrc"></div></div>'+
  '<div class="row" style="padding:8px 0 4px 8px"><button class="btn primary" id="fasave"'+(busy?" disabled":"")+'>保存账户</button>'+(web?'<button class="btn" id="faclear"'+(busy?" disabled":"")+'>删除网页账户</button>':"")+
  '<span class="muted">抓取将使用：</span>'+using+(web&&envOk?'<span class="muted">（环境变量里也有账号，网页填写的优先）</span>':"")+"</div>";
 function err(m){$("#faerr").textContent=m||"";}
 function done(info,msg){toast(msg);drawFr24Account(box,info,busy);}
 function fail(e){err(e.message);}
 $("#fasave").onclick=function(){var u=$("#fau").value.trim(),p=$("#fap").value;err("");
  if(!u)return err("请填写账号");if(!p&&!(web&&u===a.user))return err("请填写密码");
  this.disabled=true;var b=this;api("POST","api/fr24/account",{user:u,password:p}).then(function(info){done(info,"FR24 账户已保存");}).catch(function(e){b.disabled=false;fail(e);});};
 if($("#faclear"))$("#faclear").onclick=function(){
  if(!confirm("删除网页填写的 FR24 账户？"+(envOk?"之后改用环境变量里的账号。":"环境变量里也没有账号，之后 fetch 将无法登录。")))return;
  api("POST","api/fr24/account",{clear:true}).then(function(info){done(info,"已删除网页填写的 FR24 账户");}).catch(fail);};
}
function fmtVal(v){return typeof v==="string"?JSON.stringify(v):JSON.stringify(v).replace(/,/g,", ").replace(/:/g," = ");}
function effTable(e){
 return '<label class="chk" style="margin:8px 0"><input type="checkbox" id="effonly"> 只看与默认不同的</label>'+
  '<div class="tbox"><table class="tbl" id="efftbl"><thead><tr><th>节</th><th>键</th><th>生效值</th><th>来源</th><th>默认值</th></tr></thead><tbody>'+
  e.rows.map(function(r){return '<tr class="'+(r.changed?"chg":"same")+'"'+(r.changed?' style="background:var(--chg)"':"")+'><td class="mono">['+esc(r.section)+']</td><td class="mono">'+esc(r.key)+'</td><td class="mono wrap">'+esc(fmtVal(r.value))+"</td><td>"+(r.source==="default"?'<span class="muted">默认</span>':'<span class="tag'+(r.source==="season"?" rv":" ok")+'">'+esc(r.source_label)+"</span>")+'</td><td class="mono wrap muted">'+esc(fmtVal(r.default))+"</td></tr>";}).join("")+
  "</tbody></table></div>";}

/* ----- TOML file editor ----- */
function lineDiff(a,b){var n=a.length,m=b.length,L=[],i,j,out=[];
 for(i=0;i<=n;i++)L.push(new Int32Array(m+1));
 for(i=n-1;i>=0;i--)for(j=m-1;j>=0;j--)L[i][j]=a[i]===b[j]?L[i+1][j+1]+1:Math.max(L[i+1][j],L[i][j+1]);
 i=0;j=0;while(i<n||j<m){if(i<n&&j<m&&a[i]===b[j]){out.push([" ",a[i]]);i++;j++;}else if(j>=m||(i<n&&L[i+1][j]>=L[i][j+1])){out.push(["-",a[i]]);i++;}else{out.push(["+",b[j]]);j++;}}
 return out;}
function diffHtml(a,b){var d=lineDiff(a.replace(/\r/g,"").split("\n"),b.replace(/\r/g,"").split("\n")),show=d.map(function(){return false;}),k,o=[],skip=0;
 d.forEach(function(x,i){if(x[0]!==" ")for(k=Math.max(0,i-2);k<=Math.min(d.length-1,i+2);k++)show[k]=true;});
 if(!show.some(Boolean))return '<div class="muted">与当前内容完全相同。</div>';
 d.forEach(function(x,i){if(!show[i]){skip++;return;}if(skip){o.push('<span class="dskip">… '+skip+" 行相同 …</span>");skip=0;}
  o.push('<span class="d'+(x[0]==="+"?"add":x[0]==="-"?"del":"")+'">'+(x[0]===" "?"  ":x[0]+" ")+esc(x[1])+"</span>");});
 if(skip)o.push('<span class="dskip">… '+skip+" 行相同 …</span>");
 return '<pre class="preview diff">'+o.join("\n")+"</pre>";}
function pageCfgFile(P,d,cur){
 P.innerHTML=cfgHead(d)+
  '<div class="cfgbar"><span id="cstat" class="cstat"></span><span id="cdirty"></span><span class="sp"></span>'+
  '<span class="hwrap"><button class="btn" id="chist"></button><div class="hpop panel" id="hpop" hidden></div></span>'+
  '<button class="btn primary" id="csave" disabled title="Ctrl+S">保存</button></div>'+
  '<div class="editor" id="edbox"><pre class="gut" id="gut"></pre><textarea id="ced" spellcheck="false" wrap="off"></textarea></div><div id="cres"></div><div id="hprev"></div></div>';
 cfgTabs(d.files,cur.key);
 var ed=$("#ced"),gut=$("#gut"),errLine=null,vt=null,vseq=0,state="clean";
 cur.text=cur.text.replace(/\r\n?/g,"\n");    /* a textarea always uses \n: compare (and save) like that */
 ed.value=cur.text;
 var STAGE={syntax:"TOML 语法错误",config:"配置校验失败",conflict:"保存冲突"};
 function status(html){$("#cstat").innerHTML=html;}
 function idle(){status('<span class="muted mono">'+esc(cur.path)+"</span>");}
 function setDirty(v){S.dirty=v;$("#cdirty").innerHTML=v?'<span class="dirty">● 未保存</span> <button type="button" class="linkbtn" id="cundo">撤销</button>':"";
  if(v)$("#cundo").onclick=function(){ed.value=cur.text;ed.oninput();};
  $("#csave").disabled=!v||state==="bad"||d.busy;}
 function gutter(){var n=ed.value.split("\n").length,o=[];for(var i=1;i<=n;i++)o.push(i===errLine?"<b>"+i+"</b>":String(i));gut.innerHTML=o.join("\n")+"\n";gut.scrollTop=ed.scrollTop;
  $("#edbox").style.height=Math.min(Math.max(n+1,12)*18+18,Math.round(window.innerHeight*0.72))+"px";}
 function show(r,saved){errLine=r.syntax&&r.syntax.line||null;gutter();
  var warn=r.warnings&&r.warnings.length?'<div class="msg warn"><b>警告</b>'+errList(r.warnings)+"</div>":"";
  if(r.ok){state="ok";status('<span class="okc">✓ '+(saved?"已保存":"校验通过")+"</span>"+(saved&&r.backup?'<span class="muted"> · 旧版本已备份</span>':"")+
    (r.summary?'<span class="muted"> · '+esc(r.summary.season)+" "+esc(r.summary.start)+" ~ "+esc(r.summary.end)+" · 生效 "+esc(r.summary.effective_time)+" UTC</span>":""));$("#cres").innerHTML=warn;}
  else{state="bad";status('<span class="errc">✗ '+(STAGE[r.stage]||"失败")+(r.syntax&&r.syntax.line?"（第 "+r.syntax.line+" 行）":"")+"</span>");
   $("#cres").innerHTML='<div class="msg err">'+errList(r.errors)+(saved===false?"文件未改动。":"")+"</div>";}
  setDirty(S.dirty);}
 function validate(){var my=++vseq,text=ed.value;
  api("POST","api/config/validate",{season:S.season,key:cur.key,text:text}).then(function(r){if(my===vseq)show(r);})
   .catch(function(e){if(my!==vseq)return;if(e.data&&e.data.stage)show(e.data);else{state="ok";status('<span class="muted">无法校验：'+esc(e.message)+"</span>");}});}
 ed.oninput=function(){var dirty=ed.value!==cur.text;clearTimeout(vt);vseq++;
  if(!dirty){state="clean";errLine=null;idle();$("#cres").innerHTML="";}
  else{state="checking";status('<span class="muted"><span class="spinner"></span>校验中…</span>');vt=setTimeout(validate,700);}
  setDirty(dirty);gutter();};
 ed.onscroll=function(){gut.scrollTop=ed.scrollTop;};
 ed.onkeydown=function(e){if(e.key==="Tab"){e.preventDefault();var s=ed.selectionStart;ed.setRangeText("    ",s,ed.selectionEnd,"end");ed.oninput();}
  else if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){e.preventDefault();save();}};
 function save(){if(!S.dirty||state==="bad")return;if(d.busy){toast("有任务正在运行，暂时不能保存",true);return;}
  clearTimeout(vt);vseq++;$("#csave").disabled=true;var text=ed.value;
  api("POST","api/config/save",{season:S.season,key:cur.key,text:text,mtime_ns:cur.mtime_ns}).then(function(r){
   cur.text=text;cur.mtime_ns=r.mtime_ns;S.dirty=false;show(r,true);setDirty(ed.value!==cur.text);toast("已保存");loadHist();})
  .catch(function(e){if(e.data&&e.data.stage)show(e.data,false);else{$("#cres").innerHTML='<div class="msg err">'+esc(e.message)+"</div>";setDirty(true);}});}
 $("#csave").onclick=save;
 /* history: drop-down list, diff against the editor, restore */
 var hb=$("#chist"),hp=$("#hpop");
 function drawHist(){var H=cur.history||[];hb.textContent="历史 ("+H.length+")";hb.hidden=!H.length;
  hp.innerHTML='<div class="muted" style="margin-bottom:4px">每次保存前自动备份；对比 = 历史版本 → 编辑器当前内容</div><ul class="flist">'+H.map(function(v){
   return '<li><span><span class="mono">'+esc(v.version)+'</span> <span class="n">'+fmtTime(v.mtime,false)+'</span></span><span><button class="btn sm" data-pv="'+esc(v.version)+'">对比</button> <button class="btn sm" data-rs="'+esc(v.version)+'">恢复</button></span></li>';}).join("")+"</ul>";
  $$("[data-pv]",hp).forEach(function(b){b.onclick=function(){var ver=b.getAttribute("data-pv");hp.hidden=true;
   api("GET","api/config/history?"+sq({key:cur.key,version:ver})).then(function(r){
    $("#hprev").innerHTML='<div class="panel"><div class="row"><b>'+esc(r.version)+' → 当前内容</b><span class="sp"></span><button class="btn sm" id="hclose">关闭</button></div>'+diffHtml(r.text,ed.value)+"</div>";
    $("#hclose").onclick=function(){$("#hprev").innerHTML="";};$("#hprev").scrollIntoView({block:"nearest"});}).catch(function(e){toast(e.message,true);});};});
  $$("[data-rs]",hp).forEach(function(b){b.onclick=function(){var ver=b.getAttribute("data-rs");if(!confirm("用历史版本 "+ver+" 替换当前文件？（当前文件会先备份，恢复同样需要通过校验）"+(S.dirty?"\n编辑器里未保存的修改会丢失。":"")))return;
   hp.hidden=true;api("POST","api/config/restore",{season:S.season,key:cur.key,version:ver,mtime_ns:cur.mtime_ns}).then(function(){S.dirty=false;toast("已恢复 "+ver);route(true);})
    .catch(function(e){if(e.data&&e.data.stage)show(e.data,false);else toast(e.message,true);});};});}
 function loadHist(){api("GET","api/config?"+sq()).then(function(dd){var f=dd.files.filter(function(x){return x.key===cur.key;})[0];cur.history=f.history;drawHist();});}
 hb.onclick=function(ev){ev.stopPropagation();hp.hidden=!hp.hidden;};
 hp.onclick=function(ev){ev.stopPropagation();};
 $("#cfgpage").onclick=function(){hp.hidden=true;};
 drawHist();idle();gutter();setDirty(false);
}

/* ---------- help ---------- */
function pageHelp(P){
 P.innerHTML='<div class="help"><h1>说明</h1><div class="cols2"><div class="panel"><h2 style="margin-top:0">日常流程</h2><ol>'+
  '<li><a href="#/jobs">运行</a> → <b>全流程 run</b>（或依次 fetch → build → export）。日常增量抓取通常几十到几百个请求。</li>'+
  '<li><a href="#/results">结果</a> 打开最新一次：概览、各分类表格、单条航线"计划 / VAMSYS / 推荐 / FR24"对照、FR24 逐日日历、与上次运行的差异。</li>'+
  '<li>处理"人工复核"标签页与 <code>_待复核</code> CSV 里的条目（确认无误的行可并入对应主 CSV）。</li>'+
  '<li>按顺序在 FlightOpsDesk 导入（结果页左栏已按此顺序排列）。</li>'+
  '<li>FlightOpsDesk 同步 VAMSYS 后，到 <a href="#/data">数据</a> 页上传新的 workspaces.db，再跑一次 build：结果应只剩新的变化。</li></ol></div>'+
  '<div class="panel"><h2 style="margin-top:0">FlightOpsDesk 导入顺序</h2><ol><li><code>01_airports_add_*.csv</code>（如有）</li><li><code>02_routes_retire_*.csv</code>（停用，写 End Date）</li><li><code>02b_routes_retire_deferred_*.csv</code>（暂缓停用，确认后再导）</li><li><code>03_routes_update_*.csv</code></li><li><code>04_routes_create_*.csv</code></li></ol>'+
  '<p><code>CHARTER</code>（临时加班/包机）、<code>POSITIONING</code>（调机/测试）、<code>WATCH_PLAN</code>（计划有但近期未飞）、<code>HOLD</code> 只出现在报告里，不进 CSV。</p>'+
  '<p>生效时间（停用 End Date / 新建 Start Date）默认是"运行当天的次日 00:00 北京时间"（UTC 16:00）。</p></div>'+
  '<div class="panel"><h2 style="margin-top:0">换季</h2><ol><li><a href="#/data">数据</a> 页"新建航季"（精简模板，只有 [season]）。</li><li>上传本季三份 CAAC <code>（三字码）.xls</code>。</li><li><a href="#/config">配置</a> 页的"设置"表单改本航季名称、日期（留空则自动取 FD23/FD24 众数）；工作区、生效时间、Cost Index、阈值等全站设置也在这个表单里，每项都标明来自默认 / 全站 / 本航季。</li><li>运行 status 检查无报错，再 run。新航季首次抓取约 30–40 分钟。</li></ol></div>'+
  '<div class="panel"><h2 style="margin-top:0">提示</h2><ul><li>任务同一时间只能运行一个；运行中不能保存配置或上传数据。</li><li>中断 fetch 会保存进度，重新运行时已抓的不会重复。</li><li>配置保存前会完整校验，失败时文件保持不变；每次保存前的旧版本可在"历史版本"恢复。</li><li>时间：运行文件夹名与报告中的时刻均为 UTC；页面上的文件时间按浏览器时区显示并标注时区。</li></ul></div></div></div>';
}

/* ---------- boot ---------- */
VccaReport.applyTheme();
api("GET","api/me",undefined,{noRedirect:true}).then(function(d){S.user=d.user;S.csrf=d.csrf;S.maxUpload=d.max_upload_mb;return loadSeasons();})
 .then(function(){if(!location.hash||location.hash==="#/login")location.hash="#/overview";route(true);})
 .catch(function(){S.after=location.hash&&location.hash!=="#/login"?location.hash:null;showLogin();});
})();
