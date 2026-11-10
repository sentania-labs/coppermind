// Contract tests for the shipped companion: sync boundaries and secret handling.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
class El {
  constructor(tag='',options={}){this.tag=tag;this.text=options.text;this.value=options.value;this.children=[];this.disabled=false;}
  empty(){this.children=[];}
  createEl(tag,options){const el=new El(tag,options);this.children.push(el);return el;}
  createDiv(options){return this.createEl('div',options);}
  all(){return [this,...this.children.flatMap(x=>x.all())];}
}
class Setting {
  constructor(el){this.el=el;}
  setName(){return this;} setDesc(){return this;}
  addText(fn){const control={inputEl:{},setValue(){return control;},onChange(){return control;}};fn(control);return this;}
  addToggle(fn){const toggle=this.el.createEl("toggle");const control={setDisabled(v){toggle.disabled=v;return control;},onChange(fn){toggle.onchange=fn;return control;},setValue(){return control;}};fn(control);return this;}
  addButton(fn){const button=this.el.createEl('button');const control={setButtonText(v){button.text=v;return control;},onClick(fn){button.onclick=fn;return control;}};fn(control);return this;}
}
async function fixture(local, overrides={}){
  const raw='---\nid: test-note\nreviewed: false\n---\nHuman prose.\n';
  const digest='sha256:'+Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(raw))).toString('hex');
  const requests=[];const notices=[];
  const file={basename:'Note',path:'Review/Note.md'};
  let factory;
  class Plugin {registerView(type,fn){factory=fn;}addSettingTab(){}addCommand(){}addRibbonIcon(){}registerEvent(){}async loadData(){return {url:'https://coppermind.example',secret:'local-secret-name'};}}
  class ItemView {constructor(leaf){this.app=leaf.app;this.contentEl=new El();}}
  const app={metadataCache:{getFileCache:()=>({frontmatter:{id:'test-note'}})},secretStorage:{getSecret:()=> 'synthetic-credential'},vault:{read:async()=>local??raw},workspace:{getActiveFile:()=>file,on:()=>null,getActiveViewOfType:()=>null}};
  const context={module:{exports:{}},require:()=>({Plugin,ItemView,Setting,PluginSettingTab:class{},SecretComponent:class{},Notice:class{constructor(text){notices.push(text);}},MarkdownView:class{},requestUrl:async req=>{requests.push(req);if(req.url.endsWith('/v1/schema'))return {status:200,json:{roles:{id_key:'id'}}};if(req.url.includes('/v1/entities?'))return {status:200,json:overrides.peopleResults||{items:[],next_cursor:null}};if(req.url.endsWith('/v1/entities')&&req.method==='POST')return {status:200,json:{id:'new-person',name:JSON.parse(req.body).name,emails:[]}};return {status:200,json:{note:{content_hash:digest,frontmatter:{reviewed:false}},roles:{reviewed_key:'reviewed',type_key:'type'},suggestions:[],unresolved:[],stale:false,state:'suggested',suggestions_enabled:true,job_id:null,...overrides}};}}),crypto:webcrypto,TextEncoder,URL};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../coppermind_admin/obsidian_plugin/main.js'),'utf8'),context);
  const plugin=new context.module.exports();plugin.app=app;await plugin.onload();
  const view=factory({app});await view.refresh();
  return {view,plugin,requests,notices,app,raw};
}
test('unsynced note cannot complete review',async()=>{
  const {view}=await fixture('A local edit that has not synced');
  const buttons=view.contentEl.all().filter(e=>e.tag==='button');
  assert.equal(buttons.find(b=>b.text==='Mark reviewed without changes').disabled,true);
  assert.ok(view.contentEl.all().some(e=>e.text?.includes('have not reached')));
});
test('edit after panel load is refused before any write request',async()=>{
  const f=await fixture();f.app.vault.read=async()=> 'Changed after opening';
  await f.view.contentEl.all().find(e=>e.text==='Mark reviewed without changes').onclick();
  assert.equal(f.requests.filter(r=>r.method==='POST').length,0);
  assert.ok(f.notices.some(n=>n.includes('note changed')));
});
test('credential is read from secret storage, not plugin settings',async()=>{
  const {plugin,requests}=await fixture();
  assert.equal(plugin.settings.secret,'local-secret-name');
  assert.ok(!JSON.stringify(plugin.settings).includes('synthetic-credential'));
  assert.equal(requests[0].headers.Authorization,'Bearer synthetic-credential');
});

test('reviewed flag does not hide interrupted completion recovery',async()=>{
  const f=await fixture(undefined,{note:{frontmatter:{reviewed:true}},completion:{state:'applying',payload:{confirmation:'Complete review'}}});
  const retry=f.view.contentEl.all().find(e=>e.text==='Retry recorded completion');
  assert.ok(retry);
  await retry.onclick();
  assert.equal(f.requests.filter(r=>r.method==='POST').length,1);
});

for(const type of ['work','personal','reference','journal'])test(type+' review shows meeting time only for work',async()=>{
  const f=await fixture(undefined,{note:{frontmatter:{type,reviewed:false}}});
  assert.equal(f.view.contentEl.all().some(e=>e.text==='Meeting time'),type==='work');
});

test('speaker choice is sent with the selected proposal',async()=>{
  const speaker={kind:'speaker',field:'Speaker 2',index:3,value:'contact:a',display_value:'Casey',confidence:.87,reason:'Addressed by name.',quote:'',contact_ids:[],candidates:[{id:'contact:a',name:'Casey',email:'casey@example.test'},{id:'person:b',name:'Alex',email:'alex@example.test'}]};
  const f=await fixture(undefined,{suggestions:[speaker],job_id:'job',suggestions_enabled:true});
  const card=f.view.contentEl.all().find(e=>e.children.some(c=>c.text==='Casey'));
  const choice=card.children.find(c=>c.tag==='select');
  assert.equal(choice.value,'contact:a');
  choice.value='person:b';choice.onchange();
  card.children.find(c=>c.tag==='toggle').onchange(true);
  await f.view.contentEl.all().find(e=>e.text==='Apply selected & mark reviewed').onclick();
  const body=JSON.parse(f.requests.find(r=>r.method==='POST').body);
  assert.deepEqual(body.selected,[3]);
  assert.equal(body.speaker_choices['3'],'person:b');
});

const proposal=(index,confidence,value='person:a')=>({kind:'speaker',field:'Speaker '+(index+1),index,value,display_value:'Alex',confidence,reason:'Named.',quote:'',contact_ids:[],candidates:[{id:'person:a',name:'Alex',email:''},{id:'person:b',name:'Blair',email:''}]});
test('preview shows selected speaker changes and dropdown correction selects the card',async()=>{
  const f=await fixture(undefined,{suggestions:[proposal(0,.9),proposal(1,.4)],job_id:'job'});
  let text=()=>f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text(),/Speaker 1 → Alex/);assert.doesNotMatch(text(),/Speaker 2 → Alex/);
  assert.match(text(),/transcript labels, note attendees and dossier links/);assert.match(text(),/Unnamed speakers stay/);
  const selects=f.view.contentEl.all().filter(e=>e.tag==='select');selects[1].value='person:b';selects[1].onchange();
  assert.match(text(),/Speaker 2 → Blair/);
  await f.view.contentEl.all().find(e=>e.text==='Apply selected & mark reviewed').onclick();
  assert.deepEqual(JSON.parse(f.requests.find(r=>r.method==='POST').body).selected,[0,1]);
});
test('select all suggested names selects low confidence names explicitly',async()=>{
  const f=await fixture(undefined,{suggestions:[proposal(0,.4),proposal(1,.3)],job_id:'job'});
  f.view.contentEl.all().find(e=>e.text==='Select all suggested names').onclick();
  await f.view.contentEl.all().find(e=>e.text==='Apply selected & mark reviewed').onclick();
  assert.deepEqual(JSON.parse(f.requests.find(r=>r.method==='POST').body).selected,[0,1]);
});
test('failed suggestions show cause and retry and still permit manual speaker selection',async()=>{
  const f=await fixture(undefined,{state:'failed',suggestion_error:'The model timed out.',manual_speaker_review:{revision:2,speakers:['Speaker 1'],candidates:[{id:'person:a',name:'Alex'}]}});
  assert.ok(f.view.contentEl.all().some(e=>e.text?.includes('The model timed out.')));
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Retry suggestions'));
  const select=f.view.contentEl.all().find(e=>e.tag==='select');select.value='person:a';select.onchange();
  await f.view.contentEl.all().find(e=>e.text==='Apply selected & mark reviewed').onclick();
  const body=JSON.parse(f.requests.find(r=>r.method==='POST').body);
  assert.equal(body.manual_speakers['Speaker 1'],'person:a');assert.equal(body.manual_source_revision,2);
});
test('empty candidates permit searching People and choosing a result',async()=>{
  const f=await fixture(undefined,{state:'failed',manual_speaker_review:{revision:1,speakers:['Speaker 1'],candidates:[]},peopleResults:{items:[{id:'b',name:'Blair',emails:[]}],next_cursor:null}});
  assert.ok(f.view.contentEl.all().some(e=>e.text?.includes('No people suggested. Enter a name')));
  const input=f.view.contentEl.all().find(e=>e.tag==='input');input.value='Blair';
  await f.view.contentEl.all().find(e=>e.text==='Find a person').onclick();
  const select=f.view.contentEl.all().find(e=>e.tag==='select');select.value='person:b';select.onchange();
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Speaker 1 → Blair'));
});

test('a person can be added and selected when there are no candidates',async()=>{
  const f=await fixture(undefined,{state:'failed',manual_speaker_review:{source_id:'src',revision:1,speakers:['Speaker 1'],candidates:[]}});
  f.view.contentEl.all().find(e=>e.tag==='input').value='Jamie Lee';
  await f.view.contentEl.all().find(e=>e.text==='Add person').onclick();
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Speaker 1 → Jamie Lee'));
  await f.view.contentEl.all().find(e=>e.text==='Apply selected & mark reviewed').onclick();
  const review=f.requests.find(r=>r.url.endsWith('/review')&&r.method==='POST');
  assert.equal(JSON.parse(review.body).manual_speakers['Speaker 1'],'person:new-person');
});
