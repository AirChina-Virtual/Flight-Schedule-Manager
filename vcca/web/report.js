/* vCCA result renderer — VccaReport.mount(rootEl, payload, opts)
   Shared by the web app (vcca/web/app.js) and the single-file report.html (export_html inlines this file).
   payload = export_html.build_payload(...) JSON.
   opts (all optional):
     header      true  -> render title/subtitle header (report.html); false -> caller shows its own
     themeButton true  -> header button cycling auto/light/dark (localStorage "vcca-theme", <html data-theme>)
     hash        true  -> keep the current tab in location.hash (report.html)
     tab         initial tab id ("all", "cat:UPDATE", "review", "diff", "overview")
     onTab(id)   called when the tab changes
     fileUrl(name) -> URL: CSV names become download links
     diffRuns    [run names] offered as "compare with" choices on the diff tab
     onDiffChange(runName) called when another comparison run is picked
   Returns {destroy(), go(tabId)}. No fixed element ids; everything lives inside rootEl. */
(function(global){
"use strict";
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c];});}
function el(tag,attrs,html){var e=document.createElement(tag);if(attrs)for(var k in attrs)e.setAttribute(k,attrs[k]);if(html!=null)e.innerHTML=html;return e;}

/* ---------- theme (shared with the app) ---------- */
var THEMES=["auto","light","dark"],THEME_ZH={auto:"自动",light:"浅色",dark:"深色"};
function getTheme(){var t="auto";try{t=localStorage.getItem("vcca-theme")||"auto";}catch(e){}return THEMES.indexOf(t)<0?"auto":t;}
function setTheme(t){try{localStorage.setItem("vcca-theme",t);}catch(e){}applyTheme(t);}
function applyTheme(t){t=t||getTheme();if(t==="auto")document.documentElement.removeAttribute("data-theme");else document.documentElement.setAttribute("data-theme",t);return t;}

function mount(root,D,opts){
opts=opts||{};
var ROWS=D.rows, CATS=D.cats, CATZH={}; CATS.forEach(function(c){CATZH[c.code]=c.zh;});
var PAGE=400;
var CSVNAME={};(D.csv||[]).forEach(function(c){if(c.key)CSVNAME[c.key]=c.name;});
root.innerHTML="";
var box=el("div",{"class":"vr"+(opts.header?"":" embedded")});
box.innerHTML=(opts.header?'<header class="vr-head"><div><h1 data-r="title"></h1><div class="muted" data-r="subtitle"></div></div><div data-r="hbtns"></div></header>':"")+
 '<div class="vr-wrap"><div class="cards" data-r="cards"></div><div data-r="warns"></div><div class="tabs" data-r="tabs"></div><div data-r="view"></div></div>'+
 '<aside class="vr-detail" data-r="detail" aria-hidden="true"></aside>';
root.appendChild(box);
function $(r){return box.querySelector('[data-r="'+r+'"]');}
var VIEW=$("view"),DETAIL=$("detail");

function fileLink(name,label){if(!name)return "";return opts.fileUrl?'<a href="'+esc(opts.fileUrl(name))+'" download>'+esc(label||name)+"</a>":esc(label||name);}
function csvCell(r){var n=CSVNAME[r.csv];return n?fileLink(n,r.csv):esc(r.csv);}

/* ---------- header / cards ---------- */
var M=D.meta,S=D.summary;
if(opts.header){
 $("title").textContent="vCCA 航季重算报告 · "+M.season_label+" ["+M.season+"]";
 $("subtitle").innerHTML="计算 "+esc(D.meta_fmt.generated_at)+" · 生效 "+esc(D.meta_fmt.effective_time)+
  " · VAMSYS revision "+esc(M.vamsys_revision)+" · FR24 "+esc(M.fr24_rows)+" 行，最新航班 "+esc(D.meta_fmt.fr24_latest);
 if(opts.themeButton){var tb=el("button",{"class":"btn",title:"切换浅色/深色"});
  var paint=function(){tb.textContent="主题："+THEME_ZH[getTheme()];};
  tb.onclick=function(){var t=getTheme();setTheme(THEMES[(THEMES.indexOf(t)+1)%3]);paint();};
  applyTheme();paint();$("hbtns").appendChild(tb);}
}
var cards=$("cards");
function card(n,l,cls,onclick){var c=el("div",{"class":"card "+(cls||"")},'<div class="n">'+esc(n)+'</div><div class="l">'+esc(l)+'</div>');if(onclick)c.onclick=onclick;cards.appendChild(c);}
card(ROWS.length,"决策总数","",function(){go("all");});
CATS.forEach(function(c){if(c.n)card(c.n,c.zh+(c.csv?"":"（只列报告）"),"",function(){go("cat:"+c.code);});});
card(S.needs_review,"待人工复核",S.needs_review?"warn":"ok",function(){go("review");});
card(S.closure_ok?"通过":"不平",("去向核账 "+S.closure.decided+"/"+S.closure.expected),S.closure_ok?"ok":"bad",function(){go("overview");});
var csvN=D.csv.reduce(function(a,b){return a+b.n;},0);
card(csvN,"CSV 导入行（"+D.csv.length+" 个文件）","",function(){go("overview");});
if(D.diff)card(D.diff.added.length+"/"+D.diff.removed.length+"/"+D.diff.changed.length,"与 "+D.diff.prev_run+" 相比 增/删/改","",function(){go("diff");});
(S.warnings||[]).forEach(function(w){$("warns").appendChild(el("div",{"class":"warn"},"⚠ "+esc(w)));});

/* ---------- tabs ---------- */
var TABS=[["all","全部",ROWS.length]];
CATS.forEach(function(c){if(c.n)TABS.push(["cat:"+c.code,c.zh,c.n]);});
TABS.push(["review","人工复核",S.needs_review]);
if(D.diff||(opts.diffRuns&&opts.diffRuns.length))TABS.push(["diff","与上次差异",D.diff?D.diff.added.length+D.diff.removed.length+D.diff.changed.length:null]);
TABS.push(["overview","总览",null]);
var cur="all";
function drawTabs(){var t=$("tabs");t.innerHTML="";TABS.forEach(function(x){var b=el("button",{"class":"tab"+(x[0]===cur?" on":"")},esc(x[1])+(x[2]!=null?'<span class="c">'+x[2]+'</span>':""));b.onclick=function(){go(x[0]);};t.appendChild(b);});}
function go(id){if(!TABS.some(function(t){return t[0]===id;}))id="all";cur=id;drawTabs();
 if(opts.hash){try{history.replaceState(null,"","#"+id);}catch(e){}}
 if(opts.onTab)opts.onTab(id);
 if(id==="diff")drawDiff();else if(id==="overview")drawOverview();else drawTable();}

/* ---------- weekdays ---------- */
var WDZH=["一","二","三","四","五","六","日"];
function fdset(s){var o={};for(var i=0;i<7;i++)if(s&&s.charAt(i)&&s.charAt(i)!==".")o[i]=1;return o;}
function daysChanged(r){return r.ch.some(function(c){return c.f==="days";})&&r.cur&&r.cur!=="same";}
/* old -> new weekdays in one string: kept digits plain, added <ins>, dropped <del> */
function daysDiff(oldS,newS){var a=fdset(oldS),b=fdset(newS),h=[];
 for(var i=0;i<7;i++){var d=String(i+1);h.push(a[i]&&b[i]?d:b[i]?"<ins>"+d+"</ins>":a[i]?"<del>"+d+"</del>":".");}return '<span class="dd">'+h.join("")+"</span>";}
function daysCell(r){return daysChanged(r)?daysDiff(r.cur.days,r.days):esc(r.days);}
function hmMin(s){var m=/^(\d\d):(\d\d)$/.exec(s||"");return m?(+m[1])*60+(+m[2]):null;}
function circAbs(a,b){var d=Math.abs(a-b)%1440;return Math.min(d,1440-d);}
function dUTC(s){var p=s.split("-");return Date.UTC(+p[0],+p[1]-1,+p[2]);}
function dStr(t){return new Date(t).toISOString().slice(0,10);}
var DAY=86400000;

/* all decisions on the same route key (op, callsign, orig, dest), numbered S1..Sn by departure time */
var SIB=null;
function siblings(r){
 if(!SIB){SIB={};ROWS.forEach(function(x){if(x.c==="AIRPORT_ADD"||!x.cs)return;var k=[x.op,x.cs,x.o,x.d].join("|");(SIB[k]=SIB[k]||[]).push(x);});
  Object.keys(SIB).forEach(function(k){SIB[k].sort(function(a,b){return (a.tgt.dep||"")<(b.tgt.dep||"")?-1:(a.tgt.dep||"")>(b.tgt.dep||"")?1:a.i-b.i;});});}
 return r.cs&&SIB[[r.op,r.cs,r.o,r.d].join("|")]||[r];
}
/* index (in sibs) of the slot whose target dep is nearest to hm within tol, else -1 */
function slotOf(sibs,hm,tol){var m=hmMin(hm),best=-1,bd=1e9;if(m==null)return -1;
 sibs.forEach(function(x,j){var t=hmMin(x.tgt.dep);if(t==null)return;var d=circAbs(m,t);if(d<=tol&&d<bd){bd=d;best=j;}});return best;}

/* slot table: every schedule of this route side by side, FR24 actuals assigned to the nearest slot */
function slotTable(r){
 var sibs=siblings(r);if(sibs.length<2)return "";
 var tol=(D.cal_range||{}).tol||45,acts=sibs.map(function(){return 0;}),last=sibs.map(function(){return "";}),free=0;
 (r.ev.cal||[]).forEach(function(e){var p=e.split(" ");if(p[2]!=="A")return;var j=slotOf(sibs,p[1],tol);
  if(j<0){free++;return;}acts[j]++;if(p[0]>last[j])last[j]=p[0];});
 var h=['<div class="tablebox free"><table><thead><tr><th>排期</th><th>类别</th><th>VAMSYS ID</th><th>起飞</th><th>到达</th><th>星期</th><th>变更</th><th>FR24 实飞</th><th>末次</th><th>复核</th><th>CSV</th></tr></thead><tbody>'];
 sibs.forEach(function(x,j){h.push('<tr data-i="'+x.i+'"'+(x.i===r.i?' class="me"':"")+'><td class="sl">S'+(j+1)+(x.i===r.i?" ◀":"")+'</td><td><span class="tag">'+esc(CATZH[x.c]||x.c)+'</span></td><td class="mono">'+esc(x.v)+
  '</td><td class="mono">'+esc(x.tgt.dep)+'</td><td class="mono">'+esc(x.tgt.arr)+'</td><td class="mono">'+daysCell(x)+"</td><td>"+
  x.ch.map(function(c){return '<span class="tag chg">'+esc(c.f)+"</span>";}).join("")+"</td><td>"+acts[j]+'</td><td class="mono">'+esc(last[j])+"</td><td>"+
  (x.rv?'<span class="tag rv">'+esc(x.rf.join(","))+"</span>":"")+"</td><td>"+esc(x.csv)+"</td></tr>");});
 h.push("</tbody></table></div>");
 h.push('<div class="muted" style="margin-top:4px">FR24 实飞按起飞时刻归入最近的排期（±'+tol+" 分）"+(free?"；另有 "+free+" 班不属于任何排期":"")+"。点行查看该排期。</div>");
 return h.join("");
}

/* FR24 per-day grid: one row per week (Mon-Sun, UTC), each cell lists that day's departures */
function calendar(r){
 var cal=r.ev.cal,CR=D.cal_range||{};if(!cal||!cal.length||!CR.start)return "";
 var tdep=hmMin(r.tgt.dep),tol=CR.tol||45,by={},maxD=CR.latest||CR.start,sibs=siblings(r),multi=sibs.length>1,me=sibs.indexOf(r);
 cal.forEach(function(e){var p=e.split(" ");(by[p[0]]=by[p[0]]||[]).push({hm:p[1],a:p[2]==="A"});if(p[0]>maxD)maxD=p[0];});
 var s0=dUTC(CR.start),e0=Math.min(dUTC(maxD),CR.end?dUTC(CR.end):Infinity),latest=CR.latest?dUTC(CR.latest):e0;
 var w0=s0-((new Date(s0).getUTCDay()+6)%7)*DAY;
 var plan=r.plan&&r.plan.days?fdset(r.plan.days):null,cur=r.cur==="same"?fdset(r.tgt.days):(r.cur?fdset(r.cur.days):null),tg=fdset(r.tgt.days);
 /* weekday changes are shown against VAMSYS when it differs from the target, else against the CAAC plan
    (e.g. WEEKDAY_DROPPED on a route VAMSYS already matches) */
 function same(a,b){for(var i=0;i<7;i++)if(!!a[i]!==!!b[i])return false;return true;}
 var base=null,baseZh="";
 if(r.cur&&r.cur!=="same"&&!same(cur,tg)){base=cur;baseZh="VAMSYS";}else if(plan&&r.c.indexOf("RETIRE")!==0&&!same(plan,tg)){base=plan;baseZh="计划";}
 var dchg=!!base,h=[];
 h.push('<div class="legend"><span><span class="ci a">01:35</span> 推荐时段实飞</span><span><span class="ci ao">03:10</span> 其他时段实飞'+(multi?"（不属于任何排期）":"")+'</span>'+
  (multi?'<span><span class="ci ob">10:35<sub>S2</sub></span> 其他排期实飞</span>':"")+
  '<span><span class="ci sx">01:35</span> 有计划未见实飞</span><span><span class="ci s">01:35</span> 尚未到期的计划</span><span>— 无记录</span>'+
  (dchg?'<span><span class="mkd add"></span> 新增星期</span><span><span class="mkd del"></span> 删除星期</span>':"")+
  '<span class="muted">日期与时间均为 UTC；推荐时段 = '+(multi?"S"+(me+1)+" ":"")+esc(r.tgt.dep||"—")+" ±"+tol+" 分"+(multi?"；Sn = 归入第 n 个排期":"")+"</span></div>");
 h.push('<div class="calbox"><table class="cal"><thead><tr><th></th>');
 for(var i=0;i<7;i++)h.push("<th"+(dchg&&(!!base[i])!==(!!tg[i])?' class="dchg"':"")+">周"+WDZH[i]+"</th>");
 h.push("</tr></thead><tbody>");
 function mk(label,set,cls){if(!set)return;h.push('<tr class="mk"><td class="lab">'+esc(label)+"</td>");
  for(var i=0;i<7;i++)h.push("<td>"+(typeof cls==="function"?cls(i):(set[i]?'<span class="mkd '+(cls||"")+'"></span>':""))+"</td>");h.push("</tr>");}
 mk("CAAC 计划",plan);
 if(r.cur&&r.cur!=="same")mk("VAMSYS 现行",cur);
 mk((r.cur==="same"?"现行 = 推荐":(r.cur?"推荐":"推荐（新建）"))+(dchg?"（对比"+baseZh+"）":""),tg,function(i){if(!tg[i]&&!(dchg&&base[i]))return "";
  if(dchg&&tg[i]&&!base[i])return '<span class="mkd add" title="新增"></span>';if(dchg&&!tg[i]&&base[i])return '<span class="mkd del" title="删除"></span>';return '<span class="mkd tg"></span>';});
 var hit=[0,0,0,0,0,0,0],seen=[0,0,0,0,0,0,0];
 for(var w=w0;w<=e0;w+=7*DAY){h.push('<tr><td class="lab mono">'+dStr(w).slice(5)+"</td>");
  for(var i=0;i<7;i++){var t=w+i*DAY,ds=dStr(t),list=by[ds]||[];
   if(t<s0||(CR.end&&t>dUTC(CR.end))){h.push('<td class="d out"></td>');continue;}
   var past=t<=latest,inner=[],same=false;
   list.sort(function(x,y){return x.hm<y.hm?-1:1;}).forEach(function(o){
    var j=multi?slotOf(sibs,o.hm,tol):-1,near=multi?j===me:(tdep!=null&&circAbs(hmMin(o.hm),tdep)<=tol);
    if(o.a&&near)same=true;
    var cls=o.a?(near?"a":(j>=0?"ob":"ao")):(past?"sx":"s");
    inner.push('<span class="ci '+cls+'">'+esc(o.hm)+(j>=0?"<sub>S"+(j+1)+"</sub>":"")+"</span>");});
   if(past){seen[i]++;if(same)hit[i]++;}
   h.push('<td class="d'+(inner.length?"":(past?" none":" fut"))+'" title="'+ds+'">'+inner.join("")+"</td>");}
  h.push("</tr>");}
 h.push('<tr class="sum"><td class="lab">推荐时段实飞</td>');for(var i=0;i<7;i++)h.push('<td class="mono">'+hit[i]+"/"+seen[i]+"</td>");
 h.push("</tr></tbody></table></div>");
 return h.join("");
}

/* ---------- table ---------- */
var COLS=[
 ["c","类别",function(r){return '<span class="tag">'+esc(CATZH[r.c]||r.c)+"</span>";}],
 ["v","VAMSYS ID",function(r){return '<span class="mono">'+esc(r.v)+"</span>";}],
 ["op","航司"],["cs","呼号"],["num","班号"],["o","起"],["d","降"],
 ["dep","起飞UTC",null,"mono"],["arr","到达UTC",null,"mono"],["days","星期",daysCell,"mono"],
 ["chg","变更",function(r){return r.ch.map(function(c){return '<span class="tag chg">'+esc(c.f)+"</span>";}).join("");}],
 ["st","证据"],["acts","实飞"],["la","末次实飞"],
 ["rv","复核",function(r){return r.rv?'<span class="tag rv">'+esc(r.rf.join(","))+"</span>":"";}],
 ["csv","CSV",csvCell],
 ["why","原因",function(r){return esc(r.rz.join("；"));},"why"]
];
var sortKey=null,sortDesc=false,shown=PAGE,filters={q:"",op:"",st:"",conf:"",rv:false,chg:false},selIdx=null;
function val(r,k){if(k==="chg")return r.ch.length;if(k==="why")return r.rz.join(" ");return r[k];}
function hay(r){if(!r._h)r._h=[r.c,CATZH[r.c],r.v,r.op,r.cs,r.num,r.o,r.d,r.dep,r.arr,r.days,r.st,r.csv,r.rz.join(" "),r.rf.join(" "),r.ch.map(function(c){return c.f;}).join(" "),(r.plan&&r.plan.refs||[]).join(" ")].join(" ").toLowerCase();return r._h;}
function current(){
 var list=ROWS;
 if(cur.indexOf("cat:")===0){var c=cur.slice(4);list=list.filter(function(r){return r.c===c;});}
 else if(cur==="review")list=list.filter(function(r){return r.rv;});
 var q=filters.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
 return list.filter(function(r){
  if(filters.op&&r.op!==filters.op)return false;if(filters.st&&r.st!==filters.st)return false;
  if(filters.conf&&r.conf!==filters.conf)return false;if(filters.rv&&!r.rv)return false;if(filters.chg&&!r.ch.length)return false;
  if(q.length){var h=hay(r);for(var i=0;i<q.length;i++)if(h.indexOf(q[i])<0)return false;}return true;});
}
function uniq(k){var s={};ROWS.forEach(function(r){if(r[k]!==""&&r[k]!=null)s[r[k]]=1;});return Object.keys(s).sort();}
function opts_(k,label){return '<select data-f="'+k+'"><option value="">'+label+"：全部</option>"+uniq(k).map(function(v){return '<option'+(filters[k]===v?" selected":"")+">"+esc(v)+"</option>";}).join("")+"</select>";}
function drawTable(){
 VIEW.innerHTML='<div class="bar"><input type="search" data-r="q" placeholder="搜索 呼号/班号/机场/ID/原因…（空格分隔多个词）" value="'+esc(filters.q)+'">'+
  opts_("op","航司")+opts_("st","证据")+opts_("conf","置信度")+
  '<label><input type="checkbox" data-r="frv"'+(filters.rv?" checked":"")+'> 只看复核</label>'+
  '<label><input type="checkbox" data-r="fchg"'+(filters.chg?" checked":"")+'> 只看有变更</label>'+
  '<span class="muted" data-r="cnt"></span></div><div class="tablebox"><table><thead><tr></tr></thead><tbody></tbody></table></div><div class="more" data-r="more"></div>';
 var tr=VIEW.querySelector("thead tr");
 COLS.forEach(function(c){var th=el("th",{"data-k":c[0]},esc(c[1]));if(sortKey===c[0])th.className="sorted"+(sortDesc?" desc":"");
  th.onclick=function(){if(sortKey===c[0])sortDesc=!sortDesc;else{sortKey=c[0];sortDesc=false;}shown=PAGE;drawTable();};tr.appendChild(th);});
 var qi=$("q"),timer=null;qi.oninput=function(){clearTimeout(timer);timer=setTimeout(function(){filters.q=qi.value;shown=PAGE;fill();},120);};
 VIEW.querySelectorAll("select[data-f]").forEach(function(s){s.onchange=function(){filters[s.getAttribute("data-f")]=s.value;shown=PAGE;fill();};});
 $("frv").onchange=function(){filters.rv=this.checked;shown=PAGE;fill();};
 $("fchg").onchange=function(){filters.chg=this.checked;shown=PAGE;fill();};
 fill();
}
function fill(){
 var list=current();
 if(sortKey){list=list.slice().sort(function(a,b){var x=val(a,sortKey),y=val(b,sortKey);
  if(typeof x==="number"&&typeof y==="number")return sortDesc?y-x:x-y;x=String(x==null?"":x);y=String(y==null?"":y);
  return sortDesc?y.localeCompare(x,"zh"):x.localeCompare(y,"zh");});}
 var tb=VIEW.querySelector("tbody"),html=[];
 var n=Math.min(shown,list.length);
 for(var i=0;i<n;i++){var r=list[i];html.push('<tr data-i="'+r.i+'"'+(r.i===selIdx?' class="sel"':"")+">");
  for(var j=0;j<COLS.length;j++){var c=COLS[j];html.push("<td"+(c[3]?' class="'+c[3]+'"':"")+">"+(c[2]?c[2](r):esc(r[c[0]]))+"</td>");}
  html.push("</tr>");}
 tb.innerHTML=html.join("")||'<tr><td colspan="'+COLS.length+'" class="empty">没有匹配的记录</td></tr>';
 $("cnt").textContent="共 "+list.length+" 条"+(n<list.length?"，显示前 "+n+" 条":"");
 var m=$("more");m.innerHTML="";if(n<list.length){var b=el("button",{"class":"btn"},"再显示 "+Math.min(PAGE*2,list.length-n)+" 条");b.onclick=function(){shown+=PAGE*2;fill();};m.appendChild(b);
  var b2=el("button",{"class":"btn"},"全部显示");b2.onclick=function(){shown=list.length;fill();};m.appendChild(b2);}
}
function onClick(e){
 if(e.target.closest("a"))return;
 if(e.target.closest('[data-r="close"]')){closeDetail();return;}
 var tr=e.target.closest("tbody tr[data-i]");
 if(tr&&(VIEW.contains(tr)||DETAIL.contains(tr)))openDetail(+tr.getAttribute("data-i"));
}
box.addEventListener("click",onClick);

/* ---------- detail ---------- */
var FIELDS=[["op","航司"],["cs","呼号"],["num","班号"],["o","起飞机场"],["d","到达机场"],["ty","类型"],["dep","起飞 UTC"],["arr","到达 UTC"],
 ["days","星期"],["fl","机队"],["ci","Cost Index"],["sd","Start Date"],["ed","End Date"]];
var FMAP={orig:"o",dest:"d",type:"ty",callsign:"cs",number:"num",fleet_ids:"fl",cost_index:"ci",start_date:"sd",end_date:"ed",dep:"dep",arr:"arr",days:"days",hidden:"hid"};
function closeDetail(){DETAIL.classList.remove("open");DETAIL.setAttribute("aria-hidden","true");}
function openDetail(i){
 selIdx=i;var r=ROWS[i];VIEW.querySelectorAll("tbody tr.sel").forEach(function(x){x.classList.remove("sel");});
 var t=VIEW.querySelector('tbody tr[data-i="'+i+'"]');if(t)t.classList.add("sel");
 var cur=r.cur==="same"?r.tgt:r.cur,chg={};r.ch.forEach(function(c){chg[FMAP[c.f]||c.f]=1;});
 var p=r.plan||{},ev=r.ev;
 var planCol={op:r.plan?(p.key.split(" ")[0]||""):"",cs:r.plan?(p.key.split(" ")[1]||""):"",o:r.plan?(p.key.split(" ")[2]||""):"",d:r.plan?(p.key.split(" ")[3]||""):"",
  dep:p.dep||"",arr:p.arr||"",days:p.days||""};
 var frCol={dep:(ev.banks||[]).map(function(b){return b.dep;}).join(" / "),arr:(ev.banks||[]).map(function(b){return b.arr;}).join(" / "),
  days:(ev.banks||[]).map(function(b){return b.fd3;}).join(" / "),fl:Object.keys(ev.ac||{}).map(function(k){return k+"×"+ev.ac[k];}).join(", ")};
 var h=['<button class="btn close" data-r="close">关闭 ✕</button>',
  "<h2>"+esc(r.cs||r.o)+" "+esc(r.o)+(r.d?"–"+esc(r.d):"")+' <span class="tag">'+esc(CATZH[r.c]||r.c)+"</span>"+(r.rv?' <span class="tag rv">需复核</span>':"")+"</h2>",
  '<div class="muted">VAMSYS ID '+esc(r.v||"—")+" · 置信度 "+esc(r.conf)+" · CSV "+(r.csv?csvCell(r):"不导出")+(r.si?" · 时段 "+esc(r.si):"")+"</div>",
  "<h2>计划 / VAMSYS / 推荐 / FR24</h2>",'<div class="grid4"><div class="h"></div><div class="h">CAAC 计划</div><div class="h">VAMSYS 现行</div><div class="h">推荐（目标）</div><div class="h">FR24 证据</div>'];
 FIELDS.forEach(function(f){var k=f[0],c=chg[k]?" chg":"";
  h.push('<div class="k">'+esc(f[1])+"</div><div>"+esc(planCol[k]||"")+"</div><div"+(c?' class="chg"':"")+">"+esc(cur?cur[k]:"—")+"</div><div"+(c?' class="chg"':"")+">"+(k==="days"&&daysChanged(r)?daysDiff(r.cur.days,r.tgt.days):esc(r.tgt[k]))+"</div><div>"+esc(frCol[k]||"")+"</div>");});
 h.push("</div>");
 if(r.ch.length){h.push("<h2>变更</h2><ul class=\"reasons\">");r.ch.forEach(function(c){h.push("<li><b>"+esc(c.f)+"</b>："+
  (c.f==="days"&&daysChanged(r)?daysDiff(r.cur.days,r.tgt.days)+" <span class=\"muted\">（"+esc(c.o)+" → "+esc(c.n)+"）</span>":esc(c.o)+" → "+esc(c.n))+"</li>");});h.push("</ul>");}
 var stH=slotTable(r);if(stH)h.push("<h2>同航线全部排期（"+siblings(r).length+"）</h2>"+stH);
 var calH=calendar(r);if(calH)h.push("<h2>FR24 逐日记录</h2>"+calH);
 h.push("<h2>原因</h2><ul class=\"reasons\">");r.rz.forEach(function(z,j){h.push("<li>"+esc(z)+(r.rf.indexOf(r.rc[j])>=0?' <span class="tag rv">复核</span>':"")+' <span class="muted mono">'+esc(r.rc[j])+"</span></li>");});h.push("</ul>");
 h.push("<h2>FR24 证据</h2><div class=\"kv\"><div>状态</div><div>"+esc(r.st)+"</div><div>实飞 / 观测 / 近期实飞</div><div>"+esc(r.acts)+" / "+esc(ev.obs)+" / "+esc(r.ra)+"</div><div>末次实飞</div><div>"+esc(r.la)+"</div>"+
  "<div>机型</div><div>"+esc(frCol.fl)+"</div><div>注册号</div><div>"+esc((ev.regs||[]).join(", "))+"</div><div>日期</div><div>"+esc((ev.dates||[]).join(", "))+"</div>"+
  "<div>同班号他司</div><div>"+esc((ev.oo||[]).map(function(x){return x.join("/");}).join(", "))+"</div><div>同呼号其他航段</div><div>"+esc((ev.od||[]).map(function(x){return x.join("/");}).join(", "))+"</div></div>");
 if(ev.banks&&ev.banks.length){h.push('<h2>FR24 时段</h2><div class="tablebox free"><table><thead><tr><th>起飞</th><th>到达</th><th>星期</th><th>实飞</th><th>观测</th><th>近期观测</th></tr></thead><tbody>');
  ev.banks.forEach(function(b){h.push('<tr><td class="mono">'+esc(b.dep)+'</td><td class="mono">'+esc(b.arr)+'</td><td class="mono">'+esc(b.fd3)+"</td><td>"+esc(b.acts)+"</td><td>"+esc(b.obs)+"</td><td>"+esc(b.recent_obs)+"</td></tr>");});h.push("</tbody></table></div>");}
 h.push("<h2>计划来源</h2><div>"+((p.refs||[]).map(esc).join("<br>")||'<span class="muted">（无计划行）</span>')+"</div>");
 if(r.dap&&r.dap.length)h.push("<h2>依赖新增机场</h2><div>"+esc(r.dap.join(", "))+"</div>");
 var dk=Object.keys(r.det||{});if(dk.length){h.push('<h2>其他</h2><div class="kv">');dk.forEach(function(k){h.push("<div>"+esc(k)+"</div><div>"+esc(JSON.stringify(r.det[k]))+"</div>");});h.push("</div>");}
 DETAIL.innerHTML=h.join("");DETAIL.classList.add("open");DETAIL.setAttribute("aria-hidden","false");DETAIL.scrollTop=0;
}
function onKey(e){if(!box.isConnected){document.removeEventListener("keydown",onKey);return;}if(e.key==="Escape")closeDetail();}
document.addEventListener("keydown",onKey);

/* ---------- diff ---------- */
function drawDiff(){
 var df=D.diff,h=[];
 if(opts.diffRuns&&opts.diffRuns.length){
  h.push('<div class="diffpick"><label>对比运行 <select data-r="diffsel">'+(df?"":'<option value="">（选择）</option>')+
   opts.diffRuns.map(function(n){return "<option"+(df&&df.prev_run===n?" selected":"")+">"+esc(n)+"</option>";}).join("")+"</select></label></div>");}
 if(!df){h.push('<div class="empty">没有上一次运行可对比</div>');VIEW.innerHTML=h.join("");bindDiffSel();return;}
 h.push('<p class="muted">与运行 <b>'+esc(df.prev_run)+"</b> 对比（按 VAMSYS ID，或 航司+呼号+起降+时段 匹配）。</p>");
 function tbl(title,list,withDiff){h.push("<h2>"+esc(title)+"（"+list.length+"）</h2>");if(!list.length){h.push('<div class="muted">无</div>');return;}
  h.push('<div class="tablebox mid"><table><thead><tr><th>类别</th>'+(withDiff?"<th>上次类别</th>":"")+"<th>VAMSYS ID</th><th>航线</th><th>起飞</th><th>到达</th><th>星期</th><th>复核</th>"+(withDiff?"<th>差异</th>":"")+"</tr></thead><tbody>");
  list.forEach(function(e){h.push('<tr><td><span class="tag">'+esc(e.category_zh)+"</span></td>"+(withDiff?"<td>"+esc(CATZH[e.prev_category]||e.prev_category)+"</td>":"")+'<td class="mono">'+esc(e.vroute_id||"")+"</td><td>"+esc(e.label)+'</td><td class="mono">'+esc(e.dep)+'</td><td class="mono">'+esc(e.arr)+'</td><td class="mono">'+esc(e.days)+"</td><td>"+(e.needs_review?'<span class="tag rv">是</span>':"")+"</td>"+
   (withDiff?'<td class="why">'+e.diffs.map(function(x){return "<b>"+esc(x.field)+"</b>: "+esc(x.old)+" → "+esc(x.new);}).join("<br>")+"</td>":"")+"</tr>");});
  h.push("</tbody></table></div>");}
 tbl("新增的决策",df.added,false);tbl("消失的决策",df.removed,false);tbl("变化的决策",df.changed,true);
 VIEW.innerHTML=h.join("");bindDiffSel();
}
function bindDiffSel(){var s=$("diffsel");if(s)s.onchange=function(){if(s.value&&opts.onDiffChange)opts.onDiffChange(s.value);};}

/* ---------- overview ---------- */
function drawOverview(){
 var h=["<h2>数据源</h2><div class=\"kv\">"];
 [["航季",M.season_label+"（"+M.season_start+" ~ "+M.season_end+"）"],["计算时间",D.meta_fmt.generated_at],["生效时间",D.meta_fmt.effective_time],
  ["CAAC 计划","目标航司行 "+M.plan_rows+"，航段 "+M.plan_legs+"，班号 "+M.plan_numbers],
  ["VAMSYS","revision "+M.vamsys_revision+"（StorageRevision "+M.vamsys_storage_revision+"），航线 "+M.vamsys_routes+"，商业 "+M.vamsys_commercial_routes],
  ["FR24 缓存",M.fr24_rows+" 行；最新航班 "+D.meta_fmt.fr24_latest+"；最后抓取 "+D.meta_fmt.fr24_last_fetch]].forEach(function(x){h.push("<div>"+esc(x[0])+"</div><div>"+esc(x[1])+"</div>");});
 h.push("</div><h2>决策分类</h2><div class=\"tablebox free\"><table><thead><tr><th>类别</th><th>说明</th><th>数量</th><th>待复核</th><th>CSV</th></tr></thead><tbody>");
 CATS.forEach(function(c){h.push('<tr><td class="mono">'+esc(c.code)+"</td><td>"+esc(c.zh)+"</td><td>"+c.n+"</td><td>"+(c.rv||"")+"</td><td>"+esc(c.csv||"只列报告")+"</td></tr>");});
 h.push("</tbody></table></div><h2>去向核账</h2><div class=\"kv\"><div>结果</div><div>"+(S.closure_ok?'<span class="tag ok">通过</span>':'<span class="tag bad">不平</span>')+"</div><div>已决 / 应有</div><div>"+S.closure.decided+" / "+S.closure.expected+"</div><div>重复去向</div><div>"+esc(S.closure.duplicates.join(", ")||"无")+"</div></div>");
 h.push("<h2>CSV 文件</h2>"+(D.csv.length?"<ul>"+D.csv.map(function(c){return "<li class=\"mono\">"+fileLink(c.name)+"</li>";}).join("")+"</ul>":'<div class="muted">无需导入的变更</div>'));
 h.push("<h2>人工复核标记</h2>"+(Object.keys(S.review_flags).length?"<ul>"+Object.keys(S.review_flags).map(function(k){return "<li><span class=\"mono\">"+esc(k)+"</span>："+S.review_flags[k]+"</li>";}).join("")+"</ul>":'<div class="muted">无</div>'));
 h.push("<h2>阈值与命中</h2><div class=\"tablebox free\"><table><thead><tr><th>阈值</th><th>取值</th><th>命中航线</th></tr></thead><tbody>");
 var names=Object.keys(M.thresholds||{});Object.keys(M.threshold_hits||{}).forEach(function(k){if(names.indexOf(k)<0)names.push(k);});
 names.forEach(function(k){var t=M.thresholds[k];h.push('<tr><td class="mono">'+esc(k)+'</td><td class="mono">'+esc(t==null?"":JSON.stringify(t))+"</td><td>"+esc(M.threshold_hits[k]==null?"":M.threshold_hits[k])+"</td></tr>");});
 h.push("</tbody></table></div>");
 VIEW.innerHTML=h.join("");
}

var start=opts.tab||(opts.hash?(location.hash||"").slice(1):"")||"all";
go(start);
return {go:go,destroy:function(){document.removeEventListener("keydown",onKey);box.removeEventListener("click",onClick);root.innerHTML="";}};
}

global.VccaReport={mount:mount,applyTheme:applyTheme,getTheme:getTheme,setTheme:setTheme,THEMES:THEMES,THEME_ZH:THEME_ZH};
})(window);
