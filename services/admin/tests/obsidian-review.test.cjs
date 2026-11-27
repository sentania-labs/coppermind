// Contract tests for the shipped companion: the Review screen, pairing and secret handling.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
class El {
  constructor(tag='',options={}){this.tag=tag;this.cls=options.cls;this.attr=options.attr;this.text=options.text;this.value=options.value;this.children=[];this.disabled=false;this.style={};}
  empty(){this.children=[];}
  setText(text){this.text=text;}
  addClass(){}
  querySelector(){return null;}
  createEl(tag,options){const el=new El(tag,options);el.parent=this;this.children.push(el);return el;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
  createDiv(options){return this.createEl('div',options);}
  createSpan(options){return this.createEl('span',options);}
  all(){return [this,...this.children.flatMap(x=>x.all())];}
  querySelectorAll(selectors){const tags=selectors.split(',').map(s=>s.trim());return this.all().filter(e=>tags.includes(e.tag));}
  setText(text){this.text=text;}
}
class Setting {
  constructor(el){this.el=el;}
  setName(){return this;} setDesc(value){this.el.createEl('p',{text:value});return this;}
  addComponent(fn){fn(this.el);return this;}
  addText(fn){const control={inputEl:{},setValue(){return control;},onChange(){return control;}};fn(control);return this;}
  addToggle(fn){const toggle=this.el.createEl("toggle");const control={setDisabled(v){toggle.disabled=v;return control;},onChange(fn){toggle.onchange=fn;return control;},setValue(){return control;}};fn(control);return this;}
  addButton(fn){const button=this.el.createEl('button');const control={setButtonText(v){button.text=v;return control;},setCta(){return control;},setDisabled(v){button.disabled=v;return control;},onClick(fn){button.onclick=fn;return control;}};fn(control);return this;}
  addDropdown(fn){const dropdown=this.el.createEl('select');const control={addOption(v,text){dropdown.createEl('option',{value:v,text});return control;},setValue(v){dropdown.value=v;return control;},onChange(fn){dropdown.onchange=()=>fn(dropdown.value);return control;}};fn(control);return this;}
}
async function fixture(local, overrides={}){
  const requests=[];const notices=[];const writes=[];const opened=[];const detached=[];const changed=[];
  const files=overrides.files||{};
  let factory;const factories={};const modals=[];
  class Plugin {registerView(type,fn){factory=fn;factories[type]=fn;}addSettingTab(tab){this.settingTab=tab;}addCommand(command){(this.commands??={})[command.id]=command;}async saveData(data){(this.saved??=[]).push(JSON.parse(JSON.stringify(data)));}addRibbonIcon(icon,title){(this.ribbons??=[]).push(title);}registerEvent(){}async loadData(){return {url:'https://coppermind.example',secret:'local-secret-name'};}}
  class ItemView {constructor(leaf){this.app=leaf.app;this.contentEl=new El();}registerEvent(){}}
  const leaf={app:null,async setViewState(state){this.view=factories[state.type]({app});}};
  const app={
    metadataCache:{getFileCache:file=>files[file.path]?{frontmatter:files[file.path]}:null,on:(name,fn)=>{changed.push(fn);return {};}},
    fileManager:{async processFrontMatter(file,fn){if(overrides.processFails)throw Error('disk');const fm=files[file.path];fn(fm);writes.push({path:file.path,fm:JSON.parse(JSON.stringify(fm))});}},
    secretStorage:{getSecret:()=> 'synthetic-credential'},
    vault:{getAbstractFileByPath:path=>files[path]?{path,basename:path.split('/').pop().replace(/\.md$/,'')}:null,read:async file=>(overrides.texts||{})[file.path]??''},
    workspace:{getActiveFile:()=>null,on:()=>null,getLeavesOfType:()=>[],getRightLeaf:()=>leaf,revealLeaf:async()=>{},detachLeavesOfType:type=>detached.push(type),getLeaf:()=>({openFile:async file=>opened.push(file.path)})}};
  leaf.app=app;
  const context={module:{exports:{}},require:()=>({Plugin,ItemView,Setting,Modal:class{constructor(app){this.app=app;this.contentEl=new El();}open(){modals.push(this);this.onOpen();}close(){this.onClose();}},PluginSettingTab:class{constructor(){this.containerEl=new El();}},MarkdownView:class{},SecretComponent:class{setValue(){return this;}onChange(){return this;}},Notice:class{constructor(text){notices.push(text);}},requestUrl:async req=>{requests.push(req);const url=new URL(req.url);
    const routed=overrides.routes&&await overrides.routes(req,url);if(routed)return routed;
    if(req.url.endsWith('/v1/pair')&&overrides.pair)return overrides.pair(req);
    if(req.url.endsWith('/v1/anything')&&overrides.page)return overrides.page(req);
    if(req.url.endsWith('/v1/entities/voice/recognition'))return {status:200,json:overrides.recognition||{enabled:true,attendees_field:'attendees'}};
    if(req.url.endsWith('/v1/entities/voice/unknowns'))return {status:200,json:overrides.unknownVoices||{}};
    if(req.url.endsWith('/v1/entities/voice/people'))return {status:200,json:overrides.savedPeople||{}};
    if(req.url.includes('/v1/entities?'))return overrides.rejectPeople?{status:overrides.rejectPeople===true?403:overrides.rejectPeople,json:{error:'forbidden'}}:{status:200,json:typeof overrides.peopleResults==='function'?await overrides.peopleResults(req):overrides.peopleResults||{items:[],next_cursor:null}};
    return {status:200,json:{...overrides}};}}),crypto:webcrypto,TextEncoder,URL,
    // A long timer (the screen's poll) is one the test process does not wait for.
    setTimeout:(fn,ms)=>{const t=setTimeout(fn,ms);if(ms>=1000)t.unref?.();return t;},clearTimeout,setInterval,clearInterval};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../coppermind_admin/obsidian_plugin/main.js'),'utf8'),context);
  const plugin=new context.module.exports();plugin.app=app;await plugin.onload();
  return {plugin,requests,notices,app,factories,modals,files,writes,opened,detached,changed,leaf,rules:context.module.exports.rules};
}

test('credential is read from secret storage, not plugin settings',async()=>{
  const {plugin,requests}=await fixture();
  await plugin.call('/v1/schema');
  assert.equal(plugin.settings.secret,'local-secret-name');
  assert.ok(!JSON.stringify(plugin.settings).includes('synthetic-credential'));
  assert.equal(requests[0].headers.Authorization,'Bearer synthetic-credential');
});
test('companion settings offer pairing first and name the key-by-hand path',async()=>{
  const f=await fixture();f.plugin.settingTab.display();
  const text=f.plugin.settingTab.containerEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/Paired with Coppermind/);assert.match(text,/Pair again/);
  assert.match(text,/create a new key with: Read notes, Add and change notes, Hand in recordings and files, Search, and Read and correct people and companies\. Select that key here/);
  assert.doesNotMatch(text,/entities:read|sources:write|change its permissions|API Keys/);
});

test('pairing trades the six digit code for a key kept in secret storage only',async()=>{
  const f=await fixture();let stored=null;
  f.app.secretStorage.setSecret=(name,value)=>{stored=[name,value];};
  await assert.rejects(f.plugin.pair('12 34'),/six-digit pairing code/);
  assert.equal(f.requests.filter(r=>r.url.endsWith('/v1/pair')).length,0);
  const before=f.requests.length;
  await assert.rejects(f.plugin.pair('123 456'),/did not answer like Coppermind|not accepted/);
  const sent=f.requests[before];
  assert.equal(sent.url,'https://coppermind.example/v1/pair');assert.equal(sent.method,'POST');
  assert.deepEqual(Object.keys(JSON.parse(sent.body)).sort(),['code','device']);
  assert.equal(JSON.parse(sent.body).code,'123456');
  assert.equal(sent.headers.Authorization,undefined);
  assert.equal(stored,null);
});

const webPage=()=>({status:404,get json(){throw new SyntaxError("Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON");},text:'<!DOCTYPE html><html><body>404 Not Found</body></html>'});
test('an address that answers with a web page is told in plain words, with the raw error behind details',async()=>{
  const f=await fixture(undefined,{pair:webPage});
  const stored=[];f.app.secretStorage.setSecret=(name,value)=>stored.push([name,value]);
  const error=await f.plugin.pair('123 456').then(()=>null,e=>e);
  assert.match(error.message,/did not answer like Coppermind/);
  assert.doesNotMatch(error.message,/SyntaxError|Unexpected token|DOCTYPE/);
  assert.match(error.details,/HTTP 404: <!DOCTYPE html>/);
  assert.deepEqual(stored,[]);
  f.plugin.commands['pair-device'].callback();
  const modal=f.modals.at(-1);
  const code=modal.contentEl.all().find(e=>e.tag==='input'&&e.attr?.inputmode==='numeric');
  code.value='123 456';
  await modal.contentEl.all().find(e=>e.tag==='button'&&e.text==='Pair this device').onclick();
  const shown=modal.contentEl.all();
  assert.match(shown.find(e=>e.tag==='p'&&e.cls==='coppermind-notice').text,/did not answer like Coppermind/);
  const details=shown.find(e=>e.tag==='details');
  assert.equal(details.children[0].text,'Details for support');
  assert.match(details.children[1].text,/HTTP 404/);
});

test('a page that is not Coppermind during a normal request is also told in plain words',async()=>{
  const f=await fixture(undefined,{page:webPage});
  const error=await f.plugin.call('/v1/anything').then(()=>null,e=>e);
  assert.match(error.message,/did not answer like Coppermind|could not complete the request/);
  assert.doesNotMatch(error.message,/SyntaxError|Unexpected token|DOCTYPE/);
  assert.match(error.details,/HTTP 404/);
  assert.equal(error.status,404);
});

test('a successful pairing keeps the key in secret storage only and uses it for later requests',async()=>{
  const credential='synthetic-paired-credential';
  const f=await fixture(undefined,{pair:()=>({status:200,json:{credential,name:'Obsidian companion on Mac'}})});
  const vault={};f.app.secretStorage.setSecret=(name,value)=>{vault[name]=value;};
  f.app.secretStorage.getSecret=name=>vault[name];
  assert.equal(f.plugin.paired(),false);
  assert.equal(await f.plugin.pair('123 456'),'Obsidian companion on Mac');
  assert.deepEqual(vault,{'coppermind-companion':credential});
  assert.equal(f.plugin.settings.secret,'coppermind-companion');
  assert.equal(f.plugin.paired(),true);
  assert.ok(!JSON.stringify(f.plugin.settings).includes(credential));
  assert.ok(f.plugin.saved.length>0&&f.plugin.saved.every(blob=>!JSON.stringify(blob).includes(credential)));
  f.requests.length=0;
  await f.plugin.call('/v1/schema');
  assert.equal(f.requests[0].headers.Authorization,'Bearer '+credential);
});

const personVoice=(signatures)=>({enabled:true,signatures});
const sig=(id)=>({note_id:id,created_at:'2026-01-02T00:00:00Z'});
async function peopleFixture(signatures){
  const f=await fixture(undefined,personVoice(signatures));
  const view=f.factories['coppermind-people']({app:f.app});
  await view.show({id:'alex-id',name:'Alex'});
  const toggle=view.contentEl.all().find(e=>e.tag==='toggle');
  const puts=()=>f.requests.filter(r=>r.method==='PUT');
  return {...f,view,toggle,puts};
}
test('the global switch stops collecting signatures without deleting saved voices',async()=>{
  const f=await peopleFixture([sig('n1')]);
  await f.toggle.onchange(false);
  assert.equal(f.puts().length,1);
  assert.match(f.puts()[0].url,/\/v1\/entities\/voice\/recognition$/);
  assert.deepEqual(JSON.parse(f.puts()[0].body),{enabled:false});
  assert.equal(f.modals.length,0);
});
test('deleting a person voice requires confirmation',async()=>{
  const f=await peopleFixture([sig('n1')]);
  f.view.contentEl.all().find(e=>e.text==='Delete voice data').onclick();
  assert.equal(f.requests.filter(r=>r.method==='DELETE').length,0);
  const modal=f.modals.at(-1);
  assert.match(modal.contentEl.all().map(e=>e.text||'').join(' '),/Future recordings cannot be recognised/);
  await modal.contentEl.all().find(e=>e.text==='Delete voice data').onclick();
  assert.equal(f.requests.filter(r=>r.method==='DELETE').length,1);
});


test('People shows saved people and confirms how many recordings a voice combines',async()=>{
  const unknownVoices={'unknown:12':{name:'Unknown voice 12',signatures:[sig('n1')],assignments:[{note_id:'n1',label:'Speaker 1'},{note_id:'n2',label:'Speaker 1'}]},'unknown:18':{name:'Unknown voice 18',signatures:[sig('n3')],assignments:[]}};
  const f=await fixture(undefined,{unknownVoices,savedPeople:{alex:{name:'Alex',signatures:[sig('n4')],assignments:[]}}});
  const view=f.factories['coppermind-people']({app:f.app});await view.show();
  assert.ok(view.contentEl.all().some(e=>e.text==='Alex · Voice data saved'));
  await view.show({id:'unknown:12',name:'Unknown voice 12',unknown:true});
  const select=view.contentEl.all().find(e=>e.tag==='select');select.value='unknown:18';
  view.contentEl.all().find(e=>e.text==='Choose and combine').onclick();
  assert.equal(f.requests.filter(r=>r.method==='POST').length,0);
  const modal=f.modals.at(-1);assert.ok(modal.contentEl.all().some(e=>e.text?.includes('Unknown voice 12 will become Unknown voice 18 in 2 recordings')));
  await modal.contentEl.all().find(e=>e.text==='Combine voices').onclick();
  assert.equal(f.requests.filter(r=>r.method==='POST').length,1);
});

test('People find-a-person shows results once, tells what will happen, and confirms it',async()=>{
  const unknownVoices={'unknown:7':{name:'Unknown voice 7',signatures:[sig('n1')],assignments:[{note_id:'n1',label:'Speaker 1'},{note_id:'n2',label:'Speaker 1'}]}};
  const f=await fixture(undefined,{unknownVoices,peopleResults:{items:[{id:'p1',name:'Scott Bowe',emails:['scott@a.test']},{id:'p2',name:'Scott Bowe',emails:['scott@b.test']},{id:'p1',name:'Scott Bowe',emails:['scott@a.test']}],next_cursor:null}});
  const view=f.factories['coppermind-people']({app:f.app});await view.show({id:'unknown:7',name:'Unknown voice 7',unknown:true});
  const all=()=>view.contentEl.all();const lookup=all().find(e=>e.text==='Find a person'&&e.tag==='button');
  all().find(e=>e.tag==='input'&&e.attr?.placeholder?.startsWith('Type a name')).value='Scott';
  await lookup.onclick();await lookup.onclick();await lookup.onclick();
  const options=all().filter(e=>e.tag==='option').map(e=>e.text);
  assert.deepEqual(options.filter(t=>t.startsWith('Scott')),['Scott Bowe (scott@a.test)','Scott Bowe (scott@b.test)']);
  assert.ok(all().some(e=>e.text==='Found 2 people. Choose one from the list below.'));
  const select=all().find(e=>e.tag==='select');select.value='person:p1';select.onchange();
  assert.ok(all().some(e=>e.text==='Unknown voice 7 will become Scott Bowe (scott@a.test) in 2 recordings.'));
  all().find(e=>e.text==='Choose and combine').onclick();
  const modal=f.modals.at(-1);assert.ok(modal.contentEl.all().some(e=>e.text?.includes('Unknown voice 7 will become Scott Bowe (scott@a.test) in 2 recordings')));
  await modal.contentEl.all().find(e=>e.text==='Combine voices').onclick();
  assert.ok(all().some(e=>e.text?.startsWith('Done: Unknown voice 7 is now Scott Bowe')));
});

test('typing a name narrows matching people without clicking Find, and says when nobody matches',async()=>{
  const unknownVoices={'unknown:7':{name:'Unknown voice 7',signatures:[sig('n1')],assignments:[]}};
  const peopleResults=req=>{const q=decodeURIComponent(req.url.split('q=')[1]);return {items:q==='Sc'?[{id:'p1',name:'Scott Bowe',emails:[]}]:[],next_cursor:null};};
  const f=await fixture(undefined,{unknownVoices,peopleResults});
  const view=f.factories['coppermind-people']({app:f.app});await view.show({id:'unknown:7',name:'Unknown voice 7',unknown:true});
  const all=()=>view.contentEl.all();const input=all().find(e=>e.tag==='input'&&e.attr?.placeholder?.startsWith('Type a name'));
  input.value='Sc';input.oninput();await new Promise(r=>setTimeout(r,400));
  assert.ok(all().some(e=>e.tag==='option'&&e.text==='Scott Bowe'));
  input.value='Zz';input.oninput();await new Promise(r=>setTimeout(r,400));
  assert.ok(all().some(e=>e.text==='No one named Zz found.'));assert.ok(!all().some(e=>e.tag==='option'&&e.text==='Scott Bowe'));
});

test('an older person search answering last does not replace the newer results',async()=>{
  const unknownVoices={'unknown:7':{name:'Unknown voice 7',signatures:[sig('n1')],assignments:[]}};
  const release={};
  const peopleResults=req=>new Promise(resolve=>{const q=decodeURIComponent(req.url.split('q=')[1]);release[q]=()=>resolve({items:[{id:q==='Al'?'old':'new',name:q==='Al'?'Alan Old':'Alex New',emails:[]}],next_cursor:null});});
  const f=await fixture(undefined,{unknownVoices,peopleResults});
  const view=f.factories['coppermind-people']({app:f.app});await view.show({id:'unknown:7',name:'Unknown voice 7',unknown:true});
  const all=()=>view.contentEl.all();const input=all().find(e=>e.tag==='input'&&e.attr?.placeholder?.startsWith('Type a name'));
  const lookup=all().find(e=>e.text==='Find a person'&&e.tag==='button');
  input.value='Al';const first=lookup.onclick();input.value='Alex';const second=lookup.onclick();
  await new Promise(r=>setTimeout(r,0));
  release.Alex();await second;release.Al();await first;
  assert.deepEqual(all().filter(e=>e.tag==='option').map(e=>e.text).filter(t=>t.includes('A')&&t!=='Choose a voice or person'),['Alex New']);
  assert.equal(all().find(e=>e.tag==='select').value,'person:new');
});

test('people who attended the voice\'s meetings are listed first, marked, and a listed match can be chosen',async()=>{
  const unknownVoices={'unknown:7':{name:'Unknown voice 7',signatures:[sig('n1')],assignments:[{note_id:'n1',label:'Speaker 1'}]}};
  const items=[{id:'a',name:'Sam Early',emails:['sam@a.test'],path:'People/Sam Early.md'},{id:'b',name:'Sam Linked',emails:['x@b.test'],path:'People/Sam Linked.md'},{id:'c',name:'Sam Mailed',emails:['mail@c.test'],path:'People/Sam Mailed.md'},{id:'d',name:'Sam Last',emails:[],path:'People/Sam Last.md'}];
  const f=await fixture(undefined,{unknownVoices,title:'Planning',path:'Planning.md',frontmatter:{attendees:['[[People/Sam Linked]]','Mail@C.test',7]},peopleResults:{items,next_cursor:null}});
  const view=f.factories['coppermind-people']({app:f.app});await view.show({id:'unknown:7',name:'Unknown voice 7',unknown:true});
  const all=()=>view.contentEl.all();const input=all().find(e=>e.tag==='input'&&e.attr?.placeholder?.startsWith('Type a name'));
  input.value='Sam';input.oninput();await new Promise(r=>setTimeout(r,400));
  const listed=all().filter(e=>e.tag==='button'&&e.text?.startsWith('Sam')).map(e=>e.text);
  assert.deepEqual(listed,['Sam Linked (x@b.test) · attended these meetings','Sam Mailed (mail@c.test) · attended these meetings','Sam Early (sam@a.test)','Sam Last']);
  all().find(e=>e.text==='Sam Early (sam@a.test)'&&e.tag==='button').onclick();
  assert.equal(all().find(e=>e.tag==='select').value,'person:a');
  assert.ok(all().some(e=>e.text==='Unknown voice 7 will become Sam Early (sam@a.test) in 1 recording.'));
  input.value='Samu';input.oninput();
  assert.ok(!all().some(e=>e.tag==='button'&&e.text?.startsWith('Sam ')));
});

test('attendee-first ordering reads the configured attendees property, not a fixed name',async()=>{
  const unknownVoices={'unknown:7':{name:'Unknown voice 7',signatures:[sig('n1')],assignments:[{note_id:'n1',label:'Speaker 1'}]}};
  const items=[{id:'a',name:'Sam Early',emails:['sam@a.test'],path:'People/Sam Early.md'},{id:'b',name:'Sam Mailed',emails:['mail@c.test'],path:'People/Sam Mailed.md'}];
  const f=await fixture(undefined,{unknownVoices,recognition:{enabled:true,attendees_field:'present'},title:'Planning',path:'Planning.md',frontmatter:{present:['mail@c.test'],attendees:['sam@a.test']},peopleResults:{items,next_cursor:null}});
  const view=f.factories['coppermind-people']({app:f.app});await view.show({id:'unknown:7',name:'Unknown voice 7',unknown:true});
  const all=()=>view.contentEl.all();const input=all().find(e=>e.tag==='input'&&e.attr?.placeholder?.startsWith('Type a name'));
  input.value='Sam';input.oninput();await new Promise(r=>setTimeout(r,400));
  assert.deepEqual(all().filter(e=>e.tag==='button'&&e.text?.startsWith('Sam')).map(e=>e.text),['Sam Mailed (mail@c.test) · attended these meetings','Sam Early (sam@a.test)']);
});

test('typing on while an earlier search is in flight never shows its results for the shorter entry',async()=>{
  const unknownVoices={'unknown:7':{name:'Unknown voice 7',signatures:[sig('n1')],assignments:[]}};
  const release={};
  const peopleResults=req=>new Promise((resolve,reject)=>{const q=decodeURIComponent(req.url.split('q=')[1]);release[q]={ok:()=>resolve({items:[{id:'s',name:'Sam Short',emails:[]}],next_cursor:null}),fail:()=>reject(new Error('down'))};});
  const f=await fixture(undefined,{unknownVoices,peopleResults});
  const view=f.factories['coppermind-people']({app:f.app});await view.show({id:'unknown:7',name:'Unknown voice 7',unknown:true});
  const all=()=>view.contentEl.all();const input=all().find(e=>e.tag==='input'&&e.attr?.placeholder?.startsWith('Type a name'));
  input.value='S';input.oninput();await new Promise(r=>setTimeout(r,400));
  input.value='Sc';input.oninput();
  release.S.ok();await new Promise(r=>setTimeout(r,0));
  assert.ok(!all().some(e=>e.tag==='option'&&e.text==='Sam Short'));
  assert.equal(all().find(e=>e.tag==='select').value,'');
  assert.ok(all().some(e=>e.text==='Searching…'));
  await new Promise(r=>setTimeout(r,400));release.Sc.fail();await new Promise(r=>setTimeout(r,0));
  assert.ok(!all().some(e=>e.text==='Searching…'));
  assert.ok(!all().some(e=>e.tag==='option'&&e.text==='Sam Short'));
});

test('a read the server never answers is cut off with a plain error',async()=>{
  const f=await fixture(undefined,{page:()=>new Promise(()=>{})});
  f.plugin.requestTimeoutMs=20;
  await assert.rejects(f.plugin.call('/v1/anything','GET',undefined,true),e=>e.timeout===true&&/did not answer in time/.test(e.message));
});

test('a write that is slow to answer is waited for, not cut off',async()=>{
  const f=await fixture(undefined,{page:()=>new Promise(resolve=>setTimeout(()=>resolve({status:200,json:{done:true}}),60))});
  f.plugin.requestTimeoutMs=20;
  assert.deepEqual(await f.plugin.call('/v1/anything','POST',{}),{done:true});
});
test('the pairing dialog points to the page that shows the code',async()=>{
  const f=await fixture();
  f.plugin.commands['pair-device'].callback();
  const text=f.modals.at(-1).contentEl.all().filter(e=>e.tag==='p').map(e=>e.text).join(' ');
  assert.match(text,/Add the companion to Obsidian/);
  assert.doesNotMatch(text,/Obsidian Sync|Pair companion/);
});


// ---- the Review screen ----------------------------------------------------------------
const BASE='https://coppermind.example';
const CHOICES={keys:{type:'type',account:'account',tags:'tags',reviewed:'reviewed',meeting:'calendar_event',attendees:'attendees'},types:['work','personal','reference'],accounts:['Acme','Contoso'],tags:['vsphere','vsan'],tags_closed:false};
const MEETINGS=[{event_id:'a',title:'Acme architecture review',start:'2026-11-26T10:00:00-06:00',end:'2026-11-26T11:00:00-06:00'},{event_id:'b',title:'Pipeline review',start:'2026-11-26T11:30:00-06:00',end:'2026-11-26T12:00:00-06:00'}];
const written={state:'written',reason:'',when:''};
const note=(id,title,over={})=>({id,title,path:'Review/'+title+'.md',date:'2026-11-26T10:02:00-06:00',from_recording:true,can_resummarize:true,takes_model:true,writeup:written,edited:false,meeting:{selected:'a',candidates:MEETINGS},...over});
const frontmatter=(over={})=>({type:'reference',account:[],tags:[],calendar_event:'a',attendees:[],reviewed:false,...over});
const ok=json=>({status:200,json});
// A server that answers the three reads the screen makes, from state the test can change.
async function review(state={}){
  const s={notes:[],failed:[],processing:[],models:{default:'gpt-default',models:['gpt-default','claude-opus-5-5'],problem:''},files:{},...state};
  for(const n of s.notes)s.files[n.path]??=frontmatter();
  const calls=[];
  const f=await fixture(undefined,{files:s.files,texts:s.texts,routes:async(req,url)=>{
    const p=url.pathname;
    if(s.respond){const answered=await s.respond(req,p);if(answered)return answered;}
    if(p==='/v1/recordings/waiting')return ok({meetings:[],failed:s.failed,processing:s.processing,missing_summaries:[]});
    if(p==='/v1/review/notes')return ok({notes:s.notes,total:s.notes.length,next_cursor:null,writeup_on:true,choices:CHOICES});
    if(p==='/v1/review/models')return s.models===false?{status:500,json:{message:'down'}}:ok(s.models);
    if(req.method==='POST'){calls.push({path:p,body:req.body?JSON.parse(req.body):null});return ok({started:true,already:false,filed:true,reopened:true,meeting:'x'});}
    if(p.startsWith('/v1/notes/')&&req.method==='GET')return ok({path:s.filedPath||'work/2026/2026-11/Note.md'});
    return null;}});
  f.plugin.pollMs=1e9;f.plugin.busyPollMs=1e9;
  const view=f.factories['coppermind-review']({app:f.app});await view.onOpen();
  const texts=()=>view.contentEl.all().map(e=>e.text).filter(Boolean);
  const button=text=>view.contentEl.all().find(e=>e.tag==='button'&&e.text===text);
  const buttons=text=>view.contentEl.all().filter(e=>e.tag==='button'&&e.text===text);
  return {...f,s,view,calls,texts,button,buttons};
}
const plain=x=>JSON.parse(JSON.stringify(x));
const posted=f=>f.calls.map(c=>c.path);
const BANNED=/needs you|needs attention|enrich|suggestion|stale|notes in review|last updated|compare with|resummarize all|earlier version/i;

test('one command and one ribbon icon open Review, and the older screens are let go',async()=>{
  const f=await fixture(undefined,{});
  assert.deepEqual(Object.keys(f.plugin.commands).sort(),['manage-people','pair-device','review']);
  assert.equal(f.plugin.commands.review.name,'Review');
  assert.deepEqual(f.plugin.ribbons,['Review']);
  assert.deepEqual(Object.keys(f.factories).sort(),['coppermind-people','coppermind-review']);
  assert.deepEqual(f.detached,['coppermind-waiting','coppermind-notes-in-review','coppermind-imports']);
  assert.equal(f.requests.length,0);
});

test('the command opens the screen and it reads what is waiting, what failed and what is under way',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  await f.plugin.commands.review.callback();
  await new Promise(r=>setTimeout(r,20));
  const paths=f.requests.map(r=>new URL(r.url).pathname);
  for(const wanted of ['/v1/recordings/waiting','/v1/review/notes','/v1/review/models'])assert.ok(paths.includes(wanted),wanted);
  assert.ok(!paths.some(p=>p.includes('dropbox')),'imports are not read');
});

test('trouble comes first, then work under way, then a card for each waiting note, newest first',async()=>{
  const f=await review({
    notes:[note('n2','Newest call'),note('n1','Older call')],
    failed:[{id:'r1',title:'Fabrikam outage review',message:'The summary did not finish. The recording and transcript are safe.',can_retry:true,can_dismiss:true}],
    processing:[{id:'r2',title:'Hallway chat',message:'Being transcribed. This can take a while for a long recording.',since:'2026-11-26T12:31:00-06:00'}]});
  const titles=view=>view.contentEl.children.map(c=>c.all().find(e=>['strong','button'].includes(e.tag)&&e.cls?.match(/name|title/))?.text).filter(Boolean);
  assert.deepEqual(titles(f.view),['Fabrikam outage review','Hallway chat','Newest call','Older call']);
  assert.ok(f.texts().includes('2 waiting'));
  assert.ok(f.texts().includes('The summary did not finish. The recording and transcript are safe.'));
  assert.ok(f.texts().includes('Being transcribed. This can take a while for a long recording.'));
  assert.ok(f.texts().every(t=>!BANNED.test(t)),f.texts().filter(t=>BANNED.test(t)).join('|'));
});

test('with nothing to do the screen says so in plain words',async()=>{
  const f=await review();
  assert.ok(f.texts().includes('Nothing to review.'));
  assert.ok(f.texts().every(t=>!BANNED.test(t)));
});

test('the title opens the note, and a note that has not reached this device says so',async()=>{
  const f=await review({notes:[note('n1','Acme call'),note('n2','Late call')]});
  delete f.files['Review/Late call.md'];
  f.view.data.notes=f.s.notes;f.view.render();
  await f.button('Acme call').onclick();
  assert.deepEqual(f.opened,['Review/Acme call.md']);
  await f.button('Late call').onclick();
  assert.deepEqual(f.opened,['Review/Acme call.md']);
  assert.ok(f.notices.includes('This note has not reached this device yet.'));
});

test('a card shows the time, and a note dated with a day only shows that day, not the one before',async()=>{
  const f=await review({notes:[note('n1','Hallway chat',{date:'2026-11-26'})]});
  assert.ok(f.texts().includes(new Date(2026,10,26).toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'})));
  assert.ok(!f.texts().some(t=>/Nov 25/.test(t)));
});

test('the card is filled in from the note, and choosing a type writes straight to its properties',async()=>{
  const f=await review({notes:[note('n1','Acme call')],files:{'Review/Acme call.md':frontmatter({type:'reference',account:['Acme'],tags:['vsphere']})}});
  assert.ok(f.texts().includes('vsphere')&&f.texts().includes('Acme'));
  assert.match(f.button('Reference').cls,/chosen/);
  await f.button('Work').onclick();
  assert.equal(f.files['Review/Acme call.md'].type,'work');
  assert.deepEqual(plain(f.writes.at(-1).fm.account),['Acme']);
  assert.match(f.button('Work').cls,/chosen/);
  assert.doesNotMatch(f.button('Reference').cls,/chosen/);
  assert.deepEqual(posted(f),[]);
});

test('an account or a tag is added from what is known or new, removed with its ×, and a closed list refuses a stranger',async()=>{
  const f=await review({notes:[note('n1','Acme call')],files:{'Review/Acme call.md':frontmatter({account:['Acme'],tags:['vsphere']})}});
  const add=label=>f.view.contentEl.all().find(e=>e.tag==='input'&&e.attr?.list?.startsWith('coppermind-'+label));
  add('account').value='contoso';await add('account').onchange();
  assert.deepEqual(plain(f.files['Review/Acme call.md'].account),['Acme','Contoso']);
  add('tags').value='brand-new';await add('tags').onchange();
  assert.deepEqual(plain(f.files['Review/Acme call.md'].tags),['vsphere','brand-new']);
  await f.view.contentEl.all().find(e=>e.attr?.['aria-label']==='Remove vsphere').onclick();
  assert.deepEqual(plain(f.files['Review/Acme call.md'].tags),['brand-new']);
  const closed=await review({notes:[note('n1','Acme call')]});
  closed.view.data.page.choices.tags_closed=true;closed.view.render();
  const tag=closed.view.contentEl.all().find(e=>e.tag==='input'&&e.attr?.list?.startsWith('coppermind-tags'));
  tag.value='stranger';await tag.onchange();
  assert.deepEqual(plain(closed.files['Review/Acme call.md'].tags),[]);
  assert.ok(closed.texts().includes('“stranger” is not one of your tags.'));
});

test('the meeting is a dropdown of the day, and choosing one tells Coppermind',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  const select=f.view.contentEl.all().find(e=>e.tag==='select'&&e.children.some(o=>o.value==='b'));
  assert.equal(select.value,'a');
  assert.deepEqual(select.children.map(o=>o.value),['','a','b']);
  select.value='b';await select.onchange();
  assert.deepEqual(f.calls,[{path:'/v1/notes/n1/meeting',body:{event_id:'b'}}]);
});

test('choosing No meeting tells Coppermind, and the card shows none after it redraws',async()=>{
  const f=await review({notes:[note('n1','Acme call')],respond:async(req,p)=>{
    if(p.endsWith('/meeting')&&req.method==='POST'){f.s.notes=[note('n1','Acme call',{meeting:{selected:'',candidates:MEETINGS}})];return null;}
    return null;}});
  const select=f.view.contentEl.all().find(e=>e.tag==='select'&&e.children.some(o=>o.value==='b'));
  assert.equal(select.value,'a');
  select.value='';await select.onchange();
  assert.deepEqual(f.calls,[{path:'/v1/notes/n1/meeting',body:{event_id:''}}]);
  const after=f.view.contentEl.all().find(e=>e.tag==='select'&&e.children.some(o=>o.value==='b'));
  assert.equal(after.value,'');
  f.view.render();
  assert.equal(f.view.contentEl.all().find(e=>e.tag==='select'&&e.children.some(o=>o.value==='b')).value,'');
});

test('a meeting that could not be changed says so in one sentence and the card stays usable',async()=>{
  const f=await review({notes:[note('n1','Acme call')],respond:(req,p)=>p.endsWith('/meeting')?{status:422,json:{message:'That meeting is not on the calendar for this day.'}}:null});
  const select=f.view.contentEl.all().find(e=>e.tag==='select'&&e.children.some(o=>o.value==='b'));
  select.value='b';await select.onchange();
  assert.ok(f.texts().includes('That meeting is not on the calendar for this day.'));
  assert.ok(f.button('Reviewed'));
});

test('Who spoke has its place on the card and shows who was matched',async()=>{
  const f=await review({notes:[note('n1','Acme call')],files:{'Review/Acme call.md':frontmatter({attendees:['[[People/Dana Whitfield]]','Priya Raman','Dana Whitfield'],type:'work'})}});
  assert.ok(f.view.contentEl.all().some(e=>e.cls==='coppermind-who'));
  assert.ok(f.texts().includes('Who spoke'));
  assert.ok(f.texts().includes('Dana Whitfield, Priya Raman'));
});

test('Reviewed sets the property, then the card says Filed with where, and Undo brings it back',async()=>{
  const f=await review({notes:[note('n1','Acme call')],filedPath:'work/2026/2026-11/Acme call.md'});
  await f.button('Reviewed').onclick();
  assert.equal(f.files['Review/Acme call.md'].reviewed,true);
  assert.ok(f.texts().includes('Reviewed. Filing…'));
  // The server files the note and it leaves Review.
  await new Promise(r=>setTimeout(r,10));
  f.s.notes=[];await f.view.refresh(true);await new Promise(r=>setTimeout(r,10));
  assert.ok(f.texts().includes('Filed ✓ work › 2026 › 2026-11'),f.texts().join('|'));
  assert.ok(f.texts().includes('Acme call'));
  await f.button('Undo').onclick();
  assert.deepEqual(posted(f),['/v1/notes/n1/reopen']);
  assert.ok(!f.texts().some(t=>t.startsWith('Filed')));
});

test('Undo before the note is filed unticks Reviewed on the note itself',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  await f.button('Reviewed').onclick();
  await f.button('Undo').onclick();
  assert.equal(f.files['Review/Acme call.md'].reviewed,false);
  assert.deepEqual(posted(f),[]);
  assert.ok(f.button('Reviewed'));
});

test('a note that has not reached this device shows its title and waits, with nothing to press',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  delete f.files['Review/Acme call.md'];f.view.render();
  assert.ok(f.texts().includes('Waiting to sync.'));
  assert.ok(!f.button('Reviewed')&&!f.button('Resummarize'));
  assert.equal(f.writes.length,0);
  await f.button('Acme call').onclick();
  assert.deepEqual(f.opened,[]);
  assert.ok(f.notices.includes('This note has not reached this device yet.'));
});

test('the model choice lists what the connection reports, starts on the configured one, and applies to one run',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  const select=f.view.contentEl.all().find(e=>e.attr?.['aria-label']==='Model');
  assert.deepEqual(select.children.map(o=>o.value),['gpt-default','claude-opus-5-5']);
  assert.equal(select.value,'gpt-default');
  await f.button('Resummarize').onclick();
  assert.deepEqual(f.calls.at(-1),{path:'/v1/notes/n1/resummarize',body:{}});
  const again=f.view.contentEl.all().find(e=>e.attr?.['aria-label']==='Model');
  again.value='claude-opus-5-5';again.onchange();
  await f.button('Resummarize').onclick();
  assert.deepEqual(f.calls.at(-1),{path:'/v1/notes/n1/resummarize',body:{model:'claude-opus-5-5'}});
  assert.equal(f.requests.filter(r=>new URL(r.url).pathname==='/v1/review/models').length,1);
});

test('with only one model, or a server that will not list them, there is no choice to make',async()=>{
  const one=await review({notes:[note('n1','Acme call')],models:{default:'gpt-default',models:['gpt-default'],problem:''}});
  assert.ok(!one.view.contentEl.all().some(e=>e.attr?.['aria-label']==='Model'));
  assert.ok(one.button('Resummarize'));
  const down=await review({notes:[note('n1','Acme call')],models:false});
  assert.ok(!down.view.contentEl.all().some(e=>e.attr?.['aria-label']==='Model'));
  await down.button('Resummarize').onclick();
  assert.deepEqual(down.calls.at(-1),{path:'/v1/notes/n1/resummarize',body:{}});
});

test('a note whose server takes no model for a run shows no model choice',async()=>{
  const f=await review({notes:[note('n1','Acme call',{takes_model:false})]});
  assert.ok(!f.view.contentEl.all().some(e=>e.attr?.['aria-label']==='Model'));
  assert.equal(f.requests.filter(r=>new URL(r.url).pathname==='/v1/review/models').length,0);
  await f.button('Resummarize').onclick();
  assert.deepEqual(f.calls.at(-1),{path:'/v1/notes/n1/resummarize',body:{}});
});

test('a failed recording row keeps the specific reason as a hover title',async()=>{
  const f=await review({failed:[{id:'r1',title:'A',message:'Sentence.',detail:'The service is paused.',can_retry:false,can_dismiss:true}]});
  const sentence=f.view.contentEl.all().find(e=>e.text==='Sentence.');
  assert.equal(sentence.attr?.title,'The service is paused.');
});

test('Resummarize shows it is running, and says what became of it',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  await f.button('Resummarize').onclick();
  f.s.notes=[note('n1','Acme call',{writeup:{state:'working',reason:'',when:'2026-11-26T12:31:00-06:00'}})];
  await f.view.refresh();
  assert.ok(f.texts().some(t=>t.startsWith('Summarizing, started')));
  assert.equal(f.button('Resummarizing…').disabled,true);
  f.s.notes=[note('n1','Acme call')];await f.view.refresh();
  assert.ok(f.texts().includes('Summary rewritten.'));
  assert.ok(f.button('Resummarize'));
});

test('a summary that failed is one plain sentence on the card, the longer reason under the pointer, and Resummarize works again',async()=>{
  const f=await review({notes:[note('n1','Acme call',{writeup:{state:'failed',reason:'The AI server is not answering. The note is unchanged.',when:''}})]});
  assert.ok(f.texts().includes('The summary did not finish.'));
  assert.ok(f.view.contentEl.all().some(e=>e.attr?.title==='The AI server is not answering. The note is unchanged.'));
  assert.ok(f.texts().every(t=>t!=='The AI server is not answering. The note is unchanged.'));
  await f.button('Resummarize').onclick();
  assert.equal(posted(f).at(-1),'/v1/notes/n1/resummarize');
});

test('a refused Resummarize shows the server words and the button works again',async()=>{
  const f=await review({notes:[note('n1','Acme call')],respond:(req,p)=>p.endsWith('/resummarize')?{status:422,json:{message:'Write-ups are not set up.'}}:null});
  await f.button('Resummarize').onclick();
  assert.ok(f.texts().includes('Write-ups are not set up.'));
  assert.equal(f.button('Resummarize').disabled,false);
});

test('an edited note asks once, in one sentence with two buttons, before its body is replaced',async()=>{
  const f=await review({notes:[note('n1','Acme call',{edited:true})]});
  await f.button('Resummarize').onclick();
  assert.deepEqual(posted(f),[]);
  assert.ok(f.texts().includes('Replace your edited note with the new summary? Your version stays in history.'));
  assert.equal(f.view.contentEl.all().filter(e=>e.cls==='coppermind-confirm')[0].children.filter(e=>e.tag==='button').length,2);
  await f.button('Cancel').onclick();
  assert.deepEqual(posted(f),[]);
  await f.button('Resummarize').onclick();
  await f.button('Replace').onclick();
  assert.deepEqual(f.calls,[{path:'/v1/notes/n1/resummarize',body:{overwrite:true}}]);
});

test('an unedited note is written again with no question',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  await f.button('Resummarize').onclick();
  assert.deepEqual(f.calls,[{path:'/v1/notes/n1/resummarize',body:{}}]);
});

test('the review screen has no diff dialog and no banned wording',async()=>{
  const f=await review({notes:[note('n1','Acme call',{edited:true}),note('n2','Other call',{writeup:{state:'failed',reason:'Failed.',when:''}})]});
  const words=f.view.contentEl.all().flatMap(e=>[e.text,e.title]).filter(Boolean);
  assert.ok(words.every(w=>!BANNED.test(w)));
  assert.equal(f.modals.length,0);
});

test('a recording that failed is a row with one sentence and Try again, and it can be dismissed without a question',async()=>{
  const f=await review({failed:[{id:'r1',title:'Fabrikam outage review',message:'The summary did not finish. The recording and transcript are safe.',can_retry:true,can_dismiss:true}]});
  await f.button('Try again').onclick();
  assert.deepEqual(posted(f),['/v1/recordings/r1/retry']);
  await f.button('Dismiss').onclick();
  assert.deepEqual(posted(f),['/v1/recordings/r1/retry','/v1/recordings/r1/dismiss']);
  assert.equal(f.modals.length,0);
});

test('a recording the app will try again by itself offers no Dismiss, and one that cannot be retried no Try again',async()=>{
  const f=await review({failed:[{id:'r1',title:'A',message:'Sentence.',can_retry:true,can_dismiss:false},{id:'r2',title:'B',message:'Sentence.',can_retry:false,can_dismiss:true}]});
  assert.equal(f.buttons('Dismiss').length,1);assert.equal(f.buttons('Try again').length,1);
});

test('a failed Try again says so and offers it again',async()=>{
  const f=await review({failed:[{id:'r1',title:'A',message:'Sentence.',can_retry:true,can_dismiss:false}],respond:(req,p)=>p.endsWith('/retry')?{status:500,json:{}}:null});
  await f.button('Try again').onclick();
  assert.ok(f.texts().includes('Could not try again. Check your connection.'));
  assert.equal(f.button('Try again').disabled,false);
});

test('a reviewed note that could not be filed is a card with one sentence and Try again',async()=>{
  const f=await review({notes:[note('n1','Acme call',{filing:{problem:'This note could not be filed because it has no date Coppermind can read.'}})]});
  assert.ok(f.texts().includes('This note could not be filed because it has no date Coppermind can read.'));
  assert.ok(!f.button('Reviewed'));
  await f.button('Try again').onclick();
  assert.deepEqual(posted(f),['/v1/notes/n1/file-again']);
});

test('a server that stops answering is said in one sentence, and a key without permission says what to do',async()=>{
  const down=await review({respond:()=>({status:500,json:{message:'down'}})});
  assert.ok(down.texts().includes('Coppermind is not answering. Check your connection and that this device is still paired.'));
  const forbidden=await review({respond:()=>({status:403,json:{message:'no'}})});
  assert.ok(forbidden.texts().some(t=>/missing something the companion needs/.test(t)));
});

test('a later failure keeps what was shown',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  f.s.respond=()=>({status:500,json:{message:'down'}});
  await f.view.refresh();
  assert.ok(f.texts().includes('Acme call'));
  assert.ok(f.texts().some(t=>/not answering/.test(t)));
});

test('a change made to the note itself shows on its card',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  f.files['Review/Acme call.md'].type='work';
  f.changed[0]({path:'Review/Acme call.md'});
  await new Promise(r=>setTimeout(r,350));
  assert.match(f.button('Work').cls,/chosen/);
});

test('a refresh in flight does not redraw over the question about replacing an edited note',async()=>{
  const f=await review({notes:[note('n1','Acme call',{edited:true})]});
  await f.button('Resummarize').onclick();
  const before=f.view.contentEl.all().length;
  await f.view.refresh(true);
  assert.equal(f.view.contentEl.all().length,before);
  assert.ok(f.button('Replace'));
});

test('the order is the newest first and a card filed this session keeps its place',()=>{
  return fixture(undefined,{}).then(f=>{
    const n=id=>({id});
    const out=f.rules.arrange([n('c'),n('a')],['b','a'],{b:{id:'b'}});
    assert.deepEqual(JSON.parse(JSON.stringify(out.map(i=>i.note?.id||'filed:'+i.filed.id))),['c','filed:b','a']);
  });
});

const digest=async text=>'sha256:'+Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(text))).toString('hex');
const SUMMARY={summary:'Launch plan.',decisions:'- Ship Friday.',action_items:'- Casey: send the plan.'};
const olderNote=over=>note('n1','Acme call',{takes_model:false,...over});

test('an older recording marked Reviewed still lands its generated summary, decisions and action items',async()=>{
  const text='---\nid: n1\n---\nBody.\n';
  const hash=await digest(text);
  const f=await review({notes:[olderNote()],texts:{'Review/Acme call.md':text},respond:(req,p)=>p==='/v1/notes/n1/review'&&req.method==='GET'?ok({state:'suggested',job_id:'job-1',meeting_summary:SUMMARY,note:{content_hash:hash}}):null});
  await f.button('Reviewed').onclick();
  assert.deepEqual(plain(f.calls),[{path:'/v1/notes/n1/review',body:{if_match:hash,job_id:'job-1',selected:[],meeting_summary:SUMMARY,confirmation:'Complete review'}}]);
  assert.equal(f.writes.length,0,'the property alone would file the note without its summary');
  assert.ok(f.texts().includes('Reviewed. Filing…'));
  await f.button('Undo').onclick();
  assert.deepEqual(posted(f),['/v1/notes/n1/review','/v1/notes/n1/reopen']);
});

test('an older recording with no summary waiting is marked reviewed on the note itself',async()=>{
  const f=await review({notes:[olderNote()],respond:(req,p)=>p==='/v1/notes/n1/review'&&req.method==='GET'?ok({state:'failed',job_id:null,meeting_summary:null,note:{content_hash:'sha256:x'}}):null});
  await f.button('Reviewed').onclick();
  assert.equal(f.files['Review/Acme call.md'].reviewed,true);
  assert.deepEqual(posted(f),[]);
});

test('an older recording whose latest edits have not reached the server waits instead of filing without them',async()=>{
  const f=await review({notes:[olderNote()],texts:{'Review/Acme call.md':'edited here only'},respond:(req,p)=>p==='/v1/notes/n1/review'&&req.method==='GET'?ok({state:'suggested',job_id:'job-1',meeting_summary:SUMMARY,note:{content_hash:'sha256:server-copy'}}):null});
  await f.button('Reviewed').onclick();
  assert.deepEqual(posted(f),[]);
  assert.equal(f.writes.length,0);
  assert.ok(f.texts().includes('This note is still syncing. Try again in a moment.'));
  assert.equal(f.button('Reviewed').disabled,false);
});

test('a refresh that finds something new leaves the screen alone while a property box is being typed in, and catches up after',async()=>{
  const f=await review({notes:[note('n1','Acme call')]});
  const box=f.view.contentEl.all().find(e=>e.tag==='input'&&e.attr?.list?.startsWith('coppermind-account'));
  box.onfocus();box.value='Contoso, half typed';
  f.s.processing=[{id:'r9',title:'New recording',message:'Waiting for its turn to be transcribed.'}];
  await f.view.refresh(true);
  assert.ok(f.view.contentEl.all().includes(box),'the box being typed in was redrawn away');
  assert.equal(box.value,'Contoso, half typed');
  assert.ok(!f.texts().includes('New recording'));
  box.onblur();await new Promise(r=>setTimeout(r,20));
  assert.ok(f.texts().includes('New recording'));
});
