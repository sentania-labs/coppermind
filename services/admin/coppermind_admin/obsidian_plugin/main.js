/* Coppermind companion: proposals stay separate until explicit review completion. */
const {Plugin, ItemView, PluginSettingTab, Setting, SecretComponent, Notice, requestUrl, MarkdownView} = require('obsidian');
const VIEW = 'coppermind-review';
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
        const changes=cards.filter(c=>c.active()&&c.chooser.value);
        if(!changes.length)preview.createEl('p',{text:'No speaker names selected.'});
        for(const c of changes){const person=c.people.find(p=>p.id===c.chooser.value);if(person)preview.createEl('p',{text:c.label+' → '+person.name});}
        const attendeesNotice=cards.length&&data.speaker_attendees_notice;
        if(attendeesNotice)preview.createEl('p',{text:data.speaker_attendees_notice});
        preview.createEl('p',{text:attendeesNotice?'Completing review updates transcript labels for selected people. Unnamed speakers stay as they are.':'Completing review updates transcript labels, note attendees and dossier links for selected people. Unnamed speakers stay as they are.'});
      };
      const makeChooser=(card,label,people,initial,onSelect,disabled)=>{
        const chooser=card.createEl('select');
        chooser.createEl('option',{text:'Leave as '+label,value:''});
        const add=person=>{if(!people.some(p=>p.id===person.id)){people.push(person);chooser.createEl('option',{text:person.name+(person.email?' ('+person.email+')':''),value:person.id});}};
        const supplied=[...people];people.length=0;for(const person of supplied)add(person);
        chooser.value=initial||'';chooser.disabled=disabled;chooser.onchange=()=>{onSelect(chooser.value);updatePreview();};
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
          }catch(e){new Notice('This key cannot read people, so speakers cannot be named with it. In Coppermind Admin, open Connected apps (API Keys) and create a new key with Read people and companies, then select that key in the companion settings.');}
        };
        find.onclick=()=>lookup(false);
        const moreButton=card.createEl('button',{text:'More people'});moreButton.disabled=true;moreButton.onclick=()=>lookup(true);
        return chooser;
      };
      for(const s of data.suggestions){
        if(s.kind==='speaker'&&!ready)continue;
        const card=el.createDiv({cls:'coppermind-card'});let toggle;
        const initial=s.kind==='speaker'&&!!s.value&&s.confidence>=0.8&&ready;
        if(initial)selected.add(s.index);
        new Setting(card).setName(s.kind==='filename'?'Note title':s.kind==='speaker'?s.field:s.field||s.kind).addToggle(t=>{toggle=t;t.setDisabled(!ready).setValue(initial).onChange(v=>{v?selected.add(s.index):selected.delete(s.index);updatePreview();});});
        card.createEl('p',{text:s.display_value||(Array.isArray(s.value)?s.value.join(', '):String(s.value??''))});
        if(s.kind==='speaker'){
          card.createEl('p',{text:'Model confidence: '+Math.round(s.confidence*100)+'%'});
          if(!s.candidates.length)card.createEl('p',{text:'No people suggested. Enter a name below, then choose Find a person.'});
          const people=[...s.candidates];
          const chooser=makeChooser(card,s.field,people,s.value,value=>{speakerChoices[s.index]=value;selected.add(s.index);toggle.setValue(true);},!ready);
          speakerChoices[s.index]=chooser.value;cards.push({label:s.field,chooser,people,active:()=>selected.has(s.index),select:()=>{if(chooser.value){selected.add(s.index);toggle.setValue(true);}}});
        }
        card.createEl('p',{text:s.reason});if(s.quote)card.createEl('blockquote',{text:s.quote});
        if(s.contact_ids.length)card.createEl('small',{text:'Linked contact evidence is recorded with this suggestion.'});
      }
      const manual=data.manual_speaker_review;
      const proposed=new Set(data.suggestions.filter(s=>s.kind==='speaker'&&ready).map(s=>s.field));
      for(const label of manual?.speakers||[]){
        if(proposed.has(label))continue;
        const card=el.createDiv({cls:'coppermind-card'});card.createEl('h3',{text:label});
        card.createEl('p',{text:'Choose a person, or leave this speaker unnamed.'});
        const people=[...manual.candidates];
        if(!people.length)card.createEl('p',{text:'No people suggested. Enter a name below, then choose Find a person.'});
        const chooser=makeChooser(card,label,people,'',value=>{manualSpeakers[label]=value;},!synced);
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
          const payload={if_match:data.note.content_hash,job_id:data.job_id,selected:apply?[...selected]:[],speaker_choices:apply?speakerChoices:{},manual_speakers:apply?manualSpeakers:{},manual_source_revision:manual?.revision??null,manual_source_id:manual?.source_id??null,accept_link_risk:acceptLinks,confirmation:'Complete review'};
          this.plugin.pending={id,payload};
          await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/review','POST',payload);
          this.plugin.pending=null;
          new Notice('Review recorded. Changes and filing will sync back to Obsidian.');await this.refresh();
        }catch(e){new Notice(e.message);}
      };
      const apply=el.createEl('button',{text:'Apply selected & mark reviewed',cls:'mod-cta'});apply.disabled=!synced;apply.onclick=()=>finish(true);
      const without=el.createEl('button',{text:'Mark reviewed without changes'});without.disabled=!synced;without.onclick=()=>finish(false);
      const refresh=el.createEl('button',{text:'Request fresh suggestions'});refresh.disabled=!synced;refresh.onclick=async()=>{
        try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/suggestions','POST',{});new Notice('Suggestions queued. Refresh when processing finishes.');}catch(e){new Notice(e.message);}
      };

    }catch(e){el.createEl('p',{text:e.message,cls:'coppermind-notice'});const retry=el.createEl('button',{text:'Retry'});retry.onclick=()=>this.refresh();}
  }
}
class Settings extends PluginSettingTab {
  constructor(app,plugin){super(app,plugin);this.plugin=plugin;}
  display(){const el=this.containerEl;el.empty();el.createEl('h2',{text:'Coppermind'});
    new Setting(el).setName('Server URL').setDesc('Your Coppermind address, without /admin.').addText(t=>t.setValue(this.plugin.settings.url).onChange(async v=>{this.plugin.settings.url=v.trim().replace(/\/$/,'');await this.plugin.saveData(this.plugin.settings);}));
    new Setting(el).setName('API credential').setDesc('In Coppermind Admin, open Connected apps (API Keys) and create a new key with Read notes, Update notes, Read sources, Update sources, and Read people and companies. Select that key here. Only the secret name is saved in plugin settings.').addComponent(container=>new SecretComponent(this.app,container).setValue(this.plugin.settings.secret).onChange(async v=>{this.plugin.settings.secret=v;await this.plugin.saveData(this.plugin.settings);}));
  }
}
module.exports=class CoppermindReview extends Plugin {
  async onload(){
    this.settings={url:'',secret:'',...await this.loadData()};
    this.registerView(VIEW,leaf=>new ReviewView(leaf,this));
    this.addSettingTab(new Settings(this.app,this));
    this.addCommand({id:'review-current-note',name:'Review current note',callback:()=>this.open()});
    this.addRibbonIcon('clipboard-check','Review with Coppermind',()=>this.open());
    this.registerEvent(this.app.workspace.on('file-open',file=>{if(file)this.noteFile=file;}));
  }
  async open(){this.noteFile=this.app.workspace.getActiveFile()||this.noteFile;let leaf=this.app.workspace.getLeavesOfType(VIEW)[0]||this.app.workspace.getRightLeaf(false);await leaf.setViewState({type:VIEW,active:true});await this.app.workspace.revealLeaf(leaf);await leaf.view.refresh();}
  async call(path,method='GET',body){
    const url=new URL(this.settings.url);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('Configure a valid Coppermind HTTP(S) address.');
    const key=this.app.secretStorage?.getSecret(this.settings.secret);if(!key)throw Error('Select an API credential in Coppermind plugin settings.');
    const result=await requestUrl({url:this.settings.url+path,method,headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,throw:false});
    const namesSpeakers=Object.values(body?.manual_speakers||{}).some(Boolean)||(body?.selected||[]).some(i=>!!body?.speaker_choices?.[i]);
    if(result.status===403&&method==='POST'&&path.endsWith('/review')&&namesSpeakers)throw Error('This key is not allowed to name speakers. In Coppermind Admin, open Connected apps (API Keys) and create a new key with Read notes, Update notes, Read sources, Update sources, and Read people and companies, then select that key in the companion settings.');
    if(result.status>=400)throw Error(result.json?.message||'Coppermind could not complete the request. Check connectivity and permissions.');return result.json;
  }
};
