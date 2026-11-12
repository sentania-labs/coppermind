// Contract tests for the shipped companion: sync boundaries and secret handling.
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
  createEl(tag,options){const el=new El(tag,options);this.children.push(el);return el;}
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
  const raw='---\nid: test-note\nreviewed: false\n---\nHuman prose.\n';
  const digest='sha256:'+Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(raw))).toString('hex');
  const requests=[];const notices=[];
  const file={basename:'Note',path:'Review/Note.md'};
  let factory;const factories={};const modals=[];
  class Plugin {registerView(type,fn){factory=fn;factories[type]=fn;}addSettingTab(tab){this.settingTab=tab;}addCommand(command){(this.commands??={})[command.id]=command;}async saveData(data){(this.saved??=[]).push(JSON.parse(JSON.stringify(data)));}addRibbonIcon(){}registerEvent(){}async loadData(){return {url:'https://coppermind.example',secret:'local-secret-name'};}}
  class ItemView {constructor(leaf){this.app=leaf.app;this.contentEl=new El();}}
  const app={metadataCache:{getFileCache:()=>({frontmatter:{id:'test-note'}})},secretStorage:{getSecret:()=> 'synthetic-credential'},vault:{read:async()=>local??raw,getAbstractFileByPath:path=>({path})},workspace:{getActiveFile:()=>file,on:()=>null,getActiveViewOfType:()=>null,openLinkText:()=>{}}};
  const context={module:{exports:{}},require:()=>({Plugin,ItemView,Setting,Modal:class{constructor(app){this.app=app;this.contentEl=new El();}open(){modals.push(this);this.onOpen();}close(){this.onClose();}},PluginSettingTab:class{constructor(){this.containerEl=new El();}},SecretComponent:class{setValue(){return this;}onChange(){return this;}},Notice:class{constructor(text){notices.push(text);}},MarkdownView:class{},requestUrl:async req=>{requests.push(req);if(req.url.endsWith('/v1/pair')&&overrides.pair)return overrides.pair(req);if(req.url.endsWith('/v1/anything')&&overrides.page)return overrides.page(req);if(req.url.endsWith('/v1/recordings/waiting'))return {status:200,json:overrides.waiting};if(req.url.includes('/v1/recordings/')&&req.url.endsWith('/dismiss'))return overrides.dismissFail?{status:500,json:{}}:{status:200,json:{}};if(req.url.includes('/v1/recordings/')&&req.url.endsWith('/retry'))return overrides.retryFail?{status:overrides.retryFail===true?500:overrides.retryFail,json:{}}:(overrides.onRetry?.(),{status:200,json:{}});if(req.url.endsWith('/v1/schema'))return {status:200,json:{roles:{id_key:'id'}}};if(req.url.endsWith('/v1/entities/voice/recognition'))return {status:200,json:{enabled:true}};if(req.url.endsWith('/v1/entities/voice/unknowns'))return {status:200,json:overrides.unknownVoices||{}};if(req.url.endsWith('/v1/entities/voice/people'))return {status:200,json:overrides.savedPeople||{}};if(req.url.includes('/v1/entities?'))return overrides.rejectPeople?{status:overrides.rejectPeople===true?403:overrides.rejectPeople,json:{error:'forbidden'}}:{status:200,json:overrides.peopleResults||{items:[],next_cursor:null}};if(req.url.endsWith('/review')&&req.method==='POST'&&overrides.rejectReview)return {status:403,json:{error:'forbidden'}};return {status:200,json:{note:{content_hash:digest,frontmatter:{reviewed:false}},roles:{reviewed_key:'reviewed',type_key:'type'},suggestions:[],unresolved:[],stale:false,state:'suggested',suggestions_enabled:true,job_id:null,...overrides}};}}),crypto:webcrypto,TextEncoder,URL};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../coppermind_admin/obsidian_plugin/main.js'),'utf8'),context);
  const plugin=new context.module.exports();plugin.app=app;await plugin.onload();
  const view=factory({app});await view.refresh();
  return {view,plugin,requests,notices,app,raw,factories,modals};
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

test('a suggested meeting can be changed before confirming',async()=>{
  const meeting={calendar_hash:'calendar',suggested_event_id:'a',candidates:[{event_id:'a',title:'Planning',people:[{name:'Alex'}]},{event_id:'b',title:'Budget',people:[{name:'Blair'}]}]};
  const f=await fixture(undefined,{recording_meeting:meeting});
  const radios=f.view.contentEl.all().filter(e=>e.tag==='input'&&['a','b'].includes(e.value));
  assert.equal(radios.find(r=>r.value==='a').checked,true);
  const choice=radios.find(r=>r.value==='b');choice.checked=true;choice.onchange();
  await f.view.contentEl.all().find(e=>e.text==='Use selected meeting').onclick();
  const request=f.requests.find(r=>r.url.endsWith('/recording-meeting'));
  assert.equal(JSON.parse(request.body).event_id,'b');
});

test('merging requires a confirmation and uses the checked destination and summary',async()=>{
  const raw='---\nid: test-note\nreviewed: false\n---\nHuman prose.\n';
  const digest='sha256:'+Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(raw))).toString('hex');
  const f=await fixture(undefined,{recording_merge:{state:'offered',transcript_path:'_Sources/Transcript.md',candidates:[{id:'target',title:'My meeting',path:'Notes/My meeting.md',content_hash:digest}]},meeting_summary:{summary:'Launch plan.',decisions:'',action_items:''},job_id:'job'});
  f.view.contentEl.all().find(e=>e.text==='Review merge').onclick();
  assert.equal(f.requests.filter(r=>r.url.endsWith('/recording-merge')).length,0);
  assert.ok(f.modals[0].contentEl.all().some(e=>e.text==='View transcript'));
  await f.modals[0].contentEl.all().find(e=>e.text==='Confirm merge').onclick();
  const request=f.requests.find(r=>r.url.endsWith('/recording-merge'));
  assert.equal(JSON.parse(request.body).target_id,'target');
  assert.equal(JSON.parse(request.body).meeting_summary.summary,'Launch plan.');
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Recording added'));
});

test('merge shows progress and prevents conflicting actions until it resolves',async()=>{
  const raw='---\nid: test-note\nreviewed: false\n---\nHuman prose.\n';
  const digest='sha256:'+Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(raw))).toString('hex');
  const f=await fixture(undefined,{recording_merge:{state:'offered',candidates:[{id:'target',title:'My meeting',path:'Notes/My meeting.md',content_hash:digest}]},meeting_summary:{summary:'Launch plan.',decisions:'',action_items:''},job_id:'job'});
  let rejectMerge;const call=f.plugin.call.bind(f.plugin);
  f.plugin.call=(url,...args)=>url.endsWith('/recording-merge')?new Promise((_,reject)=>{rejectMerge=reject;}):call(url,...args);
  f.view.contentEl.all().find(e=>e.text==='Review merge').onclick();
  const pending=f.modals[0].contentEl.all().find(e=>e.text==='Confirm merge').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Adding the recording to your note…'));
  for(const text of ['Review merge','Keep separate','Apply kept suggestions & mark reviewed','Mark reviewed without changes']){
    const button=f.view.contentEl.all().find(e=>e.text===text);assert.ok(button,text);assert.equal(button.disabled,true,text);
  }
  rejectMerge(Error('Connection lost'));await pending;
  assert.ok(f.view.contentEl.all().some(e=>e.text?.includes('Refresh this panel to check its status before trying again.')));
  assert.equal(f.view.contentEl.all().find(e=>e.text==='Refresh merge status').disabled,false);
});

test('an interrupted merge offers retry from the companion',async()=>{
  const f=await fixture(undefined,{recording_merge:{state:'attention',error:'The destination note is unavailable.'}});
  assert.ok(f.view.contentEl.all().some(e=>e.text==='The destination note is unavailable.'));
  await f.view.contentEl.all().find(e=>e.text==='Retry merge').onclick();
  assert.ok(f.requests.some(r=>r.url.endsWith('/recording-merge/retry')));
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
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  const body=JSON.parse(f.requests.find(r=>r.method==='POST').body);
  assert.deepEqual(body.selected,[3]);
  assert.equal(body.speaker_choices['3'],'person:b');
});

const proposal=(index,confidence,value='person:a')=>({kind:'speaker',field:'Speaker '+(index+1),index,value,display_value:'Alex',confidence,reason:'Named.',quote:'',contact_ids:[],candidates:[{id:'person:a',name:'Alex',email:''},{id:'person:b',name:'Blair',email:''}]});
test('preview says attendees will not be updated when no attendees property is set up',async()=>{
  const notice='Attendees will not be updated because no attendees property is set up.';
  const f=await fixture(undefined,{suggestions:[proposal(0,.9)],job_id:'job',speaker_attendees_notice:notice});
  const text=f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/Speaker 1 → Alex/);assert.match(text,/Attendees will not be updated/);
  assert.doesNotMatch(text,/note attendees and dossier links/);
});
test('attendees notice stays out of the preview when the note has no speakers',async()=>{
  const f=await fixture(undefined,{suggestions:[],job_id:'job',speaker_attendees_notice:'Attendees will not be updated because no attendees property is set up.'});
  const text=f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.doesNotMatch(text,/Attendees will not be updated/);
});
test('preview shows selected speaker changes and dropdown correction selects the card',async()=>{
  const f=await fixture(undefined,{suggestions:[proposal(0,.9),proposal(1,.4)],job_id:'job'});
  let text=()=>f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text(),/Speaker 1 → Alex/);assert.doesNotMatch(text(),/Speaker 2 → Alex/);
  assert.match(text(),/transcript labels, note attendees and links to selected people/);assert.match(text(),/Unnamed speakers stay/);
  const selects=f.view.contentEl.all().filter(e=>e.tag==='select');selects[1].value='person:b';selects[1].onchange();
  assert.match(text(),/Speaker 2 → Blair/);
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.deepEqual(JSON.parse(f.requests.find(r=>r.method==='POST').body).selected,[0,1]);
});
test('select all suggested names selects low confidence names explicitly',async()=>{
  const f=await fixture(undefined,{suggestions:[proposal(0,.4),proposal(1,.3)],job_id:'job'});
  f.view.contentEl.all().find(e=>e.text==='Select all suggested names').onclick();
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.deepEqual(JSON.parse(f.requests.find(r=>r.method==='POST').body).selected,[0,1]);
});
test('failed suggestions show cause and retry and still permit manual speaker selection',async()=>{
  const f=await fixture(undefined,{state:'failed',suggestion_error:'The model timed out.',manual_speaker_review:{revision:2,speakers:['Speaker 1'],candidates:[{id:'person:a',name:'Alex'}]}});
  assert.ok(f.view.contentEl.all().some(e=>e.text?.includes('The model timed out.')));
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Retry suggestions'));
  const select=f.view.contentEl.all().find(e=>e.tag==='select');select.value='person:a';select.onchange();
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
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

test('a key that cannot see people shows why speakers are missing',async()=>{
  const f=await fixture(undefined,{state:'suggested',suggestions:[],manual_speaker_review:null,speakers_unavailable:'This key cannot read people, so speakers cannot be named with it. In Coppermind Admin, open Connected apps (API Keys) and create a new key with Read people and companies, then select that key in the companion settings.'});
  assert.ok(f.view.contentEl.all().some(e=>e.text?.includes('cannot be named with it')));
  assert.ok(!f.view.contentEl.all().some(e=>e.tag==='select'));
});

test('permission failures give plain recovery steps',async()=>{
  const notice='This key cannot read people, so speakers cannot be named with it. In Coppermind Admin, open Connected apps and either pair this device with a code, or open the key and tick Read notes, Add and change notes, Hand in recordings and files, and Read and correct people and companies.';
  const f=await fixture(undefined,{state:'failed',rejectPeople:true,rejectReview:true,people_access_notice:notice,manual_speaker_review:{revision:1,speakers:['Speaker 1'],candidates:[]}});
  f.view.contentEl.all().find(e=>e.tag==='input').value='Alex';
  await f.view.contentEl.all().find(e=>e.text==='Find a person').onclick();
  assert.ok(f.notices.includes(notice));
  await assert.rejects(f.plugin.call('/v1/notes/test-note/review','POST',{manual_speakers:{'Speaker 1':'person:a'}}),/or open the key and tick Read notes, Add and change notes, Hand in recordings and files, and Read and correct people and companies/);
  assert.ok(f.notices.every(n=>!n.includes('entities:read')));
});

test('a server error during people search does not tell the owner to replace the key',async()=>{
  const notice='This key cannot read people, so speakers cannot be named with it. Create a new key.';
  const f=await fixture(undefined,{state:'failed',rejectPeople:500,people_access_notice:notice,manual_speaker_review:{revision:1,speakers:['Speaker 1'],candidates:[]}});
  f.view.contentEl.all().find(e=>e.tag==='input').value='Alex';
  await f.view.contentEl.all().find(e=>e.text==='Find a person').onclick();
  assert.ok(!f.notices.includes(notice));
  assert.ok(f.notices.some(n=>n.includes('Check your connection')));
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
  const credential='cm_pairedkey_0123456789abcdef';
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

const summaryProposal={summary:'Launch discussion.',decisions:'- Ship Friday.',action_items:'- Casey: send the plan.'};
test('summary edits and dropped sections are sent only on review completion',async()=>{
  const f=await fixture(undefined,{meeting_summary:summaryProposal,job_id:'job'});
  const inputs=f.view.contentEl.all().filter(e=>e.tag==='textarea');
  assert.equal(inputs.length,3);assert.equal(inputs[0].value,'Launch discussion.');
  inputs[0].value='Owner corrected summary.';inputs[0].oninput();
  await f.view.contentEl.all().find(e=>e.text==='Drop decisions').onclick();
  assert.equal(f.requests.filter(r=>r.method==='POST').length,0);
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  const body=JSON.parse(f.requests.find(r=>r.method==='POST').body);
  assert.deepEqual(body.meeting_summary,{summary:'Owner corrected summary.',decisions:'',action_items:'- Casey: send the plan.'});
});
test('review without changes drops the summary',async()=>{
  const f=await fixture(undefined,{meeting_summary:summaryProposal});
  await f.view.contentEl.all().find(e=>e.text==='Mark reviewed without changes').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).meeting_summary,null);
});
for(const state of ['failed','running','stale'])test(state+' summary cannot be applied',async()=>{
  const f=await fixture(undefined,{state,meeting_summary:summaryProposal});
  assert.ok(f.view.contentEl.all().filter(e=>e.tag==='textarea').every(e=>e.disabled));
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).meeting_summary,null);
});

const pendingSummaryNotice='The summary is still being written. Marking reviewed now files the note without it.';
for(const state of ['queued','running'])test(state+' recording warns beside completion before filing without a summary',async()=>{
  const f=await fixture(undefined,{state,manual_speaker_review:{source_id:'source',revision:1,speakers:[],candidates:[]}});
  const children=f.view.contentEl.children;
  const action=children.findIndex(e=>e.text==='Apply kept suggestions & mark reviewed');
  assert.equal(children[action-1].text,pendingSummaryNotice);
  assert.equal(children[action].disabled,false);
  await children[action].onclick();
  const writes=f.requests.filter(r=>r.method==='POST');
  assert.equal(writes.length,1);
  assert.equal(JSON.parse(writes[0].body).meeting_summary,null);
});
for(const overrides of [{state:'running'},{state:'suggested',meeting_summary:summaryProposal},{state:'failed',meeting_summary:summaryProposal}])test('no pending-summary warning for '+JSON.stringify(overrides),async()=>{
  const f=await fixture(undefined,overrides);
  assert.ok(!f.view.contentEl.all().some(e=>e.text===pendingSummaryNotice));
});
test('voice and maybe names require a deliberate choice',async()=>{
  const f=await fixture(undefined,{state:'failed',manual_speaker_review:{source_id:'source',revision:1,speakers:['Speaker 1'],candidates:[],voice_matches:{'Speaker 1':[{id:'person:a',name:'Alex',strength:'maybe'}]}}});
  const select=f.view.contentEl.all().find(e=>e.tag==='select');
  assert.equal(select.value,'');
  const button=f.view.contentEl.all().find(e=>e.text==='Maybe: Alex');assert.ok(button);
  button.onclick();
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).manual_speakers['Speaker 1'],'person:a');
});

const voiceManual=matches=>({source_id:'source',revision:1,speakers:['Speaker 1'],candidates:[],voice_matches:{'Speaker 1':matches}});
const match=(id,name,strength='strong')=>({id:'person:'+id,name,strength});
test('a single strong voice suggestion is included in one completion',async()=>{
  const f=await fixture(undefined,{manual_speaker_review:voiceManual([match('a','Alex')])});
  const text=f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/Speaker 1 → Alex/);assert.match(text,/Source: Voice/);assert.doesNotMatch(text,/No people suggested/);
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).manual_speakers['Speaker 1'],'person:a');
});
test('agreeing model and voice show both sources with one selection',async()=>{
  const s=proposal(0,.9,'contact:a');s.candidates=[{id:'contact:a',person_id:'a',name:'Alex'}];
  const f=await fixture(undefined,{suggestions:[s],job_id:'job',manual_speaker_review:voiceManual([match('a','Alex')])});
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Sources: Model and Voice'));
  assert.ok(!f.view.contentEl.all().some(e=>e.text==='Voice: Alex'));
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).speaker_choices['0'],'contact:a');
});
for(const [name,suggestions,matches] of [
  ['two voices',[],[match('a','Alex'),match('b','Blair')]],
  ['model and voice',[proposal(0,.9,'person:a')],[match('b','Blair')]],
])test(name+' conflict selects no one',async()=>{
  const f=await fixture(undefined,{suggestions,job_id:'job',manual_speaker_review:voiceManual(matches)});
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Suggestions disagree. Choose a person.'));
  assert.equal(f.view.contentEl.all().find(e=>e.tag==='select').value,'');
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  const body=JSON.parse(f.requests.find(r=>r.method==='POST').body);
  assert.deepEqual(body.selected,[]);assert.deepEqual(body.manual_speakers,{});
});
test('correcting a selected voice match sends only the corrected person',async()=>{
  const review=voiceManual([match('a','Alex')]);review.candidates=[{id:'person:b',name:'Blair'}];
  const f=await fixture(undefined,{manual_speaker_review:review});
  const select=f.view.contentEl.all().find(e=>e.tag==='select');select.value='person:b';select.onchange();
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).manual_speakers['Speaker 1'],'person:b');
});

test('a speaker without enough speech is explained in review',async()=>{
  const manual={speakers:['Speaker 2'],candidates:[],voice_matches:{},voice_unavailable:['Speaker 2']};
  const f=await fixture(undefined,{manual_speaker_review:manual});
  assert.ok(f.view.contentEl.all().some(e=>e.text?.includes('too little speech to recognise this voice')));
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

test('summary edits and strong voice are submitted together',async()=>{
  const f=await fixture(undefined,{meeting_summary:summaryProposal,job_id:'job',manual_speaker_review:voiceManual([match('a','Alex')])});
  const input=f.view.contentEl.all().find(e=>e.tag==='textarea');input.value='Corrected summary.';input.oninput();
  assert.ok(f.view.contentEl.all().some(e=>e.text==='Speaker 1 → Alex'));
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  const body=JSON.parse(f.requests.find(r=>r.method==='POST').body);
  assert.equal(body.meeting_summary.summary,'Corrected summary.');
  assert.equal(body.manual_speakers['Speaker 1'],'person:a');
});


test('voice selection does not show an empty model suggestion as a 0% voice match',async()=>{
  const s={...proposal(0,0,null),display_value:'No person suggested',reason:'No confident match was suggested.',candidates:[]};
  const f=await fixture(undefined,{suggestions:[s],job_id:'job',manual_speaker_review:voiceManual([match('a','Alex')])});
  const text=f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/Source: Voice/);assert.match(text,/Speaker 1 → Alex/);
  assert.doesNotMatch(text,/Model confidence: 0%/);assert.doesNotMatch(text,/No confident match was suggested/);
  await f.view.contentEl.all().find(e=>e.text==='Apply kept suggestions & mark reviewed').onclick();
  assert.equal(JSON.parse(f.requests.find(r=>r.method==='POST').body).speaker_choices['0'],'person:a');
});
test('useful model evidence stays attributed when voice supplies the name',async()=>{
  const s={...proposal(0,0,null),reason:'Someone said hello.',quote:'Hello.',candidates:[]};
  const f=await fixture(undefined,{suggestions:[s],job_id:'job',manual_speaker_review:voiceManual([match('a','Alex')])});
  const text=f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/Model: Someone said hello/);assert.match(text,/Model evidence/);
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
  const modal=f.modals.at(-1);assert.ok(modal.contentEl.all().some(e=>e.text?.includes('relabel 2 recordings')));
  await modal.contentEl.all().find(e=>e.text==='Combine voices').onclick();
  assert.equal(f.requests.filter(r=>r.method==='POST').length,1);
});

test('strong voice suggestion explains confirmation and earlier appearances',async()=>{
  const manual={speakers:['Unknown voice 12'],candidates:[],voice_matches:{'Unknown voice 12':[match('a','Alex')]},voice_appearances:{'Unknown voice 12':3}};
  const f=await fixture(undefined,{manual_speaker_review:manual});
  const text=f.view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/Check this suggestion before you finish/);
  assert.match(text,/remembered across recordings/);
  assert.match(text,/2 earlier recordings/);
});

test('waiting view opens the note and handles a failed retry',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[{title:'Planning',path:'Review/Planning.md',date:''}],failed:[{id:'abc',title:'Call',message:'Try again.',can_retry:true}]},retryFail:true});
  const file={path:'Review/Planning.md'};let opened;
  f.app.vault.getAbstractFileByPath=path=>path===file.path?file:null;
  f.app.workspace.getLeaf=()=>({openFile:async value=>{opened=value;}});
  f.plugin.open=async()=>{};
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  assert.ok(view.contentEl.all().some(e=>e.text==='1 meeting to review · 1 recording failed'));
  await view.contentEl.all().find(e=>e.text==='Planning').onclick();assert.equal(opened,file);
  const retry=view.contentEl.all().find(e=>e.text==='Retry');await retry.onclick();
  assert.equal(retry.disabled,false);
  assert.ok(view.contentEl.all().some(e=>e.text?.includes('Could not queue retry')));
  assert.ok(f.requests.some(r=>r.url.endsWith('/v1/recordings/abc/retry')&&r.method==='POST'));
});

test('dismissing a failed recording asks first, then refreshes the list',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'No speech was found.',can_retry:false,can_dismiss:true}]}});
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  const texts=()=>view.contentEl.all().map(e=>e.text||'');
  await view.contentEl.all().find(e=>e.text==='Dismiss').onclick();
  assert.equal(f.requests.some(r=>r.url.endsWith('/dismiss')),false);
  assert.ok(texts().includes('This only removes the entry from this list and deletes nothing.'));
  const before=f.requests.filter(r=>r.url.endsWith('/v1/recordings/waiting')).length;
  await view.contentEl.all().find(e=>e.text==='Dismiss entry').onclick();
  assert.ok(f.requests.some(r=>r.url.endsWith('/v1/recordings/abc/dismiss')&&r.method==='POST'));
  assert.equal(f.requests.filter(r=>r.url.endsWith('/v1/recordings/waiting')).length,before+1);
});

test('a failed dismiss keeps the entry and lets the user try again',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'No speech was found.',can_retry:false,can_dismiss:true}]},dismissFail:true});
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  await view.contentEl.all().find(e=>e.text==='Dismiss').onclick();
  const yes=view.contentEl.all().find(e=>e.text==='Dismiss entry');await yes.onclick();
  assert.equal(yes.disabled,false);
  assert.ok(view.contentEl.all().some(e=>e.text?.includes('Could not dismiss')));
});

test('a recording the app will retry by itself offers no Dismiss',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'The app will try again automatically.',can_retry:true,can_dismiss:false,retry_after:'2026-11-11T12:00:00Z'}]}});
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  assert.equal(view.contentEl.all().some(e=>e.text==='Dismiss'),false);
  assert.ok(view.contentEl.all().some(e=>e.text==='Retry'));
});


test('a queued retry refreshes the count and removes the failed row and Dismiss',async()=>{
  const options={waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'Try again.',can_retry:true,can_dismiss:true}]}};
  options.onRetry=()=>{options.waiting={meetings:[],failed:[]};};
  const f=await fixture(undefined,options), view=f.factories['coppermind-waiting']({app:f.app});
  await view.refresh();await view.contentEl.all().find(e=>e.text==='Retry').onclick();
  const texts=view.contentEl.all().map(e=>e.text);
  assert.ok(texts.includes('0 meetings to review · 0 recordings failed'));
  assert.ok(texts.includes('No recordings need attention.'));
  assert.ok(!texts.includes('Call'));assert.ok(!texts.includes('Dismiss'));
});

test('a credential forbidden to retry sees the required permissions',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'Try again.',can_retry:true}]},retryFail:403});
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  const retry=view.contentEl.all().find(e=>e.text==='Retry');await retry.onclick();
  const message=view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(message,/missing something the companion needs/);
  assert.match(message,/either pair this device with a code, or make a key with: Read notes, Add and change notes, Hand in recordings and files, Search, and Read and correct people and companies/);
  assert.match(message,/select it in the companion settings/);assert.equal(retry.disabled,false);
  assert.doesNotMatch(message,/Check your connection/);
});


test('rejected note details show only the specific field problem',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'The meeting note fields were rejected.',field_problems:[{field:'account',reason:'should be a list'}]}]}});
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  const text=view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.ok(text.includes('Details: the "account" field should be a list.'));
  assert.doesNotMatch(text,/schema|external id/);
});

test('rejected note with unavailable field details explains the next step',async()=>{
  const f=await fixture(undefined,{waiting:{meetings:[],failed:[{id:'abc',title:'Call',message:'The meeting note fields were rejected.',field_problems:[],field_problems_unavailable:true}]}});
  const view=f.factories['coppermind-waiting']({app:f.app});await view.refresh();
  const text=view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.ok(text.includes('Details: the specific field was not recorded. Retry to check the note again.'));
});
test('imports view shows stored artifacts and failed files',async()=>{
  const f=await fixture();
  f.plugin.call=async path=>{
    assert.equal(path,'/v1/dropbox');
    return {folder:'Dropbox',items:[
      {filename:'report.pdf',state:'succeeded',parts:[{artifact_id:'a',path:'Artifacts/report.pdf'}],error:''},
      {filename:'broken.ics',state:'failed',parts:[],error:'The calendar file could not be read.'},
    ]};
  };
  const view=f.factories['coppermind-imports']({app:f.app});await view.refresh();
  const text=view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/report.pdf/);assert.match(text,/Open artifact/);
  assert.match(text,/broken.ics/);assert.match(text,/could not be read/);
});

test('imports view explains an unavailable connection',async()=>{
  const f=await fixture();f.plugin.call=async()=>{throw Error('offline');};
  const view=f.factories['coppermind-imports']({app:f.app});await view.refresh();
  assert.ok(view.contentEl.all().some(e=>e.text==='Imports are unavailable. Try again.'));
  assert.ok(view.contentEl.all().some(e=>e.text==='Retry'));
});

test('imports view explains a key that cannot read artifacts',async()=>{
  const f=await fixture();
  f.plugin.call=async()=>({folder:'Dropbox',items:[{filename:'report.pdf',state:'succeeded',parts:[{artifact:'stored'}],error:''}]});
  const view=f.factories['coppermind-imports']({app:f.app});await view.refresh();
  const text=view.contentEl.all().map(e=>e.text||'').join(' ');
  assert.match(text,/needs permission to read artifacts/);
  assert.ok(!view.contentEl.all().some(e=>e.text==='Open artifact'));
});
