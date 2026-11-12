/* Coppermind companion: proposals stay separate until explicit review completion. */
const {Plugin, ItemView, PluginSettingTab, Setting, SecretComponent, Notice, requestUrl, MarkdownView, Modal} = require('obsidian');
const VIEW = 'coppermind-review';
const PEOPLE_VIEW = 'coppermind-people';
const WAITING_VIEW = 'coppermind-waiting';
const COMPANION_PERMISSIONS = 'Read notes, Add and change notes, Hand in recordings and files, Search, and Read and correct people and companies';
const IMPORTS_VIEW = 'coppermind-imports';
async function hash(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return 'sha256:' + [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2,'0')).join('');
}
class ImportsView extends ItemView {
  constructor(leaf,plugin){super(leaf);this.plugin=plugin;this.generation=0;}
  getViewType(){return IMPORTS_VIEW;}
  getDisplayText(){return 'Imports';}
  getIcon(){return 'inbox';}
  async onOpen(){this.timer=setInterval(()=>this.refresh(),5000);await this.refresh();}
  async onClose(){clearInterval(this.timer);this.generation++;}
  async refresh(){
    if(this.refreshing)return;this.refreshing=true;
    const generation=++this.generation, el=this.contentEl;
    if(!this.loaded){el.empty();el.createEl('h2',{text:'Imports'});el.createEl('p',{text:'Loading imports…'});}
    try{
      const data=await this.plugin.call('/v1/dropbox');if(generation!==this.generation)return;
      const fingerprint=JSON.stringify(data);if(this.loaded&&fingerprint===this.fingerprint)return;this.fingerprint=fingerprint;
      this.loaded=true;el.empty();el.createEl('h2',{text:'Imports'});
      el.createEl('p',{text:'Put files in '+data.folder+'. Finished files leave the folder after they are safely stored.'});
      if(!data.items.length)el.createEl('p',{text:'No imports yet. Put a file in '+data.folder+' to get started.'});
      for(const item of data.items){
        const card=el.createDiv({cls:'coppermind-card'});card.createEl('h3',{text:item.filename});
        const labels={stabilizing:'Waiting for the file to finish syncing',pending:'Waiting to import',importing:'Processing',succeeded:'Done',already_imported:'Done',failed:'Could not import',unsupported:'Unsupported file'};
        card.createEl('p',{text:labels[item.state]||'Processing'});
        if(item.error)card.createEl('p',{text:item.error});
        const part=(item.parts||[]).find(p=>p.artifact_id);
        if(part?.path){const open=card.createEl('button',{text:'Open artifact'});open.onclick=async()=>{
          const file=this.app.vault.getAbstractFileByPath(part.path);
          if(!file){new Notice('The artifact has not synced to this device yet. Try again after sync.');return;}
          await this.app.workspace.getLeaf(false).openFile(file);
        };}
        else if((item.parts||[]).some(p=>p.artifact))card.createEl('p',{text:'Stored as an artifact. This key needs permission to read artifacts to open it.'});
      }
    }catch(e){if(generation!==this.generation)return;this.loaded=false;el.empty();el.createEl('h2',{text:'Imports'});el.createEl('p',{text:'Imports are unavailable. Try again.'});const retry=el.createEl('button',{text:'Retry'});retry.onclick=()=>this.refresh();}
    finally{this.refreshing=false;}
  }
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
      const meeting=data.recording_meeting;
      if(meeting?.candidates?.length>1){
        const box=el.createDiv({cls:'coppermind-card'});box.createEl('h3',{text:'Which meeting was this?'});
        box.createEl('p',{text:meeting.suggested_event_id?'Suggested from the conversation. Please check the choice.':'Several meetings could match. Choose one to use its context.'});
        let choice=meeting.selected_event_id||meeting.suggested_event_id||'';
        for(const event of meeting.candidates){
          const row=box.createEl('label',{cls:'coppermind-meeting-option'});
          const radio=row.createEl('input',{attr:{type:'radio',name:'coppermind-meeting-'+id},value:event.event_id});
          radio.checked=choice===event.event_id;radio.onchange=()=>{if(radio.checked)choice=event.event_id;};
          const people=(event.people||[]).map(p=>p.name).filter(Boolean).join(', ');
          const when=event.start&&!Number.isNaN(Date.parse(event.start))?new Date(event.start).toLocaleString([],{dateStyle:'medium',timeStyle:'short'}):'';
          row.createEl('span',{text:event.title+(when?' · '+when:'')+(event.event_id===meeting.suggested_event_id?' · Suggested':'')+(people?' · '+people:'')});
        }
        new Setting(box).addButton(b=>b.setButtonText('Use selected meeting').setDisabled(!synced).onClick(async()=>{
          if(!choice){new Notice('Choose a meeting first.');return;}
          if(await hash(await this.app.vault.read(file))!==data.note.content_hash){new Notice('The note changed. Wait for sync, then refresh.');return;}
          try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/recording-meeting','POST',{if_match:data.note.content_hash,calendar_hash:meeting.calendar_hash,event_id:choice});new Notice('Meeting selected. Refresh after sync.');await this.refresh();}catch(e){new Notice(e.message);}
        }));
      }
      const selected=new Set();
      const speakerChoices={}, manualSpeakers={}, cards=[];
      const ready=data.state==='suggested'&&!data.stale&&synced;
      const summary=data.meeting_summary?{...data.meeting_summary}:null;
      const merge=data.recording_merge;
      if(['applying','attention'].includes(merge?.state)){
        const box=el.createDiv({cls:'coppermind-card'});box.createEl('h3',{text:'Recording merge needs attention'});
        box.createEl('p',{text:merge.error||'The recording is being added to your note. You can retry if it does not finish.',cls:'coppermind-notice'});
        const retry=box.createEl('button',{text:'Retry merge'});retry.disabled=!synced;
        retry.onclick=async()=>{try{
          if(await hash(await this.app.vault.read(file))!==data.note.content_hash){new Notice('The note changed. Wait for sync, then refresh.');return;}
          const result=await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/recording-merge/retry','POST',{if_match:data.note.content_hash});
          el.empty();el.createEl('h3',{text:'Recording added'});el.createEl('p',{text:'The recording is now part of '+result.target_title+'.'});
          const open=el.createEl('button',{text:'Open '+result.target_title});open.onclick=()=>this.app.workspace.openLinkText(result.target_path,'',false);
        }catch(e){new Notice(e.message);}};
      }
      if(merge?.state==='offered'&&summary){
        const box=el.createDiv({cls:'coppermind-card'});box.createEl('h3',{text:'You already have a note for this meeting'});
        let destination=merge.candidates[0];
        if(merge.candidates.length>1)new Setting(box).setName('Your note').addDropdown(d=>{
          for(const candidate of merge.candidates)d.addOption(candidate.id,candidate.title);
          d.setValue(destination.id).onChange(v=>destination=merge.candidates.find(c=>c.id===v));
        });
        const open=box.createEl('a',{text:'Open your note',href:'#'});open.onclick=e=>{e.preventDefault();this.app.workspace.openLinkText(destination.path,'',false);};
        box.createEl('p',{text:'Your words stay as they are. The recording summary will be added below them with a transcript link. This separate recording note will be retired.'});
        const review=box.createEl('button',{text:'Review merge'});review.disabled=!ready;
        let merging=false;
        review.onclick=()=>new MergeModal(this.app,destination,summary,merge.transcript_path,async()=>{
          if(merging)return;
          merging=true;
          for(const control of el.querySelectorAll('button, input, select, textarea'))control.disabled=true;
          const progress=box.createEl('p',{text:'Adding the recording to your note…',cls:'coppermind-notice'});
          try{
            const targetFile=this.app.vault.getAbstractFileByPath(destination.path);
            if(!targetFile||await hash(await this.app.vault.read(targetFile))!==destination.content_hash||await hash(await this.app.vault.read(file))!==data.note.content_hash)throw Error('A note changed. Wait for sync, then refresh.');
            await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/recording-merge','POST',{if_match:data.note.content_hash,target_id:destination.id,target_hash:destination.content_hash,job_id:data.job_id,meeting_summary:summary,confirmation:'Merge recording'});
            el.empty();el.createEl('h3',{text:'Recording added'});el.createEl('p',{text:'The recording is now part of '+destination.title+'.'});
            const openResult=el.createEl('button',{text:'Open '+destination.title});openResult.onclick=()=>this.app.workspace.openLinkText(destination.path,'',false);
          }catch(e){
            progress.setText('Could not confirm the merge: '+e.message+'. Refresh this panel to check its status before trying again.');
            const refresh=box.createEl('button',{text:'Refresh merge status'});refresh.onclick=()=>this.refresh();
          }
        }).open();
        const dismiss=box.createEl('button',{text:'Keep separate'});dismiss.disabled=!synced;
        dismiss.onclick=async()=>{try{await this.plugin.call('/v1/notes/'+encodeURIComponent(id)+'/recording-merge/dismiss','POST',{if_match:data.note.content_hash});await this.refresh();}catch(e){new Notice(e.message);}};
      }
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
        for(const c of changes){const person=c.people.find(p=>p.id===c.chooser.value);if(person){preview.createEl('p',{text:c.label+' → '+person.name});const count=manual?.voice_appearances?.[c.label];if(count!==undefined)preview.createEl('p',{text:'Also name this voice in '+Math.max(0,count-1)+' earlier recordings. Transcripts and unchanged generated labels will change; your own writing in reviewed notes stays intact.'});}}
        const attendeesNotice=cards.length&&data.speaker_attendees_notice;
        if(attendeesNotice)preview.createEl('p',{text:data.speaker_attendees_notice});
        preview.createEl('p',{text:attendeesNotice?'Completing review updates transcript labels for selected people. Unnamed speakers stay as they are.':'Completing review updates transcript labels, note attendees and links to selected people. Unnamed speakers stay as they are.'});
      };
      const makeChooser=(card,label,people,initial,onSelect,disabled,voice=[])=>{
        const chooser=card.createEl('select');
        chooser.createEl('option',{text:'Leave as '+label,value:''});
        const add=person=>{if(!people.some(p=>p.id===person.id)){people.push(person);chooser.createEl('option',{text:person.name+(person.email?' ('+person.email+')':'')+(person.from_possible_meeting?' · possible meeting':''),value:person.id});}};
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
        if(s.kind==='event'&&meeting?.candidates?.length>1)continue;
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
          if(s.field.startsWith('Unknown voice '))card.createEl('p',{text:'This voice will be remembered across recordings.'});
          if(voice.strong.length===1&&!voice.conflict)card.createEl('p',{text:'Check this suggestion before you finish.'});
          if(manual?.voice_unavailable?.includes(s.field))card.createEl('p',{text:'There was too little speech to recognise this voice in later recordings. A name chosen here applies only to this recording.'});
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
        if(label.startsWith('Unknown voice '))card.createEl('p',{text:'This voice will be remembered across recordings.'});
        if(manual?.voice_unavailable?.includes(label))card.createEl('p',{text:'There was too little speech to recognise this voice in later recordings. A name chosen here applies only to this recording.'});
        card.createEl('p',{text:'Choose a person, or leave this speaker unnamed.'});
        const people=[...manual.candidates];const voice=voicePlan(label,null);
        if(!people.length&&!voice.matches.length)card.createEl('p',{text:'No people suggested. Enter a name below, then choose Find a person.'});
        if(voice.conflict)card.createEl('p',{text:'Suggestions disagree. Choose a person.'});
        else if(voice.initial){card.createEl('p',{text:'Source: Voice'});card.createEl('p',{text:'Check this suggestion before you finish.'});}
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
class WaitingView extends ItemView {
  constructor(leaf,plugin){super(leaf);this.plugin=plugin;this.generation=0;}
  getViewType(){return WAITING_VIEW;}
  getDisplayText(){return 'Meetings to review';}
  getIcon(){return 'list-checks';}
  async onOpen(){await this.refresh();}
  async refresh(){
    const generation=++this.generation, el=this.contentEl;
    el.empty();el.createEl('h2',{text:'Meetings to review'});
    const status=el.createEl('p',{text:'Loading meetings and recordings…'});
    try{
      const data=await this.plugin.call('/v1/recordings/waiting');
      if(generation!==this.generation)return;
      status.setText(`${data.meetings.length} meeting${data.meetings.length===1?'':'s'} to review · ${data.failed.length} recording${data.failed.length===1?'':'s'} failed`);
      const refresh=el.createEl('button',{text:'Refresh'});refresh.onclick=()=>this.refresh();
      el.createEl('h3',{text:`Ready to review (${data.meetings.length})`});
      if(!data.meetings.length)el.createEl('p',{text:'No meetings are waiting for review.'});
      for(const meeting of data.meetings){
        const card=el.createDiv({cls:'coppermind-card'});
        const open=card.createEl('button',{text:meeting.title});
        if(meeting.date){const date=new Date(meeting.date);if(!isNaN(date))card.createEl('p',{text:date.toLocaleDateString()});}
        card.createEl('p',{text:'Open note and review'});
        open.onclick=async()=>{
          const file=this.app.vault.getAbstractFileByPath(meeting.path);
          if(!file){let notice=card.querySelector('.coppermind-notice');if(!notice)notice=card.createEl('p',{cls:'coppermind-notice'});notice.setText('That note is not in this vault yet. Wait for sync, then refresh.');return;}
          await this.app.workspace.getLeaf(false).openFile(file);this.plugin.noteFile=file;await this.plugin.open();
        };
      }
      el.createEl('h3',{text:`Recordings needing attention (${data.failed.length})`});
      if(!data.failed.length)el.createEl('p',{text:'No recordings need attention.'});
      for(const recording of data.failed){
        const card=el.createDiv({cls:'coppermind-card'});card.createEl('h4',{text:recording.title});
        card.createEl('p',{text:recording.message});
        for(const problem of recording.field_problems||[])card.createEl('p',{text:'Details: the '+JSON.stringify(problem.field)+' field '+problem.reason+'.'});
        if(recording.field_problems_unavailable)card.createEl('p',{text:'Details: the specific field was not recorded. Retry to check the note again.'});
        if(recording.retry_after){const date=new Date(recording.retry_after);if(!isNaN(date))card.createEl('p',{text:'Next attempt after '+date.toLocaleString()+'.'});}
        if(recording.can_retry){
          const button=card.createEl('button',{text:'Retry'}), feedback=card.createEl('p');
          button.onclick=async()=>{button.disabled=true;feedback.setText('Queuing retry…');
            try{await this.plugin.call('/v1/recordings/'+encodeURIComponent(recording.id)+'/retry','POST',{});await this.refresh();}
            catch(e){feedback.setText(e.status===403?'This key is missing something the companion needs. In Coppermind Admin, open Connected apps and either pair this device with a code, or make a key with: '+COMPANION_PERMISSIONS+', then select it in the companion settings.':'Could not queue retry. Check your connection and transcription settings, then try again.');button.disabled=false;}
          };
        }
        if(recording.can_dismiss){
        const dismiss=card.createEl('button',{text:'Dismiss'}), confirm=card.createDiv();
        dismiss.onclick=()=>{
          dismiss.disabled=true;confirm.empty();
          confirm.createEl('p',{text:'This only removes the entry from this list and deletes nothing.'});
          const yes=confirm.createEl('button',{text:'Dismiss entry'}), no=confirm.createEl('button',{text:'Cancel'}), note=confirm.createEl('p');
          no.onclick=()=>{confirm.empty();dismiss.disabled=false;};
          yes.onclick=async()=>{yes.disabled=true;no.disabled=true;note.setText('Dismissing…');
            try{await this.plugin.call('/v1/recordings/'+encodeURIComponent(recording.id)+'/dismiss','POST',{});await this.refresh();}
            catch(e){note.setText('Could not dismiss. Check your connection, then try again.');yes.disabled=false;no.disabled=false;}
          };
        };
        }
      }
    }catch(e){if(generation!==this.generation)return;status.setText('Could not load meetings and recordings. Check your connection and API credential, then try again.');status.addClass('coppermind-notice');const retry=el.createEl('button',{text:'Retry'});retry.onclick=()=>this.refresh();}
  }
}
class MergeModal extends Modal {
  constructor(app,destination,summary,transcriptPath,confirm){super(app);this.destination=destination;this.summary=summary;this.transcriptPath=transcriptPath;this.confirm=confirm;}
  onOpen(){
    const el=this.contentEl;el.empty();el.createEl('h2',{text:'Add this recording to '+this.destination.title+'?'});
    el.createEl('p',{text:'Your existing text stays word for word. The recording summary and a transcript link will be added below it.'});
    for(const [key,label] of [['summary','Summary'],['decisions','Decisions'],['action_items','Action items']])if(this.summary[key]?.trim()){el.createEl('h3',{text:label});el.createEl('p',{text:this.summary[key]});}
    if(this.transcriptPath){const link=el.createEl('a',{text:'View transcript',href:'#'});link.onclick=e=>{e.preventDefault();this.app.workspace.openLinkText(this.transcriptPath,'',false);};}
    el.createEl('p',{text:'The recording will belong to your existing note and this separate recording note will be retired.'});
    const confirm=el.createEl('button',{text:'Confirm merge',cls:'mod-cta'});confirm.onclick=async()=>{this.close();await this.confirm();};
    const back=el.createEl('button',{text:'Go back'});back.onclick=()=>this.close();
  }
  onClose(){this.contentEl.empty();}
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
    const el=this.contentEl;el.empty();el.createEl('h2',{text:'People and voices'});
    try{
      const recognition=await this.plugin.call('/v1/entities/voice/recognition');
      new Setting(el).setName('Voice recognition').setDesc('Store voice signatures to recognise speakers in future recordings. No audio clips are saved for recognition.').addToggle(t=>t.setValue(recognition.enabled).onChange(async enabled=>{try{await this.plugin.call('/v1/entities/voice/recognition','PUT',{enabled});await this.show(person);}catch(e){new Notice(e.message);}}));
      if(!recognition.enabled)el.createEl('p',{text:'New voice signatures are not being stored. Existing voice data is listed below.'});
      const unknowns=await this.plugin.call('/v1/entities/voice/unknowns');
      const savedPeople=await this.plugin.call('/v1/entities/voice/people');
      const search=el.createEl('input',{attr:{type:'search',placeholder:'Find a person'}});
      const find=el.createEl('button',{text:'Search'});const list=el.createDiv();
      find.onclick=async()=>{try{list.empty();const page=await this.plugin.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(search.value.trim()));
        if(!page.items.length)list.createEl('p',{text:'No people found.'});
        for(const item of page.items){const b=list.createEl('button',{text:item.name});b.onclick=()=>this.show(item);}
      }catch(e){new Notice(e.message);}};
      el.createEl('h3',{text:'Saved voices'});
      if(!Object.keys(unknowns).length&&!Object.keys(savedPeople).length)el.createEl('p',{text:'No voices saved yet.'});
      for(const [id,item] of Object.entries(savedPeople)){const b=el.createEl('button',{text:item.name+' · Voice data saved'});b.onclick=()=>this.show({id,name:item.name});}
      for(const [id,item] of Object.entries(unknowns)){const b=el.createEl('button',{text:item.name+' · '+item.assignments.length+' recordings'});b.onclick=()=>this.show({id,name:item.name,unknown:true});}
      if(!person)return;
      const path=person.unknown?'/v1/entities/voice/unknowns/'+encodeURIComponent(person.id):'/v1/entities/'+encodeURIComponent(person.id)+'/voice';
      const voice=person.unknown?unknowns[person.id]:await this.plugin.call(path);
      if(!voice){await this.show();return;}
      el.createEl('h3',{text:person.name});
      el.createEl('p',{text:voice.signatures.length+' saved voice signatures'});
      for(const assignment of voice.assignments||[]){const line=el.createDiv();
        try{const note=await this.plugin.call('/v1/notes/'+encodeURIComponent(assignment.note_id));const a=line.createEl('a',{text:note.title,href:'#'});a.onclick=e=>{e.preventDefault();this.app.workspace.openLinkText(note.path,'',false);};}
        catch{line.createSpan({text:'Source note unavailable'});}
        const split=line.createEl('button',{text:'This is not '+person.name});split.onclick=()=>new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call('/v1/entities/voice/voices/'+encodeURIComponent(person.unknown?person.id:'person:'+person.id)+'/split','POST',{note_id:assignment.note_id,label:assignment.label,source_id:assignment.source_id});await this.show(person);}catch(e){new Notice(e.message);}},{title:'Separate this recording?',text:'This recording will use a new unknown voice label. Other recordings stay with '+person.name+'. Your own writing in reviewed notes stays intact.',action:'Separate recording'}).open();
      }
      if(person.unknown){
        el.createEl('p',{text:'Combine this voice with another unknown voice or attach it to a person. Earlier recordings will be relabelled; your own writing in reviewed notes stays intact.'});
        const target=el.createEl('select');target.createEl('option',{text:'Choose a voice or person',value:''});
        for(const [id,item] of Object.entries(unknowns))if(id!==person.id)target.createEl('option',{text:item.name,value:id});
        const query=el.createEl('input',{attr:{type:'search',placeholder:'Find a person'}});const lookup=el.createEl('button',{text:'Find a person'});
        lookup.onclick=async()=>{try{const page=await this.plugin.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(query.value.trim()));for(const p of page.items)target.createEl('option',{text:p.name,value:'person:'+p.id});if(!page.items.length)new Notice('No people found.');}catch(e){new Notice(e.message);}};
        const combine=el.createEl('button',{text:'Choose and combine',cls:'mod-cta'});combine.onclick=()=>{if(!target.value){new Notice('Choose a voice or person first.');return;}const chosen=target.value;const count=new Set((voice.assignments||[]).map(a=>a.note_id)).size;new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call(path+'/combine','POST',{target:chosen});await this.show();}catch(e){new Notice(e.message);}},{title:'Combine these voices?',text:'This will relabel '+count+' recording'+(count===1?'':'s')+'. Transcripts and unchanged generated labels will change; your own writing in reviewed notes stays intact.',action:'Combine voices'}).open();};
      }
      if(person.unknown||voice.signatures.length){const del=el.createEl('button',{text:'Delete voice data',cls:'mod-warning'});del.onclick=()=>new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call(path,'DELETE');await this.show(person.unknown?undefined:person);}catch(e){new Notice(e.message);}},{title:'Delete voice data?',text:'Delete '+voice.signatures.length+' saved voice signature'+(voice.signatures.length===1?'':'s')+' for '+person.name+'? Future recordings cannot be recognised from these signatures. This cannot be undone.',action:'Delete voice data'}).open();}
    }catch(e){el.createEl('p',{text:e.message,cls:'coppermind-notice'});}
  }
}
const NOT_COPPERMIND='That address did not answer like Coppermind. Check the address and try again.';
function readJson(result){try{const value=result.json;return value&&typeof value==='object'?value:null;}catch{return null;}}
function withDetails(message,result,json){
  const raw=json?null:String(result.text??'').replace(/\s+/g,' ').trim().slice(0,200);
  return Object.assign(Error(message),{details:'HTTP '+result.status+(raw?': '+raw:'')});
}
function showError(el,status,e){
  status.setText(e.message);
  if(e.details){const more=el.createEl('details');more.createEl('summary',{text:'Details for support'});more.createEl('p',{text:e.details});}
}
const PAIR_SECRET='coppermind-companion';
const PAIR_HELP='In Coppermind Admin open Obsidian Sync, press Pair companion on this device\'s line, and type the six-digit code it shows.';
class PairModal extends Modal {
  constructor(app,plugin,done){super(app);this.plugin=plugin;this.done=done;}
  onOpen(){
    const el=this.contentEl;el.empty();el.createEl('h2',{text:'Pair with Coppermind'});
    el.createEl('p',{text:PAIR_HELP});
    let address=null;
    if(!this.plugin.settings.url){el.createEl('label',{text:'Address of your Coppermind server'});address=el.createEl('input',{attr:{type:'url',placeholder:'https://coppermind.example'}});}
    el.createEl('label',{text:'Pairing code'});
    const code=el.createEl('input',{attr:{type:'text',inputmode:'numeric',autocomplete:'one-time-code',placeholder:'123 456'}});
    const status=el.createEl('p',{cls:'coppermind-notice'});
    const button=el.createEl('button',{text:'Pair this device',cls:'mod-cta'});
    button.onclick=async()=>{
      button.disabled=true;status.setText('Pairing…');
      try{
        if(address){this.plugin.settings.url=address.value.trim().replace(/\/$/,'');await this.plugin.saveData(this.plugin.settings);}
        const name=await this.plugin.pair(code.value);
        status.setText('Paired. Coppermind knows this device as "'+name+'".');
        new Notice('Coppermind is paired with this device.');
        if(this.done)this.done();
        this.close();
      }catch(e){showError(el,status,e);button.disabled=false;}
    };
  }
  onClose(){this.contentEl.empty();}
}
class Settings extends PluginSettingTab {
  constructor(app,plugin){super(app,plugin);this.plugin=plugin;}
  display(){const el=this.containerEl;el.empty();el.createEl('h2',{text:'Coppermind'});
    new Setting(el).setName('Server address').setDesc('The address of your Coppermind server. Filled in by Coppermind when it put this add-on in your vault.').addText(t=>t.setValue(this.plugin.settings.url).onChange(async v=>{this.plugin.settings.url=v.trim().replace(/\/$/,'');await this.plugin.saveData(this.plugin.settings);}));
    new Setting(el).setName('This device').setDesc(this.plugin.paired()?'Paired with Coppermind. To pair again, unpair this device in Coppermind Admin first, then press Pair.':'Not paired yet. '+PAIR_HELP).addButton(b=>b.setButtonText(this.plugin.paired()?'Pair again':'Pair this device').setCta().onClick(()=>new PairModal(this.app,this.plugin,()=>this.display()).open()));
    new Setting(el).setName('Use a key instead').setDesc('Only if you would rather paste a key than pair with a code. In Coppermind Admin, open Connected apps, choose Connect an app, then Something else, and create a new key with: '+COMPANION_PERMISSIONS+'. Select that key here. Only the name of the saved key is kept in these settings.').addComponent(container=>new SecretComponent(this.app,container).setValue(this.plugin.settings.secret).onChange(async v=>{this.plugin.settings.secret=v;await this.plugin.saveData(this.plugin.settings);}));
  }
}
module.exports=class CoppermindReview extends Plugin {
  async onload(){
    this.settings={url:'',secret:'',...await this.loadData()};
    this.registerView(WAITING_VIEW,leaf=>new WaitingView(leaf,this));
    this.registerView(PEOPLE_VIEW,leaf=>new PeopleView(leaf,this));
    this.registerView(IMPORTS_VIEW,leaf=>new ImportsView(leaf,this));
    this.registerView(VIEW,leaf=>new ReviewView(leaf,this));
    this.addSettingTab(new Settings(this.app,this));
    this.addCommand({id:'meetings-to-review',name:'Meetings to review',callback:()=>this.openWaiting()});
    this.addRibbonIcon('list-checks','Meetings to review',()=>this.openWaiting());
    this.addCommand({id:'show-imports',name:'Show imports',callback:()=>this.openImports()});
    this.addCommand({id:'manage-people',name:'Manage people and voice recognition',callback:()=>this.openPeople()});
    this.addCommand({id:'review-current-note',name:'Review current note',callback:()=>this.open()});
    this.addRibbonIcon('clipboard-check','Review with Coppermind',()=>this.open());
    this.addRibbonIcon('inbox','Coppermind imports',()=>this.openImports());
    this.registerEvent(this.app.workspace.on('file-open',file=>{if(file)this.noteFile=file;}));
    this.addCommand({id:'pair-device',name:'Pair this device with Coppermind',callback:()=>new PairModal(this.app,this).open()});
    // A device that has the add-on but no key yet is asked for the pairing code once it is ready.
    this.app.workspace.onLayoutReady?.(()=>{if(!this.paired())new PairModal(this.app,this).open();});
  }
  paired(){try{return !!(this.settings.secret&&this.app.secretStorage?.getSecret(this.settings.secret));}catch{return false;}}
  deviceName(){
    // The name Obsidian Sync shows for this device, so Admin lists the same name twice over.
    try{const sync=this.app.internalPlugins?.plugins?.sync?.instance;const name=sync?.deviceName||sync?.getDefaultDeviceName?.();if(name)return String(name).slice(0,60);}catch{}
    const agent=typeof navigator==='undefined'?'':navigator.userAgent||'';
    if(/iPhone/.test(agent))return 'iPhone';if(/iPad/.test(agent))return 'iPad';if(/Android/.test(agent))return 'Android device';
    if(/Mac/.test(agent))return 'Mac';if(/Windows/.test(agent))return 'Windows computer';if(/Linux/.test(agent))return 'Linux computer';
    return 'a device';
  }
  async pair(code){
    let url;try{url=new URL(this.settings.url);}catch{throw Error('Type the address of your Coppermind server first.');}
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('Type the address of your Coppermind server first.');
    const digits=String(code||'').replace(/\D/g,'');
    if(digits.length!==6)throw Error('Type the six-digit pairing code shown in Coppermind Admin.');
    const result=await requestUrl({url:this.settings.url+'/v1/pair',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:digits,device:this.deviceName()}),throw:false});
    const json=readJson(result);
    if(result.status>=400||!json?.credential)throw withDetails(json?.message||NOT_COPPERMIND,result,json);
    this.app.secretStorage.setSecret(PAIR_SECRET,json.credential);
    this.settings.secret=PAIR_SECRET;await this.saveData(this.settings);
    return json.name;
  }
  async openWaiting(){
    let leaf=this.app.workspace.getLeavesOfType(WAITING_VIEW)[0]||this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({type:WAITING_VIEW,active:true});await this.app.workspace.revealLeaf(leaf);await leaf.view.refresh();
  }
  async openImports(){let leaf=this.app.workspace.getLeavesOfType(IMPORTS_VIEW)[0]||this.app.workspace.getRightLeaf(false);await leaf.setViewState({type:IMPORTS_VIEW,active:true});await this.app.workspace.revealLeaf(leaf);}
  async openPeople(){
    const file=this.app.workspace.getActiveFile();
    let leaf=this.app.workspace.getLeavesOfType(PEOPLE_VIEW)[0]||this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({type:PEOPLE_VIEW,active:true});await this.app.workspace.revealLeaf(leaf);
    if(file){try{const result=await this.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(file.basename));const person=result.items?.find(p=>p.path===file.path);if(person)await leaf.view.show(person);}catch{}}
  }
  async open(){this.noteFile=this.app.workspace.getActiveFile()||this.noteFile;let leaf=this.app.workspace.getLeavesOfType(VIEW)[0]||this.app.workspace.getRightLeaf(false);await leaf.setViewState({type:VIEW,active:true});await this.app.workspace.revealLeaf(leaf);await leaf.view.refresh();}
  async call(path,method='GET',body){
    const url=new URL(this.settings.url);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('Configure a valid Coppermind HTTP(S) address.');
    const key=this.app.secretStorage?.getSecret(this.settings.secret);if(!key)throw Error('This device is not paired with Coppermind yet. '+PAIR_HELP);
    const result=await requestUrl({url:this.settings.url+path,method,headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,throw:false});
    const namesSpeakers=Object.values(body?.manual_speakers||{}).some(Boolean)||(body?.selected||[]).some(i=>!!body?.speaker_choices?.[i]);
    if(result.status===403&&method==='POST'&&path.endsWith('/review')&&namesSpeakers)throw Error('This key cannot read people, so speakers cannot be named with it. In Coppermind Admin, open Connected apps and either pair this device with a code, or open the key and tick Read notes, Add and change notes, Hand in recordings and files, and Read and correct people and companies.');
    const json=readJson(result);
    if(result.status>=400)throw Object.assign(withDetails(json?.message||'Coppermind could not complete the request. Check connectivity and permissions.',result,json),{status:result.status});
    if(json)return json;
    if(!String(result.text??'').trim())return {};
    throw withDetails(NOT_COPPERMIND,result,json);
  }
};
