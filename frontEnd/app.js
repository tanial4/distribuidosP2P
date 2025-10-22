// ====== Config ======
const BOOTSTRAP = "http://148.220.215.162:8002"; // Liz actuará de bootstrap


let token = null;
let username = null;
let apiBase = null;      // asignado en /login según el usuario
let es = null;
let afterId = 0;
const seenIds = new Set();
let pendingFile = null;

const $ = (id) => document.getElementById(id);

function show(viewId){
  ["loginView","chatView"].forEach(v => $(v).classList.add("hidden"));
  $(viewId).classList.remove("hidden");
}

function escapeHTML(s){
  return s.replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;", '"':"&quot;" }[c]));
}

function renderMessageContent(author, text){
  try {
    const obj = JSON.parse(text);
    if (obj && obj.type === "file" && obj.url && obj.name) {
      const sizeStr = obj.size ? ` (${Math.ceil(obj.size/1024)} KB)` : "";
      return `<div class="by">${author}</div>
              <div><a href="${obj.url}" target="_blank" rel="noopener noreferrer">${escapeHTML(obj.name)}</a>${sizeStr}</div>`;
    }
  } catch(_) {}
  return `<div class="by">${author}</div><div>${escapeHTML(text)}</div>`;
}

function logMsg({ id, author, text }, mine=false){
  const wrap = $("messages");
  const item = document.createElement("div");
  item.className = "msg" + (mine ? " mine" : "");
  item.innerHTML = renderMessageContent(author, text);
  wrap.appendChild(item);
  wrap.scrollTop = wrap.scrollHeight;
  if (id) {
    const n = Number(id);
    if (!Number.isNaN(n)) afterId = Math.max(afterId, n);
  }
}

// API
async function apiLogin(user, pass){
  const res = await fetch(`${BOOTSTRAP}/login`, {
    method: "POST",
    headers: { "Content-Type":"application/json" },
    body: JSON.stringify({ username: user, password: pass })
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json(); // { access_token, user, peer_http_base }
}

async function apiSend(text){
  const res = await fetch(`${apiBase}/send`, {
    method: "POST",
    headers: {
      "Content-Type":"application/json",
      "Authorization": `Bearer ${token}`
    },
    body: JSON.stringify({ text })
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function apiMessages(){
  const res = await fetch(`${apiBase}/messages?after_id=${afterId}`, {
    headers: { "Authorization": `Bearer ${token}` }
  });
  if (!res.ok) throw new Error(await res.text());
  const txt = await res.text();
  const rows = txt.split("\n").filter(Boolean);
  for (const line of rows){
    const [idStr, author, text] = line.split("|");
    const id = Number(idStr || 0);
    if (author === username) continue;
    if (id && seenIds.has(id)) continue;
    logMsg({ id, author, text }, false);
    if (id) seenIds.add(id);
  }
}

async function apiUpload(file){
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${apiBase}/upload`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}` },
    body: form
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json(); 
}

// SSE
function connectSSE(){
  disconnectSSE();
  $("statusChip").textContent = "SSE: conectando…";
  es = new EventSource(`${apiBase}/stream?token=${encodeURIComponent(token)}`);
  es.onopen = () => { $("statusChip").textContent = "SSE: conectado"; };
  es.onerror = () => { $("statusChip").textContent = "SSE: error"; };
  es.onmessage = (e) => {
    const id = Number(e.lastEventId || 0);
    const idx = e.data.indexOf(": ");
    const author = idx > 0 ? e.data.slice(0, idx) : "desconocido";
    const text = idx > 0 ? e.data.slice(idx + 2) : e.data;
    if (author === username) return;
    if (id && (id <= afterId || seenIds.has(id))) return;
    logMsg({ id, author, text }, false);
    if (id) seenIds.add(id);
  };
}
function disconnectSSE(){ if (es){ es.close(); es = null; } }

// ====== Navegación ======
function goChat(){
  $("userChip").textContent = `Usuario: ${username}`;
  $("peerChip").textContent = `Peer: ${apiBase}`;
  show("chatView");
  apiMessages().catch(console.warn).finally(connectSSE);
}
function goLogin(){
  disconnectSSE();
  token = null; username = null; apiBase = null; afterId = 0;
  seenIds.clear();
  $("messages").innerHTML = "";
  $("fileHint").style.display = "none";
  $("fileName").textContent = "";
  pendingFile = null;
  show("loginView");
}

// ====== Handlers ======
$("btnLogin").onclick = async () => {
  const user = $("username").value.trim();
  const pass = $("password").value.trim();
  if (!user || !pass) { alert("Completa usuario y contraseña"); return; }
  try{
    const data = await apiLogin(user, pass);
    token = data.access_token;
    username = data.user || user;
    apiBase = data.peer_http_base;
    goChat();
  }catch(e){
    console.error(e);
    alert("Login falló: " + (e.message || "Error"));
  }
};

$("btnAttach").onclick = () => $("fileInput").click();

$("fileInput").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  pendingFile = f || null;
  if (pendingFile) {
    $("fileName").textContent = pendingFile.name;
    $("fileHint").style.display = "block";
  } else {
    $("fileName").textContent = "";
    $("fileHint").style.display = "none";
  }
});

$("btnSend").onclick = async () => {
  const input = $("text");
  const text = input.value.trim();
  if (!token) return;

  try{
    if (pendingFile) {
      const info = await apiUpload(pendingFile);
      const payload = { type: "file", url: info.url, name: info.name, size: info.size, mime: info.mime };
      await apiSend(JSON.stringify(payload));
      logMsg({ id: null, author: username, text: JSON.stringify(payload) }, true);
      pendingFile = null; $("fileName").textContent = ""; $("fileHint").style.display = "none"; $("fileInput").value = "";
    }
    if (text) {
      await apiSend(text);
      logMsg({ id: null, author: username, text }, true);
      input.value = ""; input.focus();
    }
  }catch(e){
    console.error(e);
    alert("No se pudo enviar: " + (e.message || "Error"));
  }
};

$("text").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnSend").click(); });
$("btnLogout").onclick = goLogin;

// arranque
show("loginView");
