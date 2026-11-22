import {request} from './api/client.js';
import {state, setSession} from './stores/session.js';
import {roleLabels} from './constants/workflow.js';
import {topic} from './constants/project.js';
import {loginPage} from './pages/login.js';
import {casesPage} from './pages/cases.js';
import {bindEditor, editorPage} from './pages/editor.js';
import {runsPage} from './pages/runs.js';
import {bindEvidence, evidencePage} from './pages/evidence.js';
import {bindTransfers, transfersPage} from './pages/transfers.js';
import {go, route} from './router/hash.js';
import {submit} from './hooks/submit.js';
import {esc} from './utils/dom.js';

const app = document.getElementById('app');
document.title = topic.name;
let timer;
let revision = 0;
function fail(error) {
  if (error.status===401 && state.session) { setSession(null); void refresh(); return; }
  const message = document.getElementById('message');
  if (message) { message.textContent=error.message; message.className='error'; }
}
const api = (path, options={})=>request(path, {...options, token:state.session?.token});
const pages = {cases:casesPage, editor:editorPage, runs:runsPage, evidence:evidencePage, transfers:transfersPage};
async function refresh() {
  clearTimeout(timer);
  const key = ++revision;
  if (!state.session) {
    app.innerHTML=loginPage();
    submit('login-form', async data=>{
      setSession(await api('/login', {method:'POST', data:Object.fromEntries(data)}));
      if (location.hash!=='#cases') go('cases');
      else await refresh();
    }, fail);
    return;
  }
  const {key: page,id} = route();
  if (!pages[page]) { go('cases'); return; }
  try {
    const result = await Promise.all([
      state.spec || api('/spec'),
      state.transferSpec || api('/transfer-spec'),
      api('/cases'), api('/runs'), api('/evidence'), api('/audit'), api('/transfers')]);
    if (revision !== key) return;
    [state.spec, state.transferSpec, state.cases, state.runs, state.evidence, state.audit, state.transfers]=result;
  } catch (error) {
    if (revision !== key) return;
    if (!document.getElementById('message')) app.innerHTML='<main><div id="message" role="alert"></div><button id="retry">重试</button></main>';
    document.getElementById('retry')?.addEventListener('click', refresh);
    fail(error);
    return;
  }
  // 编辑器仅经办员可见；其余模块按角色全部开放导航。
  const visibleModules = topic.modules.filter((_, index) => !(index===1 && state.session.role!=='operator'));
  const pageKeys = Object.keys(pages).filter(key => !(key==='editor' && state.session.role!=='operator'));
  const navigation = visibleModules.map((label,index)=>{
    const key = pageKeys[index];
    return `<a href="#${key}" ${key===page?'aria-current="page"':''}>${esc(label)}</a>`;
  }).join('');
  app.innerHTML=`<header class="masthead"><a class="identity" href="#cases">${esc(topic.name)}</a><div class="user"><span>${esc(roleLabels[state.session.role])}</span><button id="logout" class="secondary">退出</button></div></header>
    <div class="workspace"><nav aria-label="业务模块">${navigation}</nav><main><div id="message" role="alert"></div>${pages[page](state,id)}</main></div>`;
  document.getElementById('logout').addEventListener('click',()=>{setSession(null); void refresh();});
  if (page==='editor') bindEditor(state, api, fail);
  if (page==='evidence') bindEvidence(api, refresh, fail);
  if (page==='transfers') {
    bindTransfers(state, api, fail, refresh);
    if (state.transfers.some(row=>['queued','running','prepared'].includes(row.status))) timer=setTimeout(refresh,1000);
  }
  if (page==='runs') {
    document.getElementById('refresh').addEventListener('click', refresh);
    if (state.runs.some(row=>['queued','running'].includes(row.status))) timer=setTimeout(refresh,1000);
  }
}
window.addEventListener('hashchange', refresh);
void refresh();
