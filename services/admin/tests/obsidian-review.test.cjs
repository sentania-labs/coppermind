// Contract tests for the shipped companion: sync boundaries and secret handling.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
class El {
  constructor(tag='',options={}){this.tag=tag;this.text=options.text;this.children=[];this.disabled=false;}
  empty(){this.children=[];}
  createEl(tag,options){const el=new El(tag,options);this.children.push(el);return el;}
  createDiv(options){return this.createEl('div',options);}
  all(){return [this,...this.children.flatMap(x=>x.all())];}
}
class Setting {
  constructor(el){this.el=el;}
  setName(){return this;} setDesc(){return this;}
  addText(fn){const control={inputEl:{},setValue(){return control;},onChange(){return control;}};fn(control);return this;}
  addToggle(fn){const control={setDisabled(){return control;},onChange(){return control;},setValue(){return control;}};fn(control);return this;}
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
  const context={module:{exports:{}},require:()=>({Plugin,ItemView,Setting,PluginSettingTab:class{},SecretComponent:class{},Notice:class{constructor(text){notices.push(text);}},MarkdownView:class{},requestUrl:async req=>{requests.push(req);if(req.url.endsWith('/v1/schema'))return {status:200,json:{roles:{id_key:'id'}}};return {status:200,json:{note:{content_hash:digest,frontmatter:{reviewed:false}},roles:{reviewed_key:'reviewed',type_key:'type'},suggestions:[],unresolved:[],stale:false,job_id:null,...overrides}};}}),crypto:webcrypto,TextEncoder,URL};
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
