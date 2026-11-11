/* Coppermind companion: proposals stay separate until explicit review completion. */
const {Plugin, ItemView, PluginSettingTab, Setting, SecretComponent, Notice, requestUrl, MarkdownView, Modal} = require('obsidian');
const VIEW = 'coppermind-review';
const PEOPLE_VIEW = 'coppermind-people';
async function hash(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return 'sha256:' + [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2,'0')).join('');
}
class ReviewView extends ItemView {
  constructor(leaf, plugin) { super(leaf); this.plugin=plugin; this.generation=0; }
  getViewType(){return VIEW;}
  getDisplayText(){return 'Review note';}
  getIcon(){return 'clipboard-check';}
  async onOpen(){await this.refresh();}
  async refresh(){
    const generation=++this.generation;
    const el=this.contentEl; el.empty(); el.createEl('h2',{text:'Review note'});
    const file=this.plugin.noteFile || this.app.workspace.getActiveFile();
    if(!file){el.createEl('p',{text:'Open a note, then refresh this panel.'});return;}
    this.file=file;
    try {
      const schema=await this.plugin.call('/v1/schema');
      const fm=this.app.metadataCache.getFileCache(file)?.frontmatter;
      const id=fm?.[schema.roles.id_key];
      if(!id) throw Error('This note has not been adopted yet. Wait for sync and reconciliation.');
      const data=await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/review');
      if(generation!==this.generation)return;
      this.data=data;
      if(data.completion?.state==='applying')this.plugin.pending={id,payload:data.completion.payload};
      el.createEl('h3',{text:file.basename});
      const local=await this.app.vault.read(file);
      const synced=await hash(local)===data.note.content_hash;
      if(!synced) el.createEl('p',{text:'Your latest edits have not reached Coppermind. Wait for sync, then refresh.',cls:'coppermind-notice'});
      const reload=el.createEl('button',{text:'Refresh from Coppermind'});reload.onclick=()=>this.refresh();
      if(data.note.frontmatter[data.roles.type_key]==='work'){
        const box=el.createDiv({cls:'coppermind-card'});box.createEl('h3',{text:'Meeting time'});
        let meetingTime=String(fm[data.roles.date_key]||data.note.frontmatter[data.roles.date_key]||''), unknown=fm.time_unknown===true;
        new Setting(box).setName('Date and start time').addText(t=>{t.inputEl.type='datetime-local';t.inputEl.step='1';t.setValue(meetingTime).onChange(v=>meetingTime=v);});
        new Setting(box).setName('Time unknown').addToggle(t=>t.setValue(unknown).onChange(v=>unknown=v));
        new Setting(box).setName('Save meeting properties').setDesc('Writes only these properties; let sync finish before review.').addButton(b=>b.setButtonText('Save time').onClick(async()=>{
          await this.app.fileManager.processFrontMatter(file,f=>{
            f.time_unknown=unknown;
            if(meetingTime){f[data.roles.date_key]=/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(meetingTime)?meetingTime+':00':meetingTime;delete f.time;delete f.timezone;}
          });new Notice('Meeting time saved. Wait for sync, then refresh.');await this.refresh();
        }));
      }
      if(this.plugin.pending?.id===id){const resume=el.createEl('button',{text:'Retry recorded completion'});resume.onclick=async()=>{
        try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/review','POST',this.plugin.pending.payload);this.plugin.pending=null;await this.refresh();}catch(e){new Notice(e.message);}
      };}
      if(data.note.frontmatter[data.roles.reviewed_key]===true){
        el.createEl('p',{text:data.review_problem?'Review needs attention: '+data.review_problem:'Reviewed. Filing and indexing finish in the background.'});
        if(data.calendar_update){
          const update=data.calendar_update;
          el.createEl('p',{text:update.message});
          let eventId='';
          new Setting(el).setName('Calendar association').addDropdown(d=>{
            d.addOption('','Keep existing decisions; dismiss update');
            for(const c of update.candidates||[])d.addOption(c.event_id,c.title);
            d.onChange(v=>eventId=v);
          });
          new Setting(el).addButton(b=>b.setButtonText('Confirm calendar decision').onClick(async()=>{
            try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/calendar-update','POST',{if_match:data.note.content_hash,calendar_hash:update.calendar_hash,event_id:eventId});await this.refresh();}catch(e){new Notice(e.message);}
          }));
        }
        return;
      }
      el.createEl('p',{text:'Edit the note and its properties as usual. Finish here when you are ready.'});
      const selected=new Set();
      const speakerChoices={}, manualSpeakers={}, cards=[];
      const ready=data.state==='suggested'&&!data.stale&&synced;
      const summary=data.meeting_summary?{...data.meeting_summary}:null;
      if(!data.suggestions_enabled)el.createEl('p',{text:'Suggestions are off. You can name speakers below, or turn on enrichment in Model connections.',cls:'coppermind-notice'});
      else if(data.suggestion_error){
        el.createEl('p',{text:'Suggestions could not be completed: '+data.suggestion_error,cls:'coppermind-notice'});
        const retry=el.createEl('button',{text:'Retry suggestions'});retry.disabled=!synced;retry.onclick=async()=>{
          try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/suggestions','POST',{});new Notice('Suggestions queued. Refresh when ready.');}catch(e){new Notice(e.message);}
        };
      } else if(['queued','running'].includes(data.state))el.createEl('p',{text:'Suggestions are in progress. Refresh this panel when ready.',cls:'coppermind-notice'});
      if(data.speakers_unavailable)el.createEl('p',{text:data.speakers_unavailable,cls:'coppermind-notice'});
      if(data.speaker_notice)el.createEl('p',{text:data.speaker_notice,cls:'coppermind-notice'});
      if(data.stale)el.createEl('p',{text:'Suggestions are from an earlier version. Keep your edits and finish without them, or request fresh suggestions.',cls:'coppermind-notice'});
      const preview=el.createDiv({cls:'coppermind-card'});
      const updatePreview=()=>{
        preview.empty();preview.createEl('h3',{text:'What will change'});
        if(summary&&ready)preview.createEl('p',{text:Object.values(summary).some(v=>v.trim())?'The kept summary sections will be appended to your note. Your existing text stays intact.':'All summary sections dropped. No summary text will be added.'});
        const changes=cards.filter(c=>c.active()&&c.chooser.value);
        if(!changes.length)preview.createEl('p',{text:'No speaker names selected.'});
        for(const c of changes){const person=c.people.find(p=>p.id===c.chooser.value);if(person)preview.createEl('p',{text:c.label+' → '+person.name});}
        const attendeesNotice=cards.length&&data.speaker_attendees_notice;
        if(attendeesNotice)preview.createEl('p',{text:data.speaker_attendees_notice});
        preview.createEl('p',{text:attendeesNotice?'Completing review updates transcript labels for selected people. Unnamed speakers stay as they are.':'Completing review updates transcript labels, note attendees and dossier links for selected people. Unnamed speakers stay as they are.'});
      };
      const makeChooser=(card,label,people,initial,onSelect,disabled,voice=[])=>{
        const chooser=card.createEl('select');
        chooser.createEl('option',{text:'Leave as '+label,value:''});
        const add=person=>{if(!people.some(p=>p.id===person.id)){people.push(person);chooser.createEl('option',{text:person.name+(person.email?' ('+person.email+')':''),value:person.id});}};
        const supplied=[...people];people.length=0;for(const person of supplied)add(person);
        for(const match of voice)add(match);
        chooser.value=initial||'';chooser.disabled=disabled;chooser.onchange=()=>{onSelect(chooser.value);updatePreview();};
        for(const match of voice){
          const choice=card.createEl('button',{text:(match.strength==='maybe'?'Maybe: ':'Voice: ')+match.name});
          choice.disabled=disabled;choice.onclick=()=>{chooser.value=match.id;chooser.onchange();};
        }
        const search=card.createEl('input',{attr:{type:'search',placeholder:'Find a person'}});search.disabled=disabled;
        const find=card.createEl('button',{text:'Find a person'});find.disabled=disabled;
        let cursor='';
        const lookup=async more=>{
          try{
            if(!search.value?.trim()){new Notice('Enter a name to find a person.');return;}
            const page=await this.plugin.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(search.value.trim())+(more&&cursor?'&cursor='+encodeURIComponent(cursor):''));
            for(const person of page.items||[])add({id:'person:'+person.id,name:person.name,email:person.emails?.[0]||''});
            cursor=page.next_cursor||'';moreButton.disabled=!cursor;
            if(!(page.items||[]).length)new Notice('No matching people. Search for another name.');
            else new Notice('Choose a person from the list.');
          }catch(e){new Notice(e.status===403&&data.people_access_notice||'Could not find people. Check your connection and API credential, then refresh this panel.');}
        };
        find.onclick=()=>lookup(false);
        const moreButton=card.createEl('button',{text:'More people'});moreButton.disabled=true;moreButton.onclick=()=>lookup(true);
        return chooser;
      };
      if(summary){
        const box=el.createDiv({cls:'coppermind-card'});box.createEl('h3',{text:'Meeting summary'});
        box.createEl('p',{text:'Edit these suggestions, or clear a section to drop it. Marking reviewed adds what you keep to the note. Action items are recorded only; nothing is carried out.'});
        for(const [key,label] of [['summary','Summary'],['decisions','Decisions'],['action_items','Action items and owners']]){
          const field=box.createEl('label',{text:label});
          const input=field.createEl('textarea');input.value=summary[key];input.rows=6;input.maxLength=12000;input.style.width='100%';input.disabled=!ready;
          input.oninput=()=>{summary[key]=input.value;updatePreview();};
          const drop=box.createEl('button',{text:'Drop '+label.toLowerCase()});drop.disabled=!ready;
          drop.onclick=()=>{summary[key]='';input.value='';updatePreview();};
        }
      }
      const manual=data.manual_speaker_review;
      // Contact candidates can refer to the same People entry as a voice match.
      const personKey=(id,candidates)=>{
        const person=candidates.find(c=>c.id===id);
        return person?.person_id?'person:'+person.person_id:id;
      };
      const voicePlan=(label,suggestion)=>{
        const matches=manual?.voice_matches?.[label]||[];
        const strong=matches.filter(m=>m.strength==='strong');
        const candidates=suggestion?.candidates||[];
        const confident=!!suggestion?.value&&suggestion.confidence>=0.8;
        const modelKey=suggestion?.value?personKey(suggestion.value,candidates):'';
        const voiceKey=strong.length===1?personKey(strong[0].id,candidates):'';
        const conflict=strong.length>1||(voiceKey&&confident&&voiceKey!==modelKey);
        const agreed=!!voiceKey&&voiceKey===modelKey;
        return {matches,strong,conflict,agreed,initial:conflict?'':voiceKey?(agreed?suggestion.value:strong[0].id):suggestion?.value||''};
      };
      for(const s of data.suggestions){
        if(s.kind==='speaker'&&!ready)continue;
        const card=el.createDiv({cls:'coppermind-card'});let toggle;
        const voice=s.kind==='speaker'?voicePlan(s.field,s):null;
        const initial=s.kind==='speaker'&&ready&&!voice.conflict&&!!voice.initial&&(voice.strong.length===1||s.confidence>=0.8);
        const noModelCandidate=s.kind==='speaker'&&!s.value&&voice.matches.length>0;
        if(initial)selected.add(s.index);
        new Setting(card).setName(s.kind==='filename'?'Note title':s.kind==='speaker'?s.field:s.field||s.kind).addToggle(t=>{toggle=t;t.setDisabled(!ready).setValue(initial).onChange(v=>{v?selected.add(s.index):selected.delete(s.index);updatePreview();});});
        const shown=s.display_value||(Array.isArray(s.value)?s.value.join(', '):String(s.value??''));
        card.createEl('p',{text:voice?.conflict&&s.value?'Model: '+shown:voice&&voice.strong.length===1&&!voice.conflict&&!voice.agreed?voice.strong[0].name:shown});
        if(s.kind==='speaker'){
          if(!noModelCandidate)card.createEl('p',{text:'Model confidence: '+Math.round(s.confidence*100)+'%'});
          if(voice.agreed)card.createEl('p',{text:'Sources: Model and Voice'});
          else if(voice.strong.length===1&&!voice.conflict)card.createEl('p',{text:'Source: Voice'});
          if(voice.conflict)card.createEl('p',{text:'Suggestions disagree. Choose a person.'});
          if(!s.candidates.length&&!voice.matches.length)card.createEl('p',{text:'No people suggested. Enter a name below, then choose Find a person.'});
          const people=[...s.candidates];
          if(voice.strong.length===1&&!voice.conflict&&!voice.agreed)people.push(voice.strong[0]);
          const choices=voice.strong.length===1&&!voice.conflict?voice.matches.filter(m=>m!==voice.strong[0]):voice.matches;
          const chooser=makeChooser(card,s.field,people,voice.initial,value=>{speakerChoices[s.index]=value;value?selected.add(s.index):selected.delete(s.index);toggle.setValue(!!value);},!ready,choices);
          speakerChoices[s.index]=chooser.value;cards.push({label:s.field,chooser,people,active:()=>selected.has(s.index),select:()=>{if(chooser.value){selected.add(s.index);toggle.setValue(true);}}});
        }
        if(s.reason&&(!noModelCandidate||!/^No (confident )?(match|person|speaker).*suggest/i.test(s.reason)))card.createEl('p',{text:s.kind==='speaker'?'Model: '+s.reason:s.reason});
        if(s.quote){if(s.kind==='speaker')card.createEl('small',{text:'Model evidence'});card.createEl('blockquote',{text:s.quote});}
        if(s.contact_ids.length)card.createEl('small',{text:'Linked contact evidence is recorded with this suggestion.'});
      }
      const proposed=new Set(data.suggestions.filter(s=>s.kind==='speaker'&&ready).map(s=>s.field));
      for(const label of manual?.speakers||[]){
        if(proposed.has(label))continue;
        const card=el.createDiv({cls:'coppermind-card'});card.createEl('h3',{text:label});
        card.createEl('p',{text:'Choose a person, or leave this speaker unnamed.'});
        const people=[...manual.candidates];const voice=voicePlan(label,null);
        if(!people.length&&!voice.matches.length)card.createEl('p',{text:'No people suggested. Enter a name below, then choose Find a person.'});
        if(voice.conflict)card.createEl('p',{text:'Suggestions disagree. Choose a person.'});
        else if(voice.initial)card.createEl('p',{text:'Source: Voice'});
        if(voice.initial)people.push(voice.strong[0]);
        const choices=voice.initial?voice.matches.filter(m=>m!==voice.strong[0]):voice.matches;
        const chooser=makeChooser(card,label,people,voice.initial,value=>{manualSpeakers[label]=value;},!synced,choices);
        if(voice.initial&&synced)manualSpeakers[label]=voice.initial;
        cards.push({label,chooser,people,active:()=>!!manualSpeakers[label],select:()=>{}});
      }
      if(cards.length){const all=el.createEl('button',{text:'Select all suggested names'});all.disabled=!ready;all.onclick=()=>{for(const card of cards)card.select();updatePreview();};}
      updatePreview();
      for(const unresolved of data.unresolved)el.createEl('p',{text:unresolved,cls:'coppermind-notice'});
      let acceptLinks=false;
      if(data.suggestions.some(s=>s.kind==='filename'))new Setting(el).setName('Allow selected filename change').setDesc('Existing filename links may require repair.').addToggle(t=>t.onChange(v=>acceptLinks=v));
      const finish=async apply=>{
        try{
          // Save any active editor, then compare exact bytes again before approval.
          const editor=this.app.workspace.getActiveViewOfType(MarkdownView);
          if(editor?.file?.path===file.path)await editor.save();
          if(await hash(await this.app.vault.read(file))!==data.note.content_hash)throw Error('The note changed. Refresh after sync before finishing.');
          const payload={if_match:data.note.content_hash,job_id:data.job_id,selected:apply?[...selected]:[],meeting_summary:apply&&ready?summary:null,speaker_choices:apply?speakerChoices:{},manual_speakers:apply?manualSpeakers:{},manual_source_revision:manual?.revision??null,manual_source_id:manual?.source_id??null,accept_link_risk:acceptLinks,confirmation:'Complete review'};
          this.plugin.pending={id,payload};
          await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/review','POST',payload);
          this.plugin.pending=null;
          new Notice('Review recorded. Changes and filing will sync back to Obsidian.');await this.refresh();
        }catch(e){new Notice(e.message);}
      };
      if((manual||summary)&&['queued','running'].includes(data.state))el.createEl('p',{text:'The summary is still being written. Marking reviewed now files the note without it.',cls:'coppermind-notice'});
      const apply=el.createEl('button',{text:'Apply kept suggestions & mark reviewed',cls:'mod-cta'});apply.disabled=!synced;apply.onclick=()=>finish(true);
      const without=el.createEl('button',{text:'Mark reviewed without changes'});without.disabled=!synced;without.onclick=()=>finish(false);
      const refresh=el.createEl('button',{text:'Request fresh suggestions'});refresh.disabled=!synced;refresh.onclick=async()=>{
        try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/suggestions','POST',{});new Notice('Suggestions queued. Refresh when processing finishes.');}catch(e){new Notice(e.message);}
      };

    }catch(e){el.createEl('p',{text:e.message,cls:'coppermind-notice'});const retry=el.createEl('button',{text:'Retry'});retry.onclick=()=>this.refresh();}
  }
}
class DeleteVoiceModal extends Modal {
  constructor(app, person, confirm, options={}){super(app);this.person=person;this.confirm=confirm;this.options=options;this.done=false;}
  onOpen(){
    const o=this.options;
    this.contentEl.createEl('h2',{text:o.title||'Delete voice signatures?'});
    this.contentEl.createEl('p',{text:o.text||'All saved voice signatures for '+this.person.name+' will be deleted. This cannot be undone.'});
    const button=this.contentEl.createEl('button',{text:o.action||'Delete signatures',cls:'mod-warning'});
    button.onclick=async()=>{this.done=true;this.close();await this.confirm();};
    const cancel=this.contentEl.createEl('button',{text:'Cancel'});cancel.onclick=()=>this.close();
  }
  onClose(){this.contentEl.empty();if(!this.done&&this.options.cancel)this.options.cancel();}
}
class PeopleView extends ItemView {
  constructor(leaf,plugin){super(leaf);this.plugin=plugin;}
  getViewType(){return PEOPLE_VIEW;}
  getDisplayText(){return 'People';}
  getIcon(){return 'users';}
  async onOpen(){await this.show();}
  async show(person){
    const el=this.contentEl;el.empty();el.createEl('h2',{text:'People'});
    const search=el.createEl('input',{attr:{type:'search',placeholder:'Find a person'}});
    const find=el.createEl('button',{text:'Search'});const list=el.createDiv();
    find.onclick=async()=>{try{list.empty();const page=await this.plugin.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(search.value.trim()));
      if(!page.items.length)list.createEl('p',{text:'No people found.'});
      for(const item of page.items){const b=list.createEl('button',{text:item.name});b.onclick=()=>this.show(item);}
    }catch(e){new Notice(e.message);}};
    if(person){
      try{
        const path='/v1/entities/'+encodeURIComponent(person.id)+'/voice';
        const voice=await this.plugin.call(path);
        el.createEl('h3',{text:person.name+' · Voice recognition'});
        el.createEl('p',{text:'Off by default. Voice signatures are stored, not audio clips. Unidentified signatures are held privately with the recording until review and removal.'});
        new Setting(el).setName('Recognise this person by voice').addToggle(t=>t.setValue(voice.enabled).onChange(async enabled=>{
          const apply=async()=>{try{await this.plugin.call(path,'PUT',{enabled});}catch(e){new Notice(e.message);}await this.show(person);};
          const count=voice.signatures.length;
          if(enabled||!count){await apply();return;}
          new DeleteVoiceModal(this.app,person,apply,{title:'Turn off voice recognition?',text:'Turning this off deletes '+count+' saved voice signature'+(count===1?'':'s')+' for '+person.name+'. Turning it on again starts from nothing.',action:'Turn off and delete',cancel:()=>this.show(person)}).open();
        }));
        el.createEl('p',{text:voice.signatures.length+' saved signatures'});
        if(!voice.signatures.length)el.createEl('p',{text:'A reviewed recording can add a signature when recognition is enabled.'});
        for(const sig of voice.signatures){const line=el.createEl('p');line.createSpan({text:sig.created_at.slice(0,10)+' · '});
          try{const note=await this.plugin.call('/v1/notes/'+encodeURIComponent(sig.note_id));const a=line.createEl('a',{text:note.title,href:'#'});a.onclick=e=>{e.preventDefault();this.app.workspace.openLinkText(note.path,'',false);};}catch{line.createSpan({text:'Source note unavailable'});}}
        if(voice.signatures.length){const del=el.createEl('button',{text:'Delete signatures',cls:'mod-warning'});del.onclick=()=>new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call(path,'DELETE');await this.show(person);}catch(e){new Notice(e.message);}}).open();}
      }catch(e){el.createEl('p',{text:e.message,cls:'coppermind-notice'});}
    }
  }
}
class Settings extends PluginSettingTab {
  constructor(app,plugin){super(app,plugin);this.plugin=plugin;}
  display(){const el=this.containerEl;el.empty();el.createEl('h2',{text:'Coppermind'});
    new Setting(el).setName('Server URL').setDesc('Your Coppermind address, without /admin.').addText(t=>t.setValue(this.plugin.settings.url).onChange(async v=>{this.plugin.settings.url=v.trim().replace(/\/$/,'');await this.plugin.saveData(this.plugin.settings);}));
    new Setting(el).setName('API credential').setDesc('In Coppermind Admin, open Connected apps (API Keys) and create a new key with Read notes, Update notes, Read sources, Update sources, and Read people and companies, and Update people and companies. Select that key here. Only the secret name is saved in plugin settings.').addComponent(container=>new SecretComponent(this.app,container).setValue(this.plugin.settings.secret).onChange(async v=>{this.plugin.settings.secret=v;await this.plugin.saveData(this.plugin.settings);}));
  }
}
module.exports=class CoppermindReview extends Plugin {
  async onload(){
    this.settings={url:'',secret:'',...await this.loadData()};
    this.registerView(PEOPLE_VIEW,leaf=>new PeopleView(leaf,this));
    this.registerView(VIEW,leaf=>new ReviewView(leaf,this));
    this.addSettingTab(new Settings(this.app,this));
    this.addCommand({id:'manage-people',name:'Manage people and voice recognition',callback:()=>this.openPeople()});
    this.addCommand({id:'review-current-note',name:'Review current note',callback:()=>this.open()});
    this.addRibbonIcon('clipboard-check','Review with Coppermind',()=>this.open());
    this.registerEvent(this.app.workspace.on('file-open',file=>{if(file)this.noteFile=file;}));
  }
  async openPeople(){
    const file=this.app.workspace.getActiveFile();
    let leaf=this.app.workspace.getLeavesOfType(PEOPLE_VIEW)[0]||this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({type:PEOPLE_VIEW,active:true});await this.app.workspace.revealLeaf(leaf);
    if(file){try{const result=await this.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(file.basename));const person=result.items?.find(p=>p.path===file.path);if(person)await leaf.view.show(person);}catch{}}
  }
  async open(){this.noteFile=this.app.workspace.getActiveFile()||this.noteFile;let leaf=this.app.workspace.getLeavesOfType(VIEW)[0]||this.app.workspace.getRightLeaf(false);await leaf.setViewState({type:VIEW,active:true});await this.app.workspace.revealLeaf(leaf);await leaf.view.refresh();}
  async call(path,method='GET',body){
    const url=new URL(this.settings.url);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('Configure a valid Coppermind HTTP(S) address.');
    const key=this.app.secretStorage?.getSecret(this.settings.secret);if(!key)throw Error('Select an API credential in Coppermind plugin settings.');
    const result=await requestUrl({url:this.settings.url+path,method,headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,throw:false});
    const namesSpeakers=Object.values(body?.manual_speakers||{}).some(Boolean)||(body?.selected||[]).some(i=>!!body?.speaker_choices?.[i]);
    if(result.status===403&&method==='POST'&&path.endsWith('/review')&&namesSpeakers)throw Error('This key is not allowed to name speakers. In Coppermind Admin, open Connected apps (API Keys) and create a new key with Read notes, Update notes, Read sources, Update sources, and Read people and companies, then select that key in the companion settings.');
    if(result.status>=400)throw Object.assign(Error(result.json?.message||'Coppermind could not complete the request. Check connectivity and permissions.'),{status:result.status});return result.json;
  }
};
