export const state = {session: JSON.parse(sessionStorage.getItem('demo-session') || 'null'), spec:null,
  transferSpec:null,
  cases:[], runs:[], evidence:[], audit:[], transfers:[], draft:null};

export function setSession(session) {
  state.session = session;
  state.spec = null;
  state.transferSpec = null;
  state.draft = null;
  if (session) sessionStorage.setItem('demo-session', JSON.stringify(session));
  else sessionStorage.removeItem('demo-session');
}
