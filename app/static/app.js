'use strict';
const $ = (s) => document.querySelector(s);
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const initData = tg?.initData || '';
let state = null;
const euros = (c) => new Intl.NumberFormat('fr-FR', {style:'currency',currency:'EUR'}).format(c / 100);
const dateTime = (t) => t ? new Date(t * 1000).toLocaleString('fr-FR') : 'Pas encore vérifié';
const duration = (s) => s < 60 ? `${s} s` : `${s/60} min`;
const e = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
function notice(message, error=false) { $('#notice').textContent=message; $('#notice').className='notice'+(error?' error':''); }
async function api(path, method='GET', data) {
  const response = await fetch('/api/'+path, {method, headers:{'X-Telegram-Init-Data':initData,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});
  if (!response.ok) { const body = await response.json(); throw new Error(body.error || 'Erreur de connexion.'); }
  return response;
}
function button(text, action, cls='secondary') { const b=e('button',text,cls); b.type='button'; b.addEventListener('click',async()=>{b.disabled=true;try{await action();}catch(err){notice(err.message,true);}finally{b.disabled=false;}}); return b; }
function link(url, label='Voir sur Vinted') { const a=e('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a; }
function empty(container, text) { container.append(e('div',text,'empty')); }
function badge(text, cls='') { return e('span',text,'badge '+cls); }
function card(title, status) { const box=e('article',undefined,'card'); const top=e('div',undefined,'card-top');top.append(e('h3',title),status);box.append(top);return box; }
async function remove(table,id) { if (!confirm('Supprimer définitivement cet élément ?')) return;await api(`${table}/${id}`,'DELETE');await refresh(); }
function render() {
  const s=state.status;
  let status=!s.collector_enabled ? 'Collecte désactivée. Vos filtres sont enregistrés ; aucune requête Vinted n’est envoyée.' : s.halted ? 'Collecte arrêtée : '+s.halted : s.error ? 'Suivi ralenti : '+s.error : 'Suivi actif. Les annonces détectées sont placées dans la file Telegram.';
  status+=` Limite globale : une requête toutes les ${s.global_gap} secondes au maximum.`;
  $('#collector-status').textContent=status;
  notice(s.telegram_linked?'Espace connecté · Notifications Telegram activées.':'Espace connecté · Envoyez /start au bot pour activer les notifications.');
  const filters=$('#filter-list');filters.replaceChildren();
  if(!state.filters.length) empty(filters,'Aucun filtre pour le moment. Créez votre première recherche.');
  for(const f of state.filters){
    const box=card(f.name,badge(f.enabled?'Actif':'En pause',f.enabled?'':'paused'));
    const query=new URL(f.url).searchParams.get('search_text');
    if(query) box.append(e('p',query));
    box.append(e('p',`Fréquence souhaitée : ${duration(f.interval)} · ${dateTime(f.last_poll)}`));
    if(f.exclude)box.append(e('p','Exclus : '+f.exclude));
    if(f.error)box.append(e('p',f.error));
    if(!f.initialized)box.append(e('p','En attente du premier relevé de référence.'));
    const actions=e('div',undefined,'actions');actions.append(button('Modifier',()=>openFilter(f)),button(f.enabled?'Mettre en pause':'Reprendre',async()=>{await api('filters/'+f.id,'PUT',{...f,enabled:!f.enabled});await refresh();}),button('Supprimer',()=>remove('filters',f.id),'danger'),link(f.url,'Recherche Vinted'));box.append(actions);filters.append(box);
  }
  const alerts=$('#alert-list');alerts.replaceChildren();
  if(!state.alerts.length)empty(alerts,'Les nouvelles annonces détectées après le premier relevé apparaîtront ici.');
  const delivery={sent:'Envoyée',pending:'En attente',failed:'Échec d’envoi'};
  for(const a of state.alerts){
    const box=card(a.title,badge(delivery[a.state]||a.state,a.state==='failed'?'failed':a.state==='pending'?'paused':''));
    box.append(e('p',a.filter_name+' · '+dateTime(a.created)),e('div',euros(a.price_cents),'price'),e('p',[a.brand,a.size].filter(Boolean).join(' · ')));
    const actions=e('div',undefined,'actions');actions.append(link(a.url),button('Enregistrer un achat',()=>openTrade({title:a.title,url:a.url,purchase_cents:a.price_cents})));box.append(actions);alerts.append(box);
  }
  const metrics=$('#metrics');metrics.replaceChildren();
  const total=state.summary;
  for(const [label,value] of [['Marge réalisée',euros(total.profit_cents)],['Ventes brutes',euros(total.revenue_cents)],['Stock au coût d’achat',euros(total.stock_cents)],['Articles en stock',total.stock_count]]){const box=e('div',undefined,'metric');box.append(e('span',label),e('strong',String(value)));metrics.append(box);}
  const trades=$('#trade-list');trades.replaceChildren();
  if(!state.trades.length)empty(trades,'Ajoutez un achat pour commencer votre suivi. Vous pourrez enregistrer sa vente ensuite.');
  for(const t of state.trades){
    const box=card(t.title,badge(t.sold_on?'Vendu':'En stock',t.sold_on?'':'paused'));
    const cost=t.purchase_cents+t.purchase_fees_cents;
    box.append(e('p',`Achat du ${t.bought_on} · Coût total ${euros(cost)}`));
    if(t.sold_on)box.append(e('p',`Vente du ${t.sold_on} · ${euros(t.sale_cents)}`),e('div',euros(t.sale_cents-t.sale_fees_cents-t.refund_cents-cost)+' de marge','price'));
    if(t.notes)box.append(e('p',t.notes));
    const actions=e('div',undefined,'actions');actions.append(button(t.sold_on?'Modifier':'Enregistrer la vente',()=>openTrade(t)),button('Supprimer',()=>remove('trades',t.id),'danger'));if(t.url)actions.append(link(t.url));box.append(actions);trades.append(box);
  }
}
async function refresh() { if(!initData){notice('Ouvrez cette mini-app avec le bouton « Mon espace » de votre bot Telegram. Aucun accès aux données n’est possible depuis cette page seule.',true);return;}try{state=await(await api('state')).json();render();}catch(err){notice(err.message,true);} }
function fill(form, values) { form.reset();form.querySelector('.form-error').textContent='';for(const [key,value] of Object.entries(values)){const field=form.elements.namedItem(key);if(!field)continue;if(field.type==='checkbox')field.checked=Boolean(value);else field.value=value??'';} }
function openFilter(f={}) {if(!state)throw new Error('Connectez-vous depuis Telegram.');fill($('#filter-form'),f);$('#filter-title').textContent=f.id?'Modifier le filtre':'Nouveau filtre';$('#filter-dialog').showModal();}
function openTrade(t={}) {if(!state)throw new Error('Connectez-vous depuis Telegram.');const now=new Date();const today=new Date(now.getTime()-now.getTimezoneOffset()*60000).toISOString().slice(0,10);const fields={bought_on:today,...t};for(const k of ['purchase','purchase_fees','sale','sale_fees','refund'])fields[k]=((t[k+'_cents']||0)/100).toFixed(2);fill($('#trade-form'),fields);$('#trade-title').textContent=t.id?'Modifier l’article':'Ajouter un achat';$('#trade-dialog').showModal();}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.removeAttribute('aria-current'));b.setAttribute('aria-current','page');document.querySelectorAll('.tab').forEach(x=>x.hidden=x.id!==b.dataset.tab);}));
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.close).close()));
$('#new-filter').addEventListener('click',()=>{try{openFilter();}catch(err){notice(err.message,true);}});
$('#new-trade').addEventListener('click',()=>{try{openTrade();}catch(err){notice(err.message,true);}});
$('#refresh').addEventListener('click',refresh);
for(const type of ['filter','trade']){
  const form=$('#'+type+'-form');form.addEventListener('submit',async event=>{event.preventDefault();const submit=form.querySelector('[type=submit]');submit.disabled=true;const data=Object.fromEntries(new FormData(form));const id=data.id;delete data.id;if(type==='filter'){data.enabled=form.elements.enabled.checked;data.interval=Number(data.interval);}try{await api(type+'s'+(id?'/'+id:''),id?'PUT':'POST',data);$('#'+type+'-dialog').close();await refresh();}catch(err){form.querySelector('.form-error').textContent=err.message;}finally{submit.disabled=false;}});
}
$('#export').addEventListener('click',async()=>{try{const blob=await(await api('export')).blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='vintedbot-compta.csv';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}catch(err){notice(err.message,true);}});
refresh();
setInterval(()=>{if(!document.hidden&&!document.querySelector('dialog[open]'))refresh();},15000);
