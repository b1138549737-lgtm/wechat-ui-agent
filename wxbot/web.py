"""T230 Web 控制面板：标准库 http.server + 单页 HTML，不引额外依赖。

接口：
  GET  /                面板页面（必须带 ?token=…）
  GET  /api/state       运行状态 + 24 小时统计 + 最近一次回复
  GET  /api/logs        最近回复记录（?limit=&username=）
  GET  /api/contacts    白名单（含 enabled / 触发模式）
  GET  /api/sessions    最近会话（加白名单时挑人用）
  POST /api/pause | /api/resume | /api/stop
  POST /api/manual      {contact, text, dry_run}
  POST /api/contacts    {action: add|toggle|remove, name, username, mode}

安全（审查 N4）：
- 启动时生成一次性 token，打印在控制台；**所有**请求都要带（页面用 ?token=，接口用
  `X-WXBot-Token` 头）。面板能替你发消息，不能只靠"只监听 127.0.0.1"。
- 写接口强制 `Content-Type: application/json` 且校验 `Origin`/`Sec-Fetch-Site`：
  否则任意网页都能用"简单请求"（text/plain 的 JSON 体，不触发预检）驱动它发消息。
- 页面里所有来自微信的数据（消息内容、联系人名）都走 `esc()` 转义后再插进 DOM，
  防止"别人发一条带 onerror 的消息 → 面板执行 JS → 借你的号发消息"。

写配置时会先把 config.yaml 备份成 config.yaml.bak（面板重写会丢注释，README 里有说明）。
"""
from __future__ import annotations

import hmac
import http.server
import json
import pathlib
import secrets
import shutil
import threading
import time
import urllib.parse

import yaml

from .runtime import RT
from .store import Store


class Handler(http.server.BaseHTTPRequestHandler):
    cfg = None
    cfg_path: pathlib.Path | None = None
    store: Store | None = None
    log_fn = print
    token: str = ""
    allowed_origins: tuple[str, ...] = ()

    # ---- 基础 ----
    def log_message(self, fmt, *args):       # 面板日志不刷屏
        return

    def _send(self, body: bytes, ctype: str, code: int = 200, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    # ---- 安全（审查 N4）----
    def _token_ok(self, query: dict, allow_query: bool = True) -> bool:
        """校验 token。

        ★第三轮审查：**写操作只认请求头**（`allow_query=False`）——
        页面地址带 token 万一被转发/粘到别处，凭据就跟着 URL 走了。
        读接口允许 query（curl 调试方便），页面本身（静态 HTML、不带数据）不需要 token，
        这样页面可以用 `#token=`（fragment 不会被发给服务端、也不进 Referer）。"""
        if not self.token:
            return True
        got = (self.headers.get("X-WXBot-Token") or "").strip()
        if not got and allow_query:
            got = (query.get("token") or [""])[0].strip()
        return bool(got) and hmac.compare_digest(got, self.token)

    def _same_site_ok(self) -> bool:
        """拦跨站写请求：浏览器发的简单请求也会带这些头。"""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True
        return any(origin == f"http://{o}" or origin.startswith(f"http://{o}:")
                   for o in self.allowed_origins)

    def _content_type_ok(self) -> bool:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() \
            == "application/json"

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            # 纯静态页面，不含任何数据 → 不需要 token（页面用 #token= 拿凭据，服务端看不到）
            return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8", 200,
                              extra={"Content-Security-Policy":
                                     "default-src 'none'; script-src 'unsafe-inline'; "
                                     "style-src 'unsafe-inline'; connect-src 'self'; "
                                     "img-src 'self' data:"})
        if not self._token_ok(query):
            return self._json({"error": "unauthorized：缺少或错误的 token"}, 403)
        if url.path == "/api/state":
            st = RT.snapshot()
            try:
                st["stats"] = self.store.summary()
            except Exception as exc:  # noqa: BLE001
                st["stats"] = {"error": str(exc)}
            st["dry_run"] = bool(self.cfg.get("app.dry_run"))
            st["window_title"] = str(self.cfg.get("send.window_title", "微信"))
            return self._json(st)
        if url.path == "/api/logs":
            limit = int((query.get("limit") or ["30"])[0])
            username = (query.get("username") or [None])[0]
            try:
                rows = self.store.recent_replies(limit=min(limit, 200), username=username)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)}, 500)
            return self._json({"rows": rows})
        if url.path == "/api/contacts":
            out = []
            for c in self.cfg.contacts(enabled_only=False):
                eff = self.cfg.effective(c)
                out.append({
                    "name": c.get("name", ""),
                    "username": c.get("username", ""),
                    "enabled": bool(c.get("enabled", True)),
                    "mode": (eff.get("trigger") or {}).get("mode", ""),
                    "self_ok": bool(c.get("self_ok")),
                })
            return self._json({"rows": out})
        if url.path == "/api/sessions":
            try:
                from .cli import Sources
                rows = [{"username": s.get("username"),
                         "name": s.get("displayName") or s.get("name") or ""}
                        for s in Sources(self.cfg).sessions(40)]
            except Exception as exc:  # noqa: BLE001
                return self._json({"rows": [], "error": str(exc)})
            return self._json({"rows": rows})
        return self._json({"error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        # ★审查 N4：顺序是"同站 → token → Content-Type"，不给跨站请求任何机会
        if not self._same_site_ok():
            return self._json({"error": "cross-site request blocked"}, 403)
        if not self._token_ok(query, allow_query=False):        # 写操作只认请求头
            return self._json({"error": "unauthorized：写操作需要 X-WXBot-Token"}, 403)
        if not self._content_type_ok():
            return self._json({"error": "只接受 Content-Type: application/json"}, 415)
        body = self._read_body()
        if url.path == "/api/pause":
            RT.pause(str(body.get("reason") or "面板暂停"))
            return self._json({"ok": True, "paused": True})
        if url.path == "/api/resume":
            RT.resume()
            return self._json({"ok": True, "paused": False})
        if url.path == "/api/stop":
            RT.request_stop()
            return self._json({"ok": True})
        if url.path == "/api/manual":
            contact = str(body.get("contact") or "").strip()
            if not contact:
                return self._json({"ok": False, "error": "缺少 contact"}, 400)
            RT.push_manual(contact, str(body.get("text") or ""), bool(body.get("dry_run")))
            return self._json({"ok": True, "queued": True})
        if url.path == "/api/contacts":
            try:
                return self._json(self._edit_contacts(body))
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc)}, 500)
        return self._json({"error": "not found"}, 404)

    # ---- 白名单编辑 ----
    def _edit_contacts(self, body: dict) -> dict:
        action = str(body.get("action") or "")
        name = str(body.get("name") or "").strip()
        # 注意：读写**原始文件**，避免把 ${VAR} 之类的占位符展开后写回去
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
        rows = raw.get("contacts") or []
        if action == "add":
            username = str(body.get("username") or "").strip()
            if not name or not username:
                return {"ok": False, "error": "name 与 username 都要填"}
            if any((c.get("username") == username) for c in rows):
                return {"ok": False, "error": f"{username} 已经在白名单里"}
            item = {"name": name, "username": username,
                    "enabled": bool(body.get("enabled", True))}
            mode = str(body.get("mode") or "").strip()
            if mode:
                item["trigger"] = {"mode": mode}
            rows.append(item)
        elif action in ("toggle", "remove"):
            hit = [c for c in rows if c.get("username") == body.get("username")]
            if not hit:
                return {"ok": False, "error": "没找到这个会话"}
            for c in hit:
                if action == "remove":
                    rows.remove(c)
                else:
                    c["enabled"] = not bool(c.get("enabled", True))
        else:
            return {"ok": False, "error": f"不认识的动作 {action!r}"}
        # ★2026-09-27 审查 P2：原来**每次**写配置都把 `config.yaml.bak` 覆盖掉 ——
        # 连做三次白名单操作后，带注释的原始版本就永久没了（实测 143 行注释 → 0）。
        # 现在：第一次写留一份"原始带注释版" .bak；之后每次写留**带时间戳**的副本，不再互相覆盖。
        backup = self.cfg_path.with_name(self.cfg_path.name + ".bak")
        if backup.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = self.cfg_path.with_name(f"{self.cfg_path.name}.bak-{stamp}")
        shutil.copyfile(self.cfg_path, backup)
        raw["contacts"] = rows
        self.cfg_path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        RT.request_reload()
        self.log_fn(f"[面板] 白名单已更新（{action} {name or body.get('username')}），"
                    f"原文件备份到 {backup.name}")
        return {"ok": True, "backup": backup.name, "count": len(rows)}


def make_server(cfg, host: str = "127.0.0.1", port: int = 8765,
                token: str | None = None) -> http.server.ThreadingHTTPServer:
    Handler.cfg = cfg
    Handler.cfg_path = pathlib.Path(cfg.path)
    Handler.store = Store.get(cfg.path_of("app.data_dir") / "wxbot.db")
    Handler.log_fn = print
    Handler.token = token if token is not None else secrets.token_urlsafe(18)
    Handler.allowed_origins = tuple({host, "127.0.0.1", "localhost"}
                                    - {"0.0.0.0", "", None})
    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.panel_token = Handler.token          # 调用方要把它打印出来
    return httpd


def web_token(cfg) -> str:
    """面板 token：配置里写死就用写死的；否则生成一次并缓存到 data/web_token.txt。

    缓存是为了让"重启后面板地址不变"（自动启动的快捷方式也就能一直用同一个链接）。
    """
    fixed = str(cfg.get("web.token") or "").strip()
    if fixed:
        return fixed
    path = cfg.path_of("app.data_dir") / "web_token.txt"
    try:
        if path.exists():
            old = path.read_text(encoding="utf-8").strip()
            if old:
                return old
        fresh = secrets.token_urlsafe(18)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh, encoding="utf-8")
        return fresh
    except Exception:  # noqa: BLE001
        return secrets.token_urlsafe(18)


def serve_in_thread(cfg, host: str = "127.0.0.1", port: int = 8765,
                    token: str | None = None):
    """起一个后台线程跑面板，返回 (server, thread)。"""
    httpd = make_server(cfg, host, port, token=token)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    t.start()
    return httpd, t


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>wxbot 控制面板</title>
<style>
 body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#222}
 header{background:#07c160;color:#fff;padding:10px 16px;font-size:17px}
 main{padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
 section{background:#fff;border-radius:8px;padding:12px;box-shadow:0 1px 3px #0001}
 h2{font-size:14px;margin:0 0 8px;color:#555}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:4px 6px;border-bottom:1px solid #eee;vertical-align:top}
 button{cursor:pointer;border:0;border-radius:5px;padding:6px 12px;background:#07c160;color:#fff;font-size:13px}
 button.gray{background:#8a8f99}button.red{background:#e05a5a}
 input,select{padding:5px;border:1px solid #ccc;border-radius:5px;font-size:13px}
 .kv{font-size:13px;line-height:1.9}
 .ok{color:#07a35a}.bad{color:#d33}
 .row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px}
 .full{grid-column:1/-1}
 pre{white-space:pre-wrap;word-break:break-all;font-size:12px;background:#fafafa;padding:6px;border-radius:5px}
</style></head><body>
<header>wxbot 控制面板</header>
<main>
 <section><h2>运行状态</h2><div id="state" class="kv">加载中…</div>
  <div class="row" style="margin-top:8px">
   <button onclick="post('/api/resume')">恢复</button>
   <button class="gray" onclick="post('/api/pause')">暂停</button>
   <button class="red" onclick="post('/api/stop')">停止</button>
   <button class="gray" onclick="load()">刷新</button>
  </div>
 </section>
 <section><h2>试跑一条（dry-run 只生成不发送）</h2>
  <div class="row"><select id="m_contact"></select>
   <input id="m_text" placeholder="要发的内容" style="flex:1">
  </div>
  <div class="row">
   <button onclick="manual(false)">真发</button>
   <button class="gray" onclick="manual(true)">dry-run</button>
  </div>
  <div id="m_result" class="kv"></div>
 </section>
 <section><h2>白名单</h2>
  <table><thead><tr><th>名字</th><th>username</th><th>模式</th><th>状态</th><th></th></tr></thead>
   <tbody id="contacts"></tbody></table>
  <div class="row" style="margin-top:8px">
   <input id="c_name" placeholder="显示名（要填微信里的完整名）">
   <select id="c_sess" onchange="pickSession()"><option value="">从会话里选…</option></select>
  </div>
  <div class="row"><input id="c_username" placeholder="username（wxid_xxx / xxx@chatroom）" style="flex:1">
   <select id="c_mode"><option value="whitelist_only">whitelist_only</option>
    <option value="mention">mention</option><option value="keyword">keyword</option>
    <option value="always">always</option></select>
   <button onclick="addContact()">加入白名单</button>
  </div>
  <div class="kv" style="color:#888">写回 config.yaml 前会先备份成 config.yaml.bak（重写会丢注释）</div>
 </section>
 <section><h2>最近回复</h2><div class="row"><input id="l_filter" placeholder="按 username 过滤">
  <button class="gray" onclick="loadLogs()">查询</button></div><div id="logs"></div>
 </section>
</main>
<script>
const $=id=>document.getElementById(id);
// token 从 URL fragment 取（#token=…）：fragment 不会被发到服务端、也不进 Referer，
// 比 ?token= 安全；兼容老的 ?token= 写法。
const TOKEN=new URLSearchParams(location.hash.slice(1)).get('token')
          || new URLSearchParams(location.search).get('token') || '';
// 所有来自微信的字段（消息内容/联系人名）都先转义再进 DOM：审查 N4 的存储型 XSS
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cut=(s,n)=>esc(String(s==null?'':s).slice(0,n));
async function api(p,body,method){const r=await fetch(p,{method:method||(body?'POST':'GET'),
 headers:{'Content-Type':'application/json','X-WXBot-Token':TOKEN},
 body:body?JSON.stringify(body):undefined});
 if(r.status===403){document.body.innerHTML='<p style="padding:20px">未授权：请用启动时打印的带 ?token= 的地址打开面板。</p>';throw new Error('unauthorized');}
 return await r.json();}
async function post(p,body){const j=await api(p,body||{});load();return j;}
function ts(t){return new Date(t*1000).toLocaleString('zh-CN');}
let LAST_OK=0, LAST_ERR='';
async function load(){
 const s=await api('/api/state');
 LAST_OK=Date.now(); LAST_ERR='';
 $('state').innerHTML=`状态：<b class="${s.paused?'bad':'ok'}">${s.paused?'已暂停':'运行中'}</b>${esc(s.paused_reason?'（'+s.paused_reason+'）':'')}<br>
  运行时长：${Math.floor(s.uptime_seconds/60)} 分 ${s.uptime_seconds%60} 秒｜本次已回：<b>${s.replied}</b> 条｜连续失败：${s.failures}｜待执行手动任务：${s.manual_pending}<br>
  24 小时：回复 ${s.stats.replies_24h||0} 条（失败 ${s.stats.failed_24h||0}）｜消息状态 ${esc(JSON.stringify(s.stats.messages||{}))}<br>
  监听会话：${esc((s.contacts||[]).join('、')||'（无）')}<br>微信窗口：${esc(s.window_title)}`;
  const lr=s.last_reply;
  $('state').innerHTML+=lr?`<br>最近一条：<span class="${lr.ok?'ok':'bad'}">${lr.ok?'成功':'失败'}</span> ${esc(lr.contact)}「${cut(lr.request,20)}」→ ${cut(lr.reply,30)}`:'' ;
 // ★两个"看着像卡住"的坑（2026-09-26 用户反馈）：
 //   ① 会话说“只加载一次”，机器人换号重启后下拉里还是旧账号的会话 → 每次 load 都重取（保留当前选中项）
 //   ② 后台标签页里浏览器会把定时器节流/冻结，且失败是静默的 → 这里显式回显"最后更新时间"，
 //      并且在窗口重新可见/获得焦点时立刻刷一次（见文件末尾的 visibilitychange/focus）
  $('state').innerHTML+=`<br><span class="kv" style="color:#888">最后更新 ${new Date().toLocaleTimeString('zh-CN')}（每 8 秒自动刷新）</span>`;
  const c=await api('/api/contacts');
 $('contacts').innerHTML=c.rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.username)}</td><td>${esc(r.mode)}</td>
  <td class="${r.enabled?'ok':'bad'}">${r.enabled?'启用':'停用'}</td>
  <td><button class="gray" data-u="${esc(r.username)}" onclick="toggleC(this.dataset.u)">${r.enabled?'停用':'启用'}</button>
  <button class="red" data-u="${esc(r.username)}" onclick="delC(this.dataset.u)">删</button></td></tr>`).join('');
 const opts=c.rows.map(r=>`<option>${esc(r.name)}</option>`).join('');
 if($('m_contact').innerHTML!==opts){$('m_contact').innerHTML=opts;$('m_contact').value=$('m_contact').value||c.rows[0]?.name||'';}
 try{
  const ss=await api('/api/sessions');
  const keep=$('c_sess').value;
  $('c_sess').innerHTML='<option value="">从会话里选…（共 '+ss.rows.length+' 个，会随微信切换自动更新）</option>'+ss.rows.map(s=>
   `<option value="${esc(s.username)}|${esc(s.name)}">${esc(s.name)} — ${esc(s.username)}</option>`).join('');
  $('c_sess').value=keep;
 }catch(e){ LAST_ERR=String(e&&e.message||e); }
  loadLogs();
}
function pickSession(){const v=$('c_sess').value;if(!v)return;const p=v.indexOf('|');if(p<0)return;
 $('c_username').value=v.slice(0,p);$('c_name').value=v.slice(p+1);}
async function manual(dry){const r=await post('/api/manual',{contact:$('m_contact').value,text:$('m_text').value,dry_run:dry});
 $('m_result').textContent=r.ok?'已排队，等运行循环下一次迭代执行（看日志）':('失败：'+(r.error||''));}
async function addContact(){const r=await post('/api/contacts',{action:'add',name:$('c_name').value,
 username:$('c_username').value,mode:$('c_mode').value});
 alert(r.ok?('已加入，白名单共 '+r.count+' 条'):('失败：'+(r.error||'')));}
async function toggleC(u){const r=await post('/api/contacts',{action:'toggle',username:u});if(!r.ok)alert(r.error||'');}
async function delC(u){if(!confirm('确定从白名单删除？'))return;const r=await post('/api/contacts',{action:'remove',username:u});if(!r.ok)alert(r.error||'');}
async function loadLogs(){const u=$('l_filter').value;const d=await api('/api/logs?limit=30&username='+encodeURIComponent(u));
 $('logs').innerHTML='<table><thead><tr><th>时间</th><th>会话</th><th>收到</th><th>回复</th><th>结果</th><th>详情</th></tr></thead><tbody>'
  +(d.rows||[]).map(r=>`<tr><td>${ts(r.ts)}</td><td>${esc(r.username)}</td><td>${cut(r.request,16)}</td>
  <td>${cut(r.reply,20)}</td><td class="${r.ok?'ok':'bad'}">${r.ok?'✅':'❌'}</td>
  <td><pre>${cut(r.detail,180)}</pre></td></tr>`).join('')+'</tbody></table>';}
// 失败要看得见：连不上时在标题栏下面挂一条红字，并显示"最后成功更新的时间"
function tick(){load().catch(e=>{LAST_ERR=String(e&&e.message||e);
 $('state').innerHTML+=`<br><span class="bad">⚠️ 面板暂时取不到数据：${esc(LAST_ERR)}`
  +(LAST_OK?`（最后成功更新 ${new Date(LAST_OK).toLocaleTimeString('zh-CN')}）`:'')+`</span>`;});}
tick();setInterval(tick,8000);
// 后台标签页会被浏览器节流（定时器可能几十秒甚至冻结）→ 一回到前台就立刻刷一次
document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
window.addEventListener('focus',tick);
</script></body></html>
"""
