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
  getDisplayText(){return 'Coppermind review';}
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
      if(data.stale)el.createEl('p',{text:'Suggestions are from an earlier version. Keep your edits and finish without them, or request fresh suggestions.',cls:'coppermind-notice'});
      for(const s of data.suggestions){
        const card=el.createDiv({cls:'coppermind-card'});
        new Setting(card).setName(s.kind==='filename'?'Note title':s.field||s.kind).addToggle(t=>t.setDisabled(data.stale||!synced).onChange(v=>v?selected.add(s.index):selected.delete(s.index)));
        card.createEl('p',{text:s.display_value||(Array.isArray(s.value)?s.value.join(', '):String(s.value??''))});
        card.createEl('p',{text:s.reason});if(s.quote)card.createEl('blockquote',{text:s.quote});
        if(s.contact_ids.length)card.createEl('small',{text:'Linked contact evidence is recorded with this suggestion.'});
      }
      for(const unresolved of data.unresolved)el.createEl('p',{text:unresolved,cls:'coppermind-notice'});
      let acceptLinks=false;
      if(data.suggestions.some(s=>s.kind==='filename'))new Setting(el).setName('Allow selected filename change').setDesc('Existing filename links may require repair.').addToggle(t=>t.onChange(v=>acceptLinks=v));
      const finish=async apply=>{
        try{
          // Save any active editor, then compare exact bytes again before approval.
          const editor=this.app.workspace.getActiveViewOfType(MarkdownView);
          if(editor?.file?.path===file.path)await editor.save();
          if(await hash(await this.app.vault.read(file))!==data.note.content_hash)throw Error('The note changed. Refresh after sync before finishing.');
          const payload={if_match:data.note.content_hash,job_id:data.job_id,selected:apply?[...selected]:[],accept_link_risk:acceptLinks,confirmation:'Complete review'};
          this.plugin.pending={id,payload};
          await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/review','POST',payload);
          this.plugin.pending=null;
          new Notice('Review recorded. Changes and filing will sync back to Obsidian.');await this.refresh();
        }catch(e){new Notice(e.message);}
      };
      const apply=el.createEl('button',{text:'Apply selected & mark reviewed',cls:'mod-cta'});apply.disabled=!synced||data.stale;apply.onclick=()=>finish(true);
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
    new Setting(el).setName('API credential').setDesc('Use a key with notes:read, notes:write and sources:read. Only the secret name is saved in plugin settings.').addComponent(container=>new SecretComponent(this.app,container).setValue(this.plugin.settings.secret).onChange(async v=>{this.plugin.settings.secret=v;await this.plugin.saveData(this.plugin.settings);}));
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
    if(result.status>=400)throw Error(result.json?.message||'Coppermind could not complete the request. Check connectivity and permissions.');return result.json;
  }
};
